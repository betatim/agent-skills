#!/usr/bin/env python3
"""Fetch every open PR of a repository into a local JSON cache via the GitHub GraphQL API.

Only bulk metadata is fetched here: enough to apply the worklist filter without
touching the REST search API, which is capped at 30 requests/minute. Per-PR
detail (body, diff, discussion) is the job of pr_context.py.

Page size is deliberately 25. Larger pages combined with the nested `files` and
`comments` selections exceed GitHub's node limit, and the failure mode is an
empty response body rather than an error, so it is easy to mistake for success.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import subprocess
import sys
import time

DEFAULT_REPO = "scikit-learn/scikit-learn"
PAGE_SIZE = 25

QUERY = """
query($owner: String!, $name: String!, $size: Int!, $cursor: String) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: $size, after: $cursor) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url createdAt updatedAt isDraft
        additions deletions changedFiles
        author { login }
        authorAssociation
        labels(first: 20) { nodes { name } }
        comments(last: 40) { totalCount nodes { authorAssociation } }
        reviews(last: 40) { totalCount nodes { authorAssociation state } }
        files(first: 60) { totalCount nodes { path } }
        closingIssuesReferences(first: 5) { nodes { number title url } }
      }
    }
  }
}
"""


class FetchError(RuntimeError):
    pass


def run_graphql(owner: str, name: str, cursor: str | None, size: int) -> dict:
    """Execute one GraphQL page, with retries for transient and node-limit failures."""
    args = [
        "gh", "api", "graphql",
        "-f", f"query={QUERY}",
        "-F", f"owner={owner}",
        "-F", f"name={name}",
        "-F", f"size={size}",
    ]
    if cursor:
        args += ["-F", f"cursor={cursor}"]

    last_problem = "unknown"
    for attempt in range(4):
        proc = subprocess.run(args, capture_output=True, text=True)

        # An empty body is the signature of exceeding the node limit. It is not
        # reported as an error, so it must be checked explicitly.
        if not proc.stdout.strip():
            last_problem = f"empty response body (exit {proc.returncode}): {proc.stderr.strip()[:300]}"
        else:
            try:
                payload = json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                last_problem = f"unparseable response: {exc}"
            else:
                if "errors" in payload:
                    last_problem = "GraphQL errors: " + json.dumps(payload["errors"])[:300]
                elif not payload.get("data", {}).get("repository"):
                    last_problem = "response contained no repository data"
                else:
                    return payload

        if attempt < 3:
            time.sleep(2 * (attempt + 1))

    raise FetchError(last_problem)


def fetch_all(owner: str, name: str, size: int, quiet: bool) -> tuple[list[dict], dict]:
    nodes: list[dict] = []
    cursor: str | None = None
    cost = 0
    rate: dict = {}

    while True:
        payload = run_graphql(owner, name, cursor, size)
        rate = payload["data"]["rateLimit"]
        cost += rate["cost"]
        page = payload["data"]["repository"]["pullRequests"]
        nodes += page["nodes"]

        if not quiet:
            print(
                f"  fetched {len(nodes)}/{page['totalCount']} open PRs",
                file=sys.stderr,
            )
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]

    return nodes, {"cost": cost, "remaining": rate.get("remaining")}


def cache_age_hours(path: pathlib.Path) -> float | None:
    """Age of the cache in hours, or None if it is absent or unreadable."""
    if not path.exists():
        return None
    try:
        stamp = json.loads(path.read_text())["fetched_at"]
        fetched = dt.datetime.fromisoformat(stamp)
    except (json.JSONDecodeError, KeyError, ValueError, OSError):
        return None
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=dt.timezone.utc)
    delta = dt.datetime.now(dt.timezone.utc) - fetched
    return delta.total_seconds() / 3600.0


def default_cache_path(repo: str) -> pathlib.Path:
    slug = repo.replace("/", "-")
    return pathlib.Path("agents/impact/.cache") / f"open-prs-{slug}.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"owner/name (default {DEFAULT_REPO})")
    parser.add_argument("--cache", type=pathlib.Path, help="cache file path")
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=24.0,
        help="reuse the cache if it is younger than this (default 24)",
    )
    parser.add_argument("--force", action="store_true", help="refetch regardless of cache age")
    parser.add_argument("--quiet", action="store_true", help="suppress progress output")
    args = parser.parse_args()

    if "/" not in args.repo:
        print(f"error: --repo must be owner/name, got {args.repo!r}", file=sys.stderr)
        return 2
    owner, name = args.repo.split("/", 1)

    cache = args.cache or default_cache_path(args.repo)
    age = cache_age_hours(cache)

    if not args.force and age is not None and age < args.max_age_hours:
        data = json.loads(cache.read_text())
        print(f"cache fresh ({age:.1f}h old): {data['count']} open PRs at {cache}")
        return 0

    if not args.quiet:
        reason = "no usable cache" if age is None else f"cache {age:.1f}h old"
        print(f"fetching {args.repo} ({reason})...", file=sys.stderr)

    try:
        nodes, usage = fetch_all(owner, name, PAGE_SIZE, args.quiet)
    except FetchError as exc:
        print(f"error: fetch failed: {exc}", file=sys.stderr)
        return 1

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {
                "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "repo": args.repo,
                "count": len(nodes),
                "prs": nodes,
            },
            indent=1,
        )
    )

    truncated = [n["number"] for n in nodes if n["files"]["totalCount"] > 60]
    print(f"wrote {len(nodes)} open PRs to {cache} (rate-limit cost {usage['cost']}, {usage['remaining']} remaining)")
    if truncated:
        print(f"note: file lists truncated at 60 for PRs {truncated}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
