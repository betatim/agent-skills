"""Clustering of failed jobs into carry-over clusters."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from last_nightly import cluster_failed_jobs, select_nightly  # noqa: E402


def test_failed_job_without_recognised_error_still_clusters():
    jobs = [{"name": "conda-cpp-tests / x", "failures": [], "job_error": None}]
    [c] = cluster_failed_jobs(jobs)
    assert c["jobs"] == ["conda-cpp-tests / x"]
    assert c["signature"].startswith("job | ")
    assert c["infra"] is False


def test_job_signature_ignores_paths():
    line = ("error    libmamba Unexpected error while linking package 'libnvforest': filesystem error: "
            "cannot copy: File exists [/opt/conda/pkgs/{0}/a.pyc] [/opt/conda/envs/test/a.pyc]")
    jobs = [{"name": n, "failures": [], "job_error": {"infra": False, "lines": [line.format(n)]}} for n in ("j1", "j2")]
    a, b = cluster_failed_jobs(jobs)
    assert a["signature"] == b["signature"] == (
        "job | error libmamba Unexpected error while linking package 'libnvforest': filesystem error: "
        "cannot copy: File exists")


def test_build_job_clusters_by_error_files():
    errors = [{"file": "cpp/src/a.cu", "error": "a.cu(1): error: x"}, {"file": "cpp/src/b.cuh", "error": "e"}]
    jobs = [{"name": n, "workflow": "build", "failures": [], "build_errors": errors, "job_error": None}
            for n in ("cpp-build / 1", "cpp-build / 2")]
    [c] = cluster_failed_jobs(jobs)
    assert c["signature"] == "build: cpp/src/a.cu"
    assert c["tests"] == ["build::cpp/src/a.cu", "build::cpp/src/b.cuh"]
    assert c["jobs"] == ["cpp-build / 1", "cpp-build / 2"] and c["workflow"] == "build"


def run(day, hour, status="completed", conclusion="success", branch="main"):
    return {"databaseId": int(day.replace("-", "") + hour), "headBranch": branch, "headSha": "s", "status": status,
            "conclusion": conclusion, "createdAt": f"{day}T{hour}:00:00Z"}


def test_select_nightly():
    builds = [run("2026-09-14", "06", conclusion="failure"), run("2026-09-12", "06"), run("2026-09-15", "06",
              status="in_progress")]
    tests = [run("2026-09-12", "07", conclusion="failure")]
    # 09-15 is still building; 09-14's build failed, so its test part is the latest earlier test run.
    build, test = select_nightly(builds, tests, "main")
    assert build["createdAt"][:10] == "2026-09-14" and test["createdAt"][:10] == "2026-09-12"
    # Scoring a date never falls back to an older test run.
    assert select_nightly(builds, tests, "main", date="2026-09-14") == (builds[0], None)
    # A build that passed but whose test run hasn't finished is not complete yet.
    build, test = select_nightly([run("2026-09-16", "06"), *builds], tests, "main", before="2026-09-16T08:00:00Z")
    assert build["createdAt"][:10] == "2026-09-14"
