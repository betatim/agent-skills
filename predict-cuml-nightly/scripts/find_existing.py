#!/usr/bin/env python
"""Find existing issues/PRs matching regexes (title + body), case-insensitive.

`gh search` and `gh issue list --search` return nothing for NVIDIA/cuml, so
this lists recent issues/PRs and greps them locally.

Usage:
  find_existing.py 'test_logistic_regression' 'alpha.*\\bC\\b' [--repo NVIDIA/cuml] [--state all]
"""

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

GH = os.environ.get("GH") or shutil.which("gh") or "gh"


def listing(kind, repo, state, limit):
    out = subprocess.run(
        [GH, kind, "list", "--repo", repo, "--state", state, "--limit", str(limit),
         "--json", "number,title,body,url,state"],
        check=True, capture_output=True, text=True,
    )
    return json.loads(out.stdout)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("patterns", nargs="+")
    ap.add_argument("--repo", default="NVIDIA/cuml")
    ap.add_argument("--state", default="open", choices=["open", "closed", "all"])
    ap.add_argument("--limit", type=int, default=300)
    args = ap.parse_args()
    regexes = [re.compile(p, re.IGNORECASE) for p in args.patterns]
    for kind in ("issue", "pr"):
        for x in listing(kind, args.repo, args.state, args.limit):
            text = f"{x['title']}\n{x['body'] or ''}"
            hits = [r.pattern for r in regexes if r.search(text)]
            if hits:
                print(f"{kind:5} #{x['number']} [{x['state']}] {x['title'][:90]}  {x['url']}  matched {hits}")


if __name__ == "__main__":
    main()
