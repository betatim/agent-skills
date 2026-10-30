#!/usr/bin/env python3
"""Assemble the deterministic context bundle for one PR.

Everything here is mechanical and sourced. The judgment that this bundle feeds
-- reach, counterfactual availability, cost, and the questions the decision
hinges on -- is the model's job, not this script's.

Four things are gathered:

1. PR metadata, body, and the diff.
2. Linked issues with their discussion, which is usually where the only
   statement of user impact lives.
3. The public API surface delta, computed by parsing the base and head versions
   of each changed Python file with `ast` and comparing them. Path heuristics do
   not work for scikit-learn, whose public names live in privately-named files
   such as `_classification.py`, so the surface is derived from the code itself.
4. Prior art: earlier issues and PRs proposing the same thing, plus other open
   PRs touching the same files. The latter comes from the local cache at zero
   API cost and is how overlapping PRs get noticed.
"""

from __future__ import annotations

import argparse
import ast
import base64
import json
import pathlib
import re
import subprocess
import sys
import time

DEFAULT_REPO = "scikit-learn/scikit-learn"
# The REST search API allows 30 requests/minute. Stay well under it.
SEARCH_SLEEP_S = 2.5
MAX_SEARCH_QUERIES = 5
MAX_SURFACE_FILES = 12
MAX_LINKED_ISSUES = 6

PR_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number title url body state isDraft createdAt updatedAt
      additions deletions changedFiles
      author { login }
      authorAssociation
      baseRefName headRefName baseRefOid headRefOid
      isCrossRepository
      headRepository { nameWithOwner }
      labels(first: 25) { nodes { name } }
      files(first: 100) { totalCount nodes { path additions deletions } }
      comments(first: 60) {
        totalCount
        nodes { author { login } authorAssociation createdAt body }
      }
      reviews(first: 40) {
        totalCount
        nodes { author { login } authorAssociation state submittedAt body }
      }
      closingIssuesReferences(first: 10) { nodes { number } }
    }
  }
}
"""

ISSUE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      number title url state stateReason createdAt closedAt body
      author { login }
      reactions { totalCount }
      labels(first: 25) { nodes { name } }
      comments(first: 40) {
        totalCount
        nodes { author { login } authorAssociation createdAt body }
      }
    }
  }
}
"""

STOPWORDS = {
    "add", "adds", "added", "adding", "allow", "allows", "and", "any", "are", "avoid",
    "based", "better", "between", "bug", "but", "can", "case", "cases", "change",
    "changes", "check", "class", "common", "correct", "data", "default", "deprecate",
    "doc", "docs", "does", "dont", "during", "else", "ensure", "error", "example",
    "examples", "fail", "failing", "fails", "fix", "fixed", "fixes", "for", "from",
    "function", "handle", "handling", "has", "have", "improve", "improved", "input",
    "instead", "into", "issue", "make", "makes", "method", "methods", "missing",
    "more", "new", "not", "now", "only", "option", "parameter", "parameters", "pass",
    "raise", "raises", "remove", "removed", "return", "returns", "same", "set",
    "should", "small", "some", "support", "test", "tests", "that", "the", "their",
    "then", "there", "this", "throw", "use", "used", "using", "value", "values",
    "when", "where", "which", "with", "without", "wrong",
}


# --------------------------------------------------------------------------- gh


def gh(args: list[str]) -> str:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"{' '.join(args[:3])} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def graphql(query: str, **variables) -> dict:
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        args += ["-F", f"{key}={value}"]
    payload = json.loads(gh(args))
    if "errors" in payload:
        raise RuntimeError("GraphQL errors: " + json.dumps(payload["errors"])[:300])
    return payload["data"]


def fetch_blob(repo: str, path: str, ref: str) -> str | None:
    """File contents at a ref. Works for fork head SHAs via the base repo."""
    try:
        raw = gh(["gh", "api", f"repos/{repo}/contents/{path}?ref={ref}"])
    except RuntimeError:
        return None
    try:
        node = json.loads(raw)
        if node.get("encoding") != "base64":
            return None
        return base64.b64decode(node["content"]).decode("utf-8", errors="replace")
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


# ----------------------------------------------------------- public API surface


def _params(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, str]:
    """Parameter name -> rendered default ('' when the parameter is required)."""
    out: dict[str, str] = {}
    pos = list(node.args.posonlyargs) + list(node.args.args)
    defaults = list(node.args.defaults)
    pad = len(pos) - len(defaults)
    for index, arg in enumerate(pos):
        if arg.arg == "self":
            continue
        default = defaults[index - pad] if index >= pad else None
        out[arg.arg] = _render(default)
    if node.args.vararg:
        out["*" + node.args.vararg.arg] = ""
    for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        out[arg.arg] = _render(default)
    if node.args.kwarg:
        out["**" + node.args.kwarg.arg] = ""
    return out


def _render(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return "?"


def public_surface(source: str) -> dict:
    """Public functions, classes and methods with their parameters.

    `__init__` is treated as public because for a scikit-learn estimator its
    parameters *are* the public API.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    surface: dict[str, dict[str, str]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                surface[node.name] = _params(node)
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            surface[node.name + " (class)"] = {}
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if item.name.startswith("_") and item.name != "__init__":
                    continue
                surface[f"{node.name}.{item.name}"] = _params(item)
    return surface


def surface_delta(base_src: str | None, head_src: str | None) -> dict:
    """Added/removed callables, and added/removed/retyped parameters."""
    base = public_surface(base_src) if base_src else {}
    head = public_surface(head_src) if head_src else {}
    delta: dict[str, list] = {
        "added_callables": sorted(set(head) - set(base)),
        "removed_callables": sorted(set(base) - set(head)),
        "added_params": [],
        "removed_params": [],
        "changed_defaults": [],
    }
    for name in sorted(set(base) & set(head)):
        before, after = base[name], head[name]
        for param in sorted(set(after) - set(before)):
            default = after[param]
            delta["added_params"].append(
                f"{name}({param}{'=' + default if default else ''})"
            )
        for param in sorted(set(before) - set(after)):
            delta["removed_params"].append(f"{name}({param})")
        for param in sorted(set(before) & set(after)):
            if before[param] != after[param]:
                delta["changed_defaults"].append(
                    f"{name}({param}): {before[param] or '<required>'} -> {after[param] or '<required>'}"
                )
    return delta


# ------------------------------------------------------------------ prior art


def search_terms(title: str, surface: dict) -> list[str]:
    """Distinctive identifiers worth searching history for."""
    terms: list[str] = []
    for match in re.findall(r"`([^`]+)`", title):
        terms.append(match.strip())
    for match in re.findall(r"\b([A-Z][a-zA-Z0-9]*[a-z][a-zA-Z0-9]*[A-Z][a-zA-Z0-9]*)\b", title):
        terms.append(match)
    for entry in surface.get("added_params", []):
        name = re.search(r"\(([^=)]+)", entry)
        if name:
            terms.append(name.group(1))
    for entry in surface.get("added_callables", []):
        terms.append(entry.replace(" (class)", "").split(".")[-1])
    for word in re.findall(r"\b[a-z][a-z_]{4,}\b", title.lower()):
        if word not in STOPWORDS:
            terms.append(word)

    seen: list[str] = []
    for term in terms:
        term = term.strip().strip("`'\"")
        if len(term) < 4 or term.lower() in STOPWORDS:
            continue
        if term.lower() not in {t.lower() for t in seen}:
            seen.append(term)
    return seen[:MAX_SEARCH_QUERIES]


def is_identifier_like(term: str) -> bool:
    """Code identifiers are high-precision search terms; bare English words are not."""
    return "_" in term or any(c.isupper() for c in term)


def prior_art(repo: str, number: int, terms: list[str]) -> tuple[list[dict], list[str]]:
    """Earlier issues and PRs mentioning the same identifiers.

    Broad single-word terms are capped hard. Searching scikit-learn history for
    a word like "categorical" returns dozens of unrelated hits that bury the one
    result that matters, which is usually a previous attempt at the same change.
    """
    hits: dict[int, dict] = {}
    notes: list[str] = []
    # Search the precise terms first, so the per-term budget is spent well.
    ordered = sorted(terms, key=lambda t: not is_identifier_like(t))
    for index, term in enumerate(ordered):
        if index:
            time.sleep(SEARCH_SLEEP_S)
        broad = not is_identifier_like(term)
        cap = 4 if broad else 25
        query = f'repo:{repo} "{term}" in:title'
        try:
            raw = gh([
                "gh", "api", "-X", "GET", "search/issues",
                "-f", f"q={query}", "-f", "per_page=30",
                "-f", "sort=created", "-f", "order=desc",
            ])
        except RuntimeError as exc:
            notes.append(f"search for {term!r} failed ({str(exc)[:80]}) -- prior art may be incomplete")
            continue
        try:
            payload = json.loads(raw)
            items = payload.get("items", [])
        except json.JSONDecodeError:
            notes.append(f"search for {term!r} returned unparseable JSON")
            continue
        if broad and payload.get("total_count", 0) > cap:
            notes.append(
                f"{term!r} is a broad term with {payload['total_count']} title matches; "
                f"showing the {cap} most recent only"
            )
        for item in items[:cap]:
            if item["number"] == number:
                continue
            hits.setdefault(item["number"], {
                "number": item["number"],
                "title": item["title"],
                "url": item["html_url"],
                "kind": "pr" if "pull_request" in item else "issue",
                "state": item["state"],
                "merged": bool(item.get("pull_request", {}).get("merged_at")),
                "created_at": item["created_at"][:10],
                "closed_at": (item.get("closed_at") or "")[:10],
                "matched": term,
            })
    return sorted(hits.values(), key=lambda h: h["created_at"]), notes


def overlapping_open_prs(cache: pathlib.Path, number: int, paths: set[str]) -> list[dict]:
    """Other open PRs touching the same files. Read from the cache, no API cost."""
    if not cache.exists():
        return []
    try:
        prs = json.loads(cache.read_text())["prs"]
    except (json.JSONDecodeError, KeyError, OSError):
        return []
    out = []
    for pr in prs:
        if pr["number"] == number:
            continue
        shared = paths & {f["path"] for f in pr["files"]["nodes"]}
        if shared:
            out.append({
                "number": pr["number"],
                "title": pr["title"],
                "url": pr["url"],
                "created_at": pr["createdAt"][:10],
                "is_draft": pr["isDraft"],
                "shared_files": sorted(shared),
            })
    return sorted(out, key=lambda p: -len(p["shared_files"]))


# --------------------------------------------------------------------- render


def truncate(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text or "(empty)"
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more characters]"


def render(bundle: dict, diff_lines: int) -> str:
    pr = bundle["pr"]
    out: list[str] = []
    add = out.append

    add(f"# Context bundle: PR #{pr['number']} -- {pr['title']}")
    add("")
    add(f"- URL: {pr['url']}")
    add(f"- Author: {(pr.get('author') or {}).get('login')} ({pr['authorAssociation']})")
    add(f"- Opened {pr['createdAt'][:10]}, last updated {pr['updatedAt'][:10]}")
    add(f"- Size: +{pr['additions']}/-{pr['deletions']} across {pr['changedFiles']} files")
    add(f"- Labels: {', '.join(n['name'] for n in pr['labels']['nodes']) or 'none'}")
    add(f"- Base {pr['baseRefName']} @ {pr['baseRefOid'][:9]}, head {pr['headRefName']} @ {pr['headRefOid'][:9]}")
    add(f"- Comments: {pr['comments']['totalCount']}, reviews: {pr['reviews']['totalCount']}")
    add("")

    add("## PR body")
    add("")
    add(truncate(pr["body"], 4000))
    add("")

    add("## Changed files")
    add("")
    for f in pr["files"]["nodes"]:
        add(f"- `{f['path']}` +{f['additions']}/-{f['deletions']}")
    if pr["files"]["totalCount"] > len(pr["files"]["nodes"]):
        add(f"- ... {pr['files']['totalCount'] - len(pr['files']['nodes'])} more files")
    add("")

    add("## Public API surface delta")
    add("")
    add("Computed by parsing base and head with `ast`. Empty means this PR adds no public surface.")
    add("")
    add("Caveat: this is a per-file, per-class view and does not follow inheritance. A method "
        "moved to a new base class shows up as both removed and added, which is a refactor "
        "rather than a removal of public API. Check the diff before describing anything as removed.")
    add("")
    surface = bundle["surface"]
    if not any(surface.get(k) for k in ("added_callables", "removed_callables", "added_params", "removed_params", "changed_defaults")):
        add("No public API surface change detected.")
    else:
        for key, label in [
            ("added_callables", "New public callables"),
            ("removed_callables", "Removed public callables"),
            ("added_params", "New parameters"),
            ("removed_params", "Removed parameters"),
            ("changed_defaults", "Changed defaults (behaviour change for existing users)"),
        ]:
            if surface.get(key):
                add(f"**{label}:**")
                for entry in surface[key]:
                    add(f"- `{entry}`")
                add("")
    for note in bundle["surface_notes"]:
        add(f"- note: {note}")
    add("")

    add("## Linked issues")
    add("")
    if not bundle["issues"]:
        add("No linked issue. The PR body is the only statement of motivation.")
    for issue in bundle["issues"]:
        state = issue["state"] + (f"/{issue['stateReason']}" if issue.get("stateReason") else "")
        add(f"### #{issue['number']} {issue['title']} [{state}]")
        add("")
        add(f"{issue['url']} -- opened {issue['createdAt'][:10]} by "
            f"{(issue.get('author') or {}).get('login')}, "
            f"{issue['reactions']['totalCount']} reactions, "
            f"{issue['comments']['totalCount']} comments")
        add(f"Labels: {', '.join(n['name'] for n in issue['labels']['nodes']) or 'none'}")
        add("")
        add(truncate(issue["body"], 2500))
        add("")
        if issue["comments"]["nodes"]:
            add("Discussion:")
            add("")
            for comment in issue["comments"]["nodes"]:
                who = (comment.get("author") or {}).get("login")
                add(f"- **{who}** ({comment['authorAssociation']}, {comment['createdAt'][:10]}): "
                    f"{truncate(comment['body'], 700)}")
            add("")

    add("## PR discussion")
    add("")
    if not pr["comments"]["nodes"] and not pr["reviews"]["nodes"]:
        add("None. Nobody has said anything on this PR.")
    for comment in pr["comments"]["nodes"]:
        who = (comment.get("author") or {}).get("login")
        add(f"- **{who}** ({comment['authorAssociation']}, {comment['createdAt'][:10]}): "
            f"{truncate(comment['body'], 700)}")
    for review in pr["reviews"]["nodes"]:
        who = (review.get("author") or {}).get("login")
        add(f"- **{who}** review {review['state']} ({review['authorAssociation']}, "
            f"{(review.get('submittedAt') or '')[:10]}): {truncate(review['body'], 700)}")
    add("")

    add("## Prior art: history mentioning the same identifiers")
    add("")
    add(f"Search terms: {', '.join(bundle['terms']) or 'none derived'}")
    add("")
    if not bundle["prior_art"]:
        add("No earlier issues or PRs found for these terms.")
    for hit in bundle["prior_art"]:
        status = "merged" if hit["merged"] else hit["state"]
        closed = f", closed {hit['closed_at']}" if hit["closed_at"] else ""
        add(f"- [{hit['kind']} #{hit['number']}]({hit['url']}) [{status}] "
            f"created {hit['created_at']}{closed} -- {hit['title']} (matched `{hit['matched']}`)")
    for note in bundle["prior_art_notes"]:
        add(f"- note: {note}")
    add("")

    add("## Other open PRs touching the same files")
    add("")
    if not bundle["overlaps"]:
        add("None.")
    for overlap in bundle["overlaps"]:
        draft = " (draft)" if overlap["is_draft"] else ""
        add(f"- [#{overlap['number']}]({overlap['url']}){draft} opened {overlap['created_at']} -- "
            f"{overlap['title']} -- shares {', '.join('`' + p + '`' for p in overlap['shared_files'])}")
    add("")

    add("## Diff")
    add("")
    diff = bundle["diff"].splitlines()
    if len(diff) > diff_lines:
        add(f"Truncated to the first {diff_lines} of {len(diff)} lines.")
        add("")
        diff = diff[:diff_lines]
    add("```diff")
    out.extend(diff)
    add("```")
    return "\n".join(out)


# ----------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("number", type=int, help="PR number")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--out", type=pathlib.Path, help="write to this path instead of stdout")
    parser.add_argument("--json", action="store_true", help="emit the raw bundle as JSON")
    parser.add_argument("--diff-lines", type=int, default=2000, help="cap on diff lines (default 2000)")
    parser.add_argument("--no-prior-art", action="store_true", help="skip the throttled history search")
    parser.add_argument("--cache", type=pathlib.Path, help="open-PR cache, for overlap detection")
    args = parser.parse_args()

    owner, name = args.repo.split("/", 1)
    try:
        pr = graphql(PR_QUERY, owner=owner, name=name, number=args.number)["repository"]["pullRequest"]
    except RuntimeError as exc:
        print(f"error: could not fetch PR #{args.number}: {exc}", file=sys.stderr)
        return 1
    if pr is None:
        print(f"error: PR #{args.number} not found in {args.repo}", file=sys.stderr)
        return 1

    try:
        diff = gh(["gh", "pr", "diff", str(args.number), "--repo", args.repo])
    except RuntimeError as exc:
        diff = f"(diff unavailable: {exc})"

    # Public API surface delta, per changed Python file.
    surface: dict[str, list] = {
        "added_callables": [], "removed_callables": [],
        "added_params": [], "removed_params": [], "changed_defaults": [],
    }
    surface_notes: list[str] = []
    py_files = [
        f["path"] for f in pr["files"]["nodes"]
        if f["path"].endswith(".py") and "/tests/" not in f["path"]
    ]
    if len(py_files) > MAX_SURFACE_FILES:
        surface_notes.append(
            f"surface computed for the first {MAX_SURFACE_FILES} of {len(py_files)} changed Python files"
        )
        py_files = py_files[:MAX_SURFACE_FILES]
    for path in py_files:
        base_src = fetch_blob(args.repo, path, pr["baseRefOid"])
        head_src = fetch_blob(args.repo, path, pr["headRefOid"])
        if base_src is None and head_src is None:
            surface_notes.append(f"could not read either version of `{path}`")
            continue
        delta = surface_delta(base_src, head_src)
        for key, entries in delta.items():
            surface[key].extend(f"{path}: {entry}" for entry in entries)

    # Linked issues: explicit closing references plus Fixes/Closes in the body.
    numbers = [node["number"] for node in pr["closingIssuesReferences"]["nodes"]]
    for match in re.findall(r"(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s*:?\s*#(\d+)", pr["body"] or "", re.I):
        if int(match) not in numbers:
            numbers.append(int(match))
    issues = []
    for issue_number in numbers[:MAX_LINKED_ISSUES]:
        try:
            issue = graphql(ISSUE_QUERY, owner=owner, name=name, number=issue_number)["repository"]["issue"]
        except RuntimeError:
            issue = None
        if issue:
            issues.append(issue)

    terms = [] if args.no_prior_art else search_terms(pr["title"], surface)
    hits, prior_notes = prior_art(args.repo, args.number, terms) if terms else ([], [])
    if args.no_prior_art:
        prior_notes = ["prior-art search skipped (--no-prior-art)"]

    cache = args.cache or pathlib.Path("agents/impact/.cache") / f"open-prs-{args.repo.replace('/', '-')}.json"
    overlaps = overlapping_open_prs(cache, args.number, {f["path"] for f in pr["files"]["nodes"]})

    bundle = {
        "pr": pr, "diff": diff, "surface": surface, "surface_notes": surface_notes,
        "issues": issues, "terms": terms, "prior_art": hits,
        "prior_art_notes": prior_notes, "overlaps": overlaps,
    }

    text = json.dumps(bundle, indent=1) if args.json else render(bundle, args.diff_lines)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
        print(f"wrote context bundle to {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
