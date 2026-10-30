#!/usr/bin/env python
"""What changed since a baseline nightly that could affect its failures.

For each branch in the baseline:
  * cuML commits between the run's SHA and the branch head, with their files
    and PR numbers;
  * for each failure cluster, the commits that touch its test files or
    mention its tests, and any implicated package (named in the error
    message or passed with --pkg) whose newest release differs from the
    version installed in the failing job.

Usage:
  changed_since.py baseline.json [--pkg xgboost] [--out changes.json]
"""

import argparse
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

REPO = "NVIDIA/cuml"
GH = os.environ.get("GH", str(Path.home() / "miniforge3/bin/gh"))
CUDA_SUFFIX = re.compile(r"-cu\d+$|-cuda\d+x$")
PR_NUMBER = re.compile(r"\(#(\d+)\)")


def gh_json(*args):
    out = subprocess.run([GH, *args], check=True, capture_output=True, text=True)
    return json.loads(out.stdout)


def commits_since(sha, branch):
    compare = gh_json("api", f"repos/{REPO}/compare/{sha}...{branch}")
    commits = []
    for c in compare["commits"]:
        detail = gh_json("api", f"repos/{REPO}/commits/{c['sha']}")
        title = c["commit"]["message"].splitlines()[0]
        pr = PR_NUMBER.search(title)
        commits.append({
            "sha": c["sha"],
            "title": title,
            "message": c["commit"]["message"],
            "pr": int(pr.group(1)) if pr else None,
            "date": c["commit"]["committer"]["date"],
            "files": [f["filename"] for f in detail.get("files", [])],
        })
    return commits


def fetch_json(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError):
        return None


def latest_release(name, channel):
    if channel == "pypi":
        data = fetch_json(f"https://pypi.org/pypi/{name}/json")
        return data and data["info"]["version"]
    if channel == "conda-forge":
        data = fetch_json(f"https://api.anaconda.org/package/conda-forge/{name}")
        return data and data.get("latest_version")
    return None  # RAPIDS nightlies change daily; handled by the dependency diff


def implicated_packages(cluster, versions, extra):
    text = cluster["message_example"].lower()
    names = set()
    for name in versions:
        base = CUDA_SUFFIX.sub("", name).replace("-", "_")
        if name in extra or base in extra or re.search(rf"\b{re.escape(base)}\b", text):
            names.add(name)
    return sorted(names)


def related_commits(cluster, commits):
    test_files = {t.split("::")[0] + "/" + t.split("::")[1] for t in cluster["tests"] if "::" in t}
    test_names = {re.sub(r"\[.*$", "", t.split("::")[-1]) for t in cluster["tests"]}
    related = []
    for c in commits:
        why = []
        if test_files & set(c["files"]):
            why.append("touches failing test file")
        mentioned = [n for n in test_names if n and n in c["message"]]
        if mentioned:
            why.append(f"mentions {mentioned[:3]}")
        if why:
            related.append({"sha": c["sha"][:9], "pr": c["pr"], "title": c["title"], "why": why})
    return related


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("baseline")
    ap.add_argument("--pkg", action="append", default=[], help="also check this package for newer releases")
    ap.add_argument("--out")
    args = ap.parse_args()

    report = []
    for base in json.loads(Path(args.baseline).read_text()):
        commits = commits_since(base["sha"], base["branch"])
        print(f"== {base['branch']}: {len(commits)} cuML commits since {base['sha'][:9]} (run {base['run_id']})")
        for c in commits:
            print(f"   {c['sha'][:9]} {c['date'][:10]} {c['title'][:100]}")
        jobs_by_name = {j["name"]: j for j in base["failed_jobs"]}
        clusters = []
        for cluster in base["clusters"]:
            versions = {}
            for job_name in cluster["jobs"]:
                versions.update(jobs_by_name[job_name]["versions"])
            pkgs = []
            for name in implicated_packages(cluster, versions, set(args.pkg)):
                installed = versions[name]
                latest = latest_release(name, installed["channel"])
                pkgs.append({"name": name, "installed": installed["version"], "channel": installed["channel"],
                             "latest": latest, "newer_available": bool(latest) and latest != installed["version"]})
            related = related_commits(cluster, commits)
            clusters.append({"suite": cluster["suite"], "signature": cluster["signature"],
                             "related_commits": related, "packages": pkgs})
            print(f"   -- [{cluster['suite']}] {cluster['signature'][:90]}")
            for r in related:
                print(f"        commit {r['sha']} (#{r['pr']}) {r['title'][:70]}: {', '.join(r['why'])}")
            for p in pkgs:
                flag = "NEWER" if p["newer_available"] else "same"
                print(f"        pkg {p['name']} installed {p['installed']} latest {p['latest']} [{flag}]")
            if not related and not any(p["newer_available"] for p in pkgs):
                print("        nothing changed -> expect repeat")
        report.append({"branch": base["branch"], "run_id": base["run_id"], "commits": commits, "clusters": clusters})
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
