#!/usr/bin/env python3
"""Post inline comments to a GitHub PR as a PENDING review (never submits).

Usage:
    post_pending_review.py --repo OWNER/NAME --pr N comments.json [--dry-run]
    post_pending_review.py --repo OWNER/NAME --pr N - < comments.json

comments.json is a list of objects:
    {"path": "src/x.py", "line": 42, "body": "..."}
    {"path": "src/x.py", "start_line": 40, "line": 42, "body": "..."}
    {"path": "src/x.py", "line": 7, "side": "LEFT", "body": "..."}

`side` is RIGHT (new file, the default) or LEFT (old file, for deleted lines).
`start_side` defaults to `side`. Lines must be part of the PR diff (added,
deleted or context lines); every comment is checked before anything is posted.

If the authenticated user already has a pending review on the PR, the comments
are added to it. Otherwise a new pending review is created. The review is
never submitted, the user does that in the browser.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

GH = os.environ.get("GH") or shutil.which("gh") or "gh"
HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def gh(*args, input=None):
    result = subprocess.run(
        [GH, *args], input=input, capture_output=True, text=True
    )
    if result.returncode != 0:
        sys.exit(f"gh {' '.join(args[:3])} failed:\n{result.stdout}{result.stderr}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def graphql(query, **variables):
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        flag = "-F" if isinstance(value, int) else "-f"
        args += [flag, f"{key}={value}"]
    return gh(*args)["data"]


def diff_lines(repo, pr):
    """Map path -> side -> {line: hunk index} for every commentable line."""
    files = gh("api", "--paginate", "--slurp", f"repos/{repo}/pulls/{pr}/files")
    out = {}
    for f in (f for page in files for f in page):
        sides = out.setdefault(f["filename"], {"LEFT": {}, "RIGHT": {}})
        left = right = hunk = None
        for text in f.get("patch", "").splitlines():
            if m := HUNK.match(text):
                left, right = int(m[1]), int(m[2])
                hunk = 0 if hunk is None else hunk + 1
            elif text.startswith("-"):
                sides["LEFT"][left] = hunk
                left += 1
            elif text.startswith("+"):
                sides["RIGHT"][right] = hunk
                right += 1
            elif text.startswith(" ") or text == "":
                sides["LEFT"][left] = hunk
                sides["RIGHT"][right] = hunk
                left += 1
                right += 1
    return out


def normalize(comment):
    c = {k: comment[k] for k in ("path", "line", "body")}
    c["side"] = comment.get("side", "RIGHT").upper()
    if "start_line" in comment:
        c["start_line"] = comment["start_line"]
        c["start_side"] = comment.get("start_side", c["side"]).upper()
    return c


def check(comment, lines):
    """Return a reason the comment can't be posted, or None."""
    sides = lines.get(comment["path"])
    if sides is None:
        return "file is not part of the PR diff"
    end = sides[comment["side"]].get(comment["line"])
    if end is None:
        return f"line {comment['line']} ({comment['side']}) is not in the diff"
    if "start_line" in comment:
        start = sides[comment["start_side"]].get(comment["start_line"])
        if start is None:
            return (
                f"start_line {comment['start_line']} "
                f"({comment['start_side']}) is not in the diff"
            )
        if start != end:
            return "start_line and line are in different diff hunks"
    return None


def location(c):
    span = f"{c['start_line']}-{c['line']}" if "start_line" in c else c["line"]
    return f"{c['path']}:{span} ({c['side']})"


PENDING_QUERY = """
query($owner: String!, $name: String!, $pr: Int!) {
  viewer { login }
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      id
      headRefOid
      url
      reviews(states: [PENDING], first: 10) { nodes { id author { login } } }
    }
  }
}
"""

ADD_THREAD = """
mutation($rid: ID!, $path: String!, $line: Int!, $side: DiffSide!,
         $startLine: Int, $startSide: DiffSide, $body: String!) {
  addPullRequestReviewThread(input: {
    pullRequestReviewId: $rid, path: $path, line: $line, side: $side,
    startLine: $startLine, startSide: $startSide, body: $body
  }) { thread { id } }
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--repo", required=True, help="OWNER/NAME")
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("comments", help="JSON file with comments, or - for stdin")
    parser.add_argument(
        "--dry-run", action="store_true", help="check comments, post nothing"
    )
    args = parser.parse_args()

    source = sys.stdin if args.comments == "-" else open(args.comments)
    comments = [normalize(c) for c in json.load(source)]
    if not comments:
        sys.exit("No comments given")

    lines = diff_lines(args.repo, args.pr)
    problems = [(c, reason) for c in comments if (reason := check(c, lines))]
    for c, reason in problems:
        print(f"INVALID {location(c)}: {reason}", file=sys.stderr)
    if problems:
        sys.exit(f"{len(problems)} of {len(comments)} comments can't be posted, nothing posted")

    owner, name = args.repo.split("/")
    data = graphql(PENDING_QUERY, owner=owner, name=name, pr=args.pr)
    viewer = data["viewer"]["login"]
    pr = data["repository"]["pullRequest"]
    pending = [
        r["id"] for r in pr["reviews"]["nodes"] if r["author"]["login"] == viewer
    ]

    for c in comments:
        print(f"ok {location(c)}")
    if args.dry_run:
        target = "your existing pending review" if pending else "a new pending review"
        print(f"Dry run: {len(comments)} comments would go to {target} as {viewer}")
        return

    if pending:
        for c in comments:
            variables = dict(rid=pending[0], path=c["path"], line=c["line"],
                             side=c["side"], body=c["body"])
            if "start_line" in c:
                variables.update(startLine=c["start_line"], startSide=c["start_side"])
            graphql(ADD_THREAD, **variables)
        print(f"Added {len(comments)} comments to {viewer}'s existing pending review")
    else:
        payload = json.dumps({"commit_id": pr["headRefOid"], "comments": comments})
        review = gh("api", "-X", "POST", f"repos/{args.repo}/pulls/{args.pr}/reviews",
                    "--input", "-", input=payload)
        assert review["state"] == "PENDING", review["state"]
        print(f"Created pending review {review['id']} for {viewer} with {len(comments)} comments")
    print(f"Review and submit at {pr['url']}/files")


if __name__ == "__main__":
    main()
