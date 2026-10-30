#!/usr/bin/env python
"""Summarise the latest completed cuML nightly per branch.

A nightly is the day's first ``build.yaml`` dispatch plus that day's ``test.yaml``
run. ``test.yaml`` only runs when the build succeeded, so a nightly is complete
when its build failed or its test run completed. When the build failed, the
test part comes from the latest earlier test run (``test_created_at`` shows
it is older), so its failures still carry over and upstream bases still point
at what was last tested. With ``--date`` there is no such fallback.

Writes a baseline JSON: run metadata, every job's conclusion (tagged with its
workflow), failed tests and build errors clustered by (suite, error signature),
and per-job resolved package versions for failed jobs. ``--items``
additionally writes carry-over ledger items. Jobs and conclusions come from
attempt 1, not from later reruns.

Usage:
    last_nightly.py --branch main --branch release/26.10 --out baseline.json
    last_nightly.py --branch auto --out baseline.json --items items.json
    last_nightly.py --branch auto --date 2026-10-28 --out scored.json
    last_nightly.py --run-id 38215532469 --out baseline.json    # test runs only, no build
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from nightly_logs import parse_job_log, test_key

REPO = "NVIDIA/cuml"
# gh lives in base miniforge, not in the cuml envs.
GH = os.environ.get("GH", str(Path.home() / "miniforge3/bin/gh"))
CACHE = Path(os.environ.get("PREDICT_NIGHTLY_CACHE", Path.home() / ".cache/predict-nightly"))
# Bracketed paths in job errors embed build strings, so they would make each night's signature unique.
BRACKET_PATH = re.compile(r"\s*\[/[^\]]*\]")


def gh(*args):
    out = subprocess.run([GH, *args], check=True, capture_output=True, text=True)
    return out.stdout


def gh_json(*args):
    return json.loads(gh(*args))


def nightly_runs(workflow, limit=100):
    return gh_json(
        "run", "list", "--repo", REPO, "--workflow", workflow,
        "--event", "workflow_dispatch", "--limit", str(limit),
        "--json", "databaseId,headBranch,headSha,status,conclusion,createdAt",
    )


def active_branches(runs):
    """main plus the newest release branch, if it had a nightly in the last 3 days."""
    newest = max(r["createdAt"] for r in runs)
    cutoff = (datetime.fromisoformat(newest) - timedelta(days=3)).isoformat()
    releases = sorted(
        {r["headBranch"] for r in runs if r["headBranch"].startswith("release/") and r["createdAt"] >= cutoff}
    )
    return ["main"] + releases[-1:]


def first_on(runs, branch, date, completed=False):
    """The day's first run: the nightly dispatch, not a later manual one."""
    day = [r for r in runs if r["headBranch"] == branch and r["createdAt"][:10] == date
           and (not completed or r["status"] == "completed")]
    return min(day, key=lambda r: r["createdAt"]) if day else None


def select_nightly(builds, tests, branch, before=None, date=None):
    """(build run, test run) of the nightly on ``date``, or of the latest complete nightly."""
    builds = [r for r in builds if before is None or r["createdAt"] < before]
    tests = [r for r in tests if before is None or r["createdAt"] < before]
    if date:
        return first_on(builds, branch, date), first_on(tests, branch, date, completed=True)
    dates = sorted({r["createdAt"][:10] for r in builds + tests if r["headBranch"] == branch}, reverse=True)
    for d in dates:
        build, test = first_on(builds, branch, d), first_on(tests, branch, d, completed=True)
        if build and build["status"] != "completed":
            continue
        if build and build["conclusion"] == "failure" and test is None:
            older = [r for r in tests if r["headBranch"] == branch and r["status"] == "completed"
                     and r["createdAt"] < build["createdAt"]]
            return build, max(older, key=lambda r: r["createdAt"]) if older else None
        if test:
            return build, test
    return None, None


def job_log(job_id):
    path = CACHE / "logs" / f"{job_id}.log"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(gh("api", f"repos/{REPO}/actions/jobs/{job_id}/logs"))
    return path.read_text(errors="replace")


def run_jobs(run_id):
    # Attempt 1 is the nightly as scheduled; manual reruns would hide its failures.
    # `gh run view --json jobs` uses per_page=100, which returns HTTP 502 for some runs.
    out = gh("api", "--paginate", f"repos/{REPO}/actions/runs/{run_id}/attempts/1/jobs?per_page=50",
             "--jq", ".jobs[] | {databaseId: .id, name, conclusion}")
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def attempt1_conclusion(run_id):
    return gh("api", f"repos/{REPO}/actions/runs/{run_id}/attempts/1", "--jq", ".conclusion").strip()


def cluster_failed_jobs(failed_jobs):
    """Cluster failed tests by (suite, signature); a failed job without test failures is its own cluster.

    A build job with compiler errors clusters by its alphabetically first error file
    (``build: <file>``); its ``tests`` are ``build::<file>`` for every file with an error.
    """
    clusters = {}
    for job in failed_jobs:
        workflow = job.get("workflow", "test")
        if workflow == "build" and job["build_errors"]:
            errors = job["build_errors"]
            key = ("build", f"build: {errors[0]['file']}")
            c = clusters.setdefault(
                key,
                {"suite": "build", "root": "build", "signature": key[1], "message_example": errors[0]["error"],
                 "tests": set(), "jobs": set(), "workflow": "build"},
            )
            c["tests"].update(f"build::{e['file']}" for e in errors)
            c["jobs"].add(job["name"])
            continue
        for f in job["failures"]:
            key = (f["suite"], f["signature"])
            c = clusters.setdefault(
                key,
                {"suite": f["suite"], "root": f["root"], "signature": f["signature"],
                 "message_example": f["message"], "tests": set(), "jobs": set(), "workflow": workflow},
            )
            c["tests"].add(test_key(f))
            c["jobs"].add(job["name"])
        if not job["failures"]:
            # Even with no recognised error line the failure must surface, or the job silently vanishes.
            error = job["job_error"] or {"infra": False, "lines": ["no recognised error line; read the job log"]}
            key = ("<job>", job["name"])
            clusters[key] = {
                "suite": None, "root": None,
                "signature": ("infra | " if error["infra"] else "job | ")
                             + " ".join(BRACKET_PATH.sub("", error["lines"][0]).split())[:160],
                "message_example": "\n".join(error["lines"]),
                "tests": set(), "jobs": {job["name"]}, "infra": error["infra"], "workflow": workflow,
            }
    return [
        {**c, "tests": sorted(c["tests"]), "jobs": sorted(c["jobs"])} for c in clusters.values()
    ]


def summarise_jobs(run, workflow):
    """(jobs, failed_jobs, other_jobs) of a run's attempt 1, each job tagged with ``workflow``."""
    jobs = run_jobs(run["databaseId"])
    # Logs needed even when the job passes: the nightly-deps job records which sklearn
    # nightly wheel was installed, and one conda job records the RAPIDS build strings
    # (which embed each library's source SHA).
    reference = {"nightly-dependencies"}
    conda_ref = next((j["name"] for j in jobs if j["conclusion"] == "success"
                      and j["name"].startswith("conda-python-tests-singlegpu") and "amd64" in j["name"]), None)
    failed_jobs, other_jobs = [], []
    for job in jobs:
        if job["conclusion"] == "failure":
            target = failed_jobs
        elif workflow == "test" and job["conclusion"] == "success" and (
                any(r in job["name"] for r in reference) or job["name"] == conda_ref):
            target = other_jobs
        else:
            continue
        parsed = parse_job_log(job_log(job["databaseId"]))
        target.append({"id": job["databaseId"], "name": job["name"], "workflow": workflow, **parsed})
    jobs = [{"id": j["databaseId"], "name": j["name"], "conclusion": j["conclusion"], "workflow": workflow}
            for j in jobs]
    return jobs, failed_jobs, other_jobs


def summarise_nightly(build, test):
    """Baseline entry for a nightly. ``created_at`` is when the nightly started (the build dispatch)."""
    jobs, failed_jobs, other_jobs = [], [], []
    build_conclusion = test_conclusion = None
    if build:
        build_conclusion = attempt1_conclusion(build["databaseId"])
        b_jobs, b_failed, _ = summarise_jobs(build, "build")
        jobs += b_jobs
        failed_jobs += b_failed
    if test:
        test_conclusion = attempt1_conclusion(test["databaseId"])
        t_jobs, t_failed, other_jobs = summarise_jobs(test, "test")
        jobs += t_jobs
        failed_jobs += t_failed
    start = build or test
    url = f"https://github.com/{REPO}/actions/runs/"
    return {
        "branch": start["headBranch"],
        "run_id": test["databaseId"] if test else None,
        "build_run_id": build["databaseId"] if build else None,
        "sha": start["headSha"],
        "created_at": start["createdAt"],
        "test_created_at": test["createdAt"] if test else None,
        "conclusion": "failure" if "failure" in (build_conclusion, test_conclusion)
                      else test_conclusion or build_conclusion,
        "build_conclusion": build_conclusion,
        "test_conclusion": test_conclusion,
        "url": url + str((test or build)["databaseId"]),
        "build_url": url + str(build["databaseId"]) if build else None,
        "jobs": jobs,
        "failed_jobs": failed_jobs,
        "other_jobs": other_jobs,
        "clusters": cluster_failed_jobs(failed_jobs),
    }


def carry_over_items(baseline):
    items = []
    for c in baseline["clusters"]:
        digest = hashlib.sha1(f"{c['suite']}|{c['signature']}".encode()).hexdigest()[:8]
        items.append({
            "id": f"co-{baseline['branch'].replace('/', '-')}-{digest}",
            "source": "carry-over",
            "branch": baseline["branch"],
            "ref": f"{c['workflow']} run {baseline['build_run_id'] if c['workflow'] == 'build' else baseline['run_id']}"
                   f" @ {baseline['sha'][:9]}",
            "title": f"[{c['suite'] or 'job'}] {c['signature']}",
            "jobs_affected": c["jobs"],
            "tests": c["tests"],
            "signature": c["signature"],
            "detail": {"message_example": c["message_example"][:2000], "n_tests": len(c["tests"]),
                       "infra": c.get("infra", False)},
        })
    return items


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--branch", action="append", help="branch name, or 'auto' for main + active release")
    ap.add_argument("--run-id", type=int, action="append", help="summarise these runs instead")
    ap.add_argument("--before", help="only consider runs created before this ISO timestamp")
    ap.add_argument("--date", help="only consider runs created on this UTC date (YYYY-MM-DD)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--items", help="also write carry-over ledger items here")
    args = ap.parse_args()

    tests = nightly_runs("test.yaml")
    if args.run_id:
        selected = [(None, r) for r in tests if r["databaseId"] in args.run_id]
        missing = set(args.run_id) - {r["databaseId"] for _, r in selected}
        if missing:
            sys.exit(f"runs not among the latest nightlies: {sorted(missing)}")
    else:
        builds = nightly_runs("build.yaml")
        branches = args.branch or ["auto"]
        if "auto" in branches:
            branches = active_branches(tests)
        selected = []
        for b in branches:
            build, test = select_nightly(builds, tests, b, args.before, args.date)
            if build is None and test is None:
                print(f"warning: no completed nightly for {b}" + (f" on {args.date}" if args.date else ""),
                      file=sys.stderr)
                continue
            selected.append((build, test))
            if not (args.before or args.date):
                start = (build or test)["createdAt"]
                for r in builds:
                    if r["headBranch"] == b and r["createdAt"] > start:
                        print(f"warning: {b}: a newer nightly started at {r['createdAt']} (build.yaml run "
                              f"{r['databaseId']}, {r['status']}); its outcome is no longer a prediction",
                              file=sys.stderr)
                        break

    baselines = [summarise_nightly(build, test) for build, test in selected]
    Path(args.out).write_text(json.dumps(baselines, indent=1))
    for b in baselines:
        n_fail = sum(1 for j in b["jobs"] if j["conclusion"] == "failure")
        stale = b["test_created_at"] and b["test_created_at"][:10] != b["created_at"][:10]
        print(f"{b['branch']}: nightly {b['created_at']} ({b['conclusion']}): build run {b['build_run_id']} "
              f"({b['build_conclusion']}), test run {b['run_id']} ({b['test_conclusion']}"
              + (f", from {b['test_created_at'][:10]}: build failed, no test run" if stale else "")
              + f"), {n_fail} failed jobs, {len(b['clusters'])} clusters")
        for c in b["clusters"]:
            print(f"   {len(c['tests']):4d} tests  {len(c['jobs'])} jobs  [{c['suite']}] {c['signature']}")
    if args.items:
        items = [i for b in baselines for i in carry_over_items(b)]
        Path(args.items).write_text(json.dumps(items, indent=1))
        print(f"wrote {len(items)} carry-over items to {args.items}")


if __name__ == "__main__":
    main()
