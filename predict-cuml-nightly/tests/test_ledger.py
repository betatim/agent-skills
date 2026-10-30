"""Ledger gate and scorer on synthetic baselines."""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "ledger.py"


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True)


def baseline(run_id, created, clusters, jobs=("job-a",), build_run_id=None, build_conclusion="success"):
    return {"branch": "main", "run_id": run_id, "build_run_id": build_run_id or run_id + 1000, "sha": "abc",
            "created_at": created, "url": f"https://x/{run_id}", "clusters": clusters, "conclusion": "failure",
            "build_conclusion": build_conclusion, "jobs": [{"name": j, "conclusion": "failure"} for j in jobs]}


def test_check_and_score(tmp_path):
    base = tmp_path / "base.json"
    base.write_text(json.dumps([baseline(1, "2026-01-01T07:00:00Z", [])]))
    ledger = tmp_path / "l.json"
    assert run("init", ledger, "--baseline", base).returncode == 0
    items = [
        {"id": "a", "source": "sklearn", "branch": "*", "title": "deprecates C", "jobs_affected": [],
         "tests": [], "signature": None},
        {"id": "b", "source": "carry-over", "branch": "main", "title": "flake", "jobs_affected": ["job-a"],
         "tests": ["r::t.py::flaky[1]"], "signature": "s1"},
    ]
    (tmp_path / "items.json").write_text(json.dumps(items))
    run("add", ledger, tmp_path / "items.json")
    assert run("check", ledger).returncode == 1  # no verdicts yet

    # A predicted breakage without tests or signature cannot be scored, so check rejects it.
    run("set", ledger, "a", "--verdict", "breaks", "--evidence", "e", "--action", '{"draft_issue": "d.md"}')
    assert "cannot be scored" in run("check", ledger).stdout
    disp = [{"id": "a", "verdict": "breaks", "evidence": "e", "tests": ["r::t.py::test_lr"],
             "action": {"draft_issue": "d.md"}},
            {"id": "b", "verdict": "fixed", "evidence": "e"}]
    (tmp_path / "d.json").write_text(json.dumps(disp))
    run("apply", ledger, tmp_path / "d.json")
    assert run("check", ledger).returncode == 0

    nxt = tmp_path / "next.json"
    # score only accepts a run from the ledger's own UTC date, after it was written.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT23:59:59Z")
    nxt.write_text(json.dumps([baseline(2, today, [
        {"suite": "s", "signature": "x", "tests": ["r::t.py::test_lr[a]", "r::t.py::test_lr[b]"], "jobs": ["job-a"]},
        {"suite": "s", "signature": "y", "tests": ["r::u.py::other"], "jobs": ["job-a"]},
    ])]))
    out = run("score", ledger, nxt)
    assert out.returncode == 0, out.stderr
    summary = json.loads(ledger.read_text())["score"]["summary"]
    assert summary["hit"] == 1          # parameterless prediction matched both parametrizations
    assert summary["miss"] == 1         # the unpredicted "y" cluster
    assert summary["false_alarm"] == 0  # "b" judged fixed and did not fail


def test_score_rejects_same_run(tmp_path):
    base = tmp_path / "base.json"
    base.write_text(json.dumps([baseline(1, "2026-01-01T07:00:00Z", [])]))
    ledger = tmp_path / "l.json"
    run("init", ledger, "--baseline", base)
    assert run("score", ledger, base).returncode != 0


def test_score_build_failure_without_test_run(tmp_path):
    base = tmp_path / "base.json"
    base.write_text(json.dumps([baseline(1, "2026-01-01T07:00:00Z", [])]))
    ledger = tmp_path / "l.json"
    run("init", ledger, "--baseline", base)
    items = [{"id": i, "source": s, "branch": "main", "title": i, "jobs_affected": [], "tests": t, "signature": sig}
             for i, s, t, sig in [("cmake", "rapids-cmake", [], "build"),
                                  ("hdr", "rmm", ["build::cpp/src/umap/algo.cuh"], None),
                                  ("skl", "scikit-learn", ["r::t.py::test_x"], None)]]
    (tmp_path / "items.json").write_text(json.dumps(items))
    run("add", ledger, tmp_path / "items.json")
    disp = [{"id": i, "verdict": "breaks", "evidence": "e", "action": {"draft_issue": "d.md"}}
            for i in ("cmake", "hdr", "skl")]
    (tmp_path / "d.json").write_text(json.dumps(disp))
    run("apply", ledger, tmp_path / "d.json")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT23:59:59Z")
    nxt = baseline(None, today, [
        {"suite": "build", "signature": "build: cpp/src/solver/cd.cuh", "workflow": "build",
         "tests": ["build::cpp/src/solver/cd.cuh", "build::cpp/src/umap/algo.cuh"], "jobs": ["cpp-build / x"]},
    ], jobs=("cpp-build / x",), build_run_id=7, build_conclusion="failure")
    (tmp_path / "next.json").write_text(json.dumps([nxt]))
    out = run("score", ledger, tmp_path / "next.json")
    assert out.returncode == 0, out.stderr
    outcomes = {i["id"]: i.get("outcome") for i in json.loads(ledger.read_text())["items"]}
    # "build" matches any build failure; a file prediction matches a cluster listing that file.
    assert outcomes == {"cmake": "hit", "hdr": "hit", "skl": "unobserved"}  # no test.yaml run
