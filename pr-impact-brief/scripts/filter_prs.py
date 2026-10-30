#!/usr/bin/env python3
"""Apply the worklist filter to the open-PR cache and draw the next PR to brief.

The default worklist is PRs that touch scikit-learn source and that no core
developer has ever commented on or reviewed. Those are the PRs where the
question "should we carry this?" has demonstrably never been asked.

Core-dev engagement is detected via `authorAssociation` of MEMBER or OWNER on
any comment or review. PRs authored by a core dev are deliberately kept when no
*other* core dev has engaged: an unattended core-dev PR carries the same
unanswered question.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import re
import sys

CORE_ASSOCIATIONS = {"MEMBER", "OWNER"}
FEATURE_TITLE = re.compile(r"^\s*(ENH|FEA|API|PRF)\b", re.IGNORECASE)
FEATURE_LABELS = {"New Feature", "Enhancement", "API"}
DOC_SUFFIXES = (".rst", ".md", ".txt")
# Leading commit-tag conventions stripped when building a filename slug.
TITLE_TAG = re.compile(
    r"^\s*(ENH|FEA|FIX|DOC|MAINT|CI|TST|BLD|PRF|API|RFC|CLN|BUG|POC|WIP)[:\s]+",
    re.IGNORECASE,
)


def is_bot(pr: dict) -> bool:
    login = (pr.get("author") or {}).get("login") or ""
    return login.endswith("[bot]") or login in {"scikit-learn-bot", "dependabot"}


def core_engaged(pr: dict) -> bool:
    for comment in pr["comments"]["nodes"]:
        if comment["authorAssociation"] in CORE_ASSOCIATIONS:
            return True
    for review in pr["reviews"]["nodes"]:
        if review["authorAssociation"] in CORE_ASSOCIATIONS:
            return True
    return False


def core_approved(pr: dict) -> bool:
    return any(
        r["state"] == "APPROVED" and r["authorAssociation"] in CORE_ASSOCIATIONS
        for r in pr["reviews"]["nodes"]
    )


def touches_sklearn_source(pr: dict) -> bool:
    for f in pr["files"]["nodes"]:
        path = f["path"]
        if not path.startswith("sklearn/"):
            continue
        if "/tests/" in path or path.startswith("sklearn/tests/"):
            continue
        if path.endswith((".py", ".pyx", ".pxd", ".pyx.tp")):
            return True
    return False


def docs_only(pr: dict) -> bool:
    paths = [f["path"] for f in pr["files"]["nodes"]]
    if not paths:
        return False
    return all(
        p.startswith(("doc/", "examples/")) or p.endswith(DOC_SUFFIXES) for p in paths
    )


def featurey(pr: dict) -> bool:
    labels = {node["name"] for node in pr["labels"]["nodes"]}
    return bool(FEATURE_TITLE.match(pr["title"]) or (labels & FEATURE_LABELS))


def slug_for(pr: dict, words: int = 7) -> str:
    """Deterministic filename slug, so the same PR always maps to the same file."""
    title = TITLE_TAG.sub("", pr["title"])
    cleaned = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    parts = [part for part in cleaned.split("-") if part]
    return "-".join(parts[:words]) or "untitled"


def modules_of(pr: dict) -> list[str]:
    return sorted(
        node["name"].removeprefix("module:")
        for node in pr["labels"]["nodes"]
        if node["name"].startswith("module:")
    )


def build_worklist(prs: list[dict], mode: str) -> list[dict]:
    candidates = [p for p in prs if not p["isDraft"] and not is_bot(p)]
    if mode == "all":
        return candidates
    selected = [
        p
        for p in candidates
        if not core_engaged(p)
        and touches_sklearn_source(p)
        and not docs_only(p)
        and not core_approved(p)
    ]
    if mode == "featurey":
        selected = [p for p in selected if featurey(p)]
    return selected


def already_briefed(brief_dir: pathlib.Path) -> set[int]:
    """PR numbers that already have a brief. Existence of the file is the state.

    Recursive, so filing a brief under `done/` still counts as briefed. Without
    that, archiving a brief would put its PR back in the draw.
    """
    numbers: set[int] = set()
    if not brief_dir.exists():
        return numbers
    for path in brief_dir.rglob("*.md"):
        head = path.stem.split("-", 1)[0]
        if head.isdigit():
            numbers.add(int(head))
    return numbers


def print_table(rows: list[dict], briefed: set[int]) -> None:
    for pr in rows:
        mark = "*" if pr["number"] in briefed else " "
        mods = ",".join(modules_of(pr)) or "-"
        print(
            f"{mark}#{pr['number']:<6} {pr['createdAt'][:7]}  "
            f"+{pr['additions']:<5}/-{pr['deletions']:<5} "
            f"{pr['authorAssociation'][:22]:<22} {mods[:24]:<24} {pr['title'][:70]}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", type=pathlib.Path, help="open-PR cache path")
    parser.add_argument("--repo", default="scikit-learn/scikit-learn")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--featurey",
        action="store_const",
        const="featurey",
        dest="mode",
        help="narrow to public-surface additions only",
    )
    group.add_argument(
        "--all",
        action="store_const",
        const="all",
        dest="mode",
        help="widen to every human non-draft PR",
    )
    parser.add_argument(
        "--brief-dir",
        type=pathlib.Path,
        default=pathlib.Path("agents/impact"),
        help="directory of existing briefs (default agents/impact)",
    )
    parser.add_argument("--pick", type=int, metavar="N", help="draw N unbriefed PRs at random")
    parser.add_argument("--seed", type=int, help="seed the random draw for reproducibility")
    parser.add_argument("--include-briefed", action="store_true", help="do not skip briefed PRs")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    parser.add_argument("--counts", action="store_true", help="print filter cardinalities and exit")
    parser.set_defaults(mode="default")
    args = parser.parse_args()

    cache = args.cache or pathlib.Path("agents/impact/.cache") / f"open-prs-{args.repo.replace('/', '-')}.json"
    if not cache.exists():
        print(f"error: no cache at {cache}; run fetch_open_prs.py first", file=sys.stderr)
        return 1
    data = json.loads(cache.read_text())
    prs = data["prs"]

    if args.counts:
        print(f"cache fetched {data['fetched_at']}  repo {data['repo']}")
        print(f"{'total open':46s} {len(prs)}")
        print(f"{'  drafts':46s} {sum(1 for p in prs if p['isDraft'])}")
        print(f"{'  bot-authored':46s} {sum(1 for p in prs if is_bot(p))}")
        for label, mode in [
            ("--all (human, non-draft)", "all"),
            ("default (unengaged + touches sklearn)", "default"),
            ("--featurey", "featurey"),
        ]:
            print(f"{label:46s} {len(build_worklist(prs, mode))}")
        return 0

    worklist = build_worklist(prs, args.mode)
    worklist.sort(key=lambda p: p["createdAt"])
    briefed = already_briefed(args.brief_dir)

    pool = worklist if args.include_briefed else [p for p in worklist if p["number"] not in briefed]

    if args.pick is not None:
        if not pool:
            print(
                f"worklist exhausted: all {len(worklist)} PRs in mode '{args.mode}' already have briefs "
                f"in {args.brief_dir}",
                file=sys.stderr,
            )
            return 3
        rng = random.Random(args.seed)
        pool = rng.sample(pool, min(args.pick, len(pool)))

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "number": p["number"],
                        "title": p["title"],
                        "url": p["url"],
                        "slug": slug_for(p),
                        "brief_path": str(args.brief_dir / f"{p['number']}-{slug_for(p)}.md"),
                        "created_at": p["createdAt"],
                        "updated_at": p["updatedAt"],
                        "author": (p.get("author") or {}).get("login"),
                        "author_association": p["authorAssociation"],
                        "additions": p["additions"],
                        "deletions": p["deletions"],
                        "changed_files": p["changedFiles"],
                        "labels": [n["name"] for n in p["labels"]["nodes"]],
                        "modules": modules_of(p),
                        "featurey": featurey(p),
                        "comment_count": p["comments"]["totalCount"],
                        "review_count": p["reviews"]["totalCount"],
                        "linked_issues": [
                            {"number": c["number"], "title": c["title"], "url": c["url"]}
                            for c in p["closingIssuesReferences"]["nodes"]
                        ],
                        "already_briefed": p["number"] in briefed,
                    }
                    for p in pool
                ],
                indent=1,
            )
        )
        return 0

    print_table(pool, briefed)
    suffix = "" if args.include_briefed else f", {len(briefed & {p['number'] for p in worklist})} already briefed and skipped"
    print(f"\n{len(pool)} PRs shown (mode '{args.mode}', worklist {len(worklist)}{suffix})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
