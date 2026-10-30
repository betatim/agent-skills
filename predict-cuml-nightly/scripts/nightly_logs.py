"""Parse GitHub Actions job logs from cuML nightly test and build jobs.

Extracts failed tests (pytest short summary and gtest), the suite each
belongs to, resolved package versions (conda solve and pip installs),
compiler errors (build jobs), and a job-level error when a job failed
without any test failures.
"""

import posixpath
import re

TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ?")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
BOX_TITLE = re.compile(r"^\|\s+(.*?)\s+\|$")
PYTEST_RESULT = re.compile(r"^(FAILED|ERROR) (.+?)(?: - (.*))?$")
PYTEST_FINAL = re.compile(r"^=+ (.*\b(?:passed|failed|error|errors)\b.*) in [\d.]+s")
GTEST_FAILED = re.compile(r"^\[  FAILED  \] ([\w/.]+)")
CONDA_PKG = re.compile(
    r"^\s+\+ (\S+)\s+(\d\S*)\s+(\S+)\s+"
    r"(conda-forge|rapidsai-nightly|rapidsai|nvidia|pytorch|\S+/label/\S+)\b"
)
PIP_INSTALLED = re.compile(r"^Successfully installed (.*)$")
WHEEL_DOWNLOAD = re.compile(r"^\s*Downloading (\S+\.whl)\b")

# Suite title (from `rapids-logger`) -> repo-relative directory pytest runs in.
SUITE_ROOTS = {
    "pytest cuml single GPU": "python/cuml/tests",
    "pytest cuml accelerator": "python/cuml/cuml_accel_tests",
    "pytest cuml-dask (No UCXX)": "python/cuml/tests/dask",
    "pytest cuml-dask (UCXX only)": "python/cuml/tests/dask",
    "pytest cuml dask": "python/cuml/tests/dask",
    "Running scikit-learn tests with cuML acceleration": "python/cuml/cuml_accel_tests/upstream/scikit-learn",
    "UMAP test suite": "python/cuml/cuml_accel_tests/upstream/umap",
    "Run gtests": "cpp",
    "Testing libcuml linkage": "python/libcuml/tests",
}

# Signs that a job died for infrastructure reasons, not because of a test.
INFRA_PATTERNS = [
    "ReadTimeoutError",
    "WARNING: Retrying",
    "ResolutionImpossible",
    "No matching distribution found",
    "Could not solve for environment specs",
    "does not exist (perhaps a missing channel)",
    "The runner has received a shutdown signal",
    "lost communication with the server",
]
JOB_ERROR_PATTERNS = [
    *INFRA_PATTERNS,
    "timeout: sending signal",
    "Segmentation fault",
    "Fatal Python error",
    "core dumped",
    "Unexpected error while linking package",
    "CMake Error",
    "Error compiling Cython file",
    "warnings treated as errors",
    "Failed to build",
    "Cannot repair wheel",
    "Process completed with exit code",
]
# nvcc: ``path(177): error: ...``; gcc/clang: ``path:163:6: error: ...``
COMPILER_ERROR = re.compile(r"^(\S+\.\w+)(?:\(\d+\)|:\d+(?::\d+)?):\s+(?:fatal\s+)?error\b")
REPO_PREFIX = re.compile(r"^(?:\$SRC_DIR|/__w/cuml/cuml|/home/runner/_work/cuml/cuml)/")

NUMBER = re.compile(r"\d+")
BRACKET_TIME = re.compile(r"\[\d\d:\d\d:\d\d\]")
SRC_LOCATION = re.compile(r"\S+\.(?:cu|cuh|cpp|hpp|h|py):\s*\d+:?")


def suite_root(suite):
    """Repo-relative directory a suite's pytest ran in (docker_run.sh banners are ``targeted <root>``)."""
    if suite and suite.startswith("targeted "):
        return suite.removeprefix("targeted ")
    return SUITE_ROOTS.get(suite)


def clean_lines(text):
    """Yield ``(timestamp, line)`` with the GitHub timestamp prefix and ANSI codes removed."""
    for raw in text.splitlines():
        ts = TIMESTAMP.match(raw)
        line = ANSI.sub("", TIMESTAMP.sub("", raw)).rstrip()
        yield (ts.group(0).strip() if ts else None), line


def signature(message, nodeid=""):
    """Normalise an error message so the same root cause clusters together.

    Uses the exception type plus the last ``: ``-separated segment, which is
    where the most specific part of chained messages (CUDA errors, wrapped
    exceptions) ends up, truncated because pytest truncates to terminal width.
    """
    test_file = nodeid.split("::")[0]
    if not message:
        return f"<no message> | {test_file}"
    # pytest marks a message truncated to terminal width with a trailing "..."
    msg = SRC_LOCATION.sub("", BRACKET_TIME.sub("", message)).removesuffix("...")
    exc_type = ""
    m = re.match(r"^([\w.]+(?:Error|Exception|Warning|Exit|Interrupt)):\s*(.*)$", msg)
    if m:
        exc_type, msg = m.group(1), m.group(2)
    last = msg.split(": ")[-1].strip() or msg.strip()
    last = NUMBER.sub("N", last)[:80]
    if not last.strip(" :"):
        # pytest truncated a multi-line message; the test file is the next best key.
        last = test_file
    return f"{exc_type} | {last}" if exc_type else last


def build_file(path):
    """Stable name for a file in a compiler error.

    cuML files become repo-relative (``cpp/src/...``), CPM-fetched sources become
    ``<dep>:<path>``, and installed dependency headers become ``include/<path>``.
    """
    m = re.search(r"/_deps/([\w.+-]+?)-src/(.+)$", path)
    if m:
        return f"{m.group(1)}:{posixpath.normpath(m.group(2))}"
    rel = REPO_PREFIX.sub("", path)
    if rel.startswith(("/", "$")):
        m = re.search(r"/include/(.+)$", rel)
        return f"include/{posixpath.normpath(m.group(1))}" if m else rel
    return posixpath.normpath(rel)


def parse_job_log(text):
    """Return dict with ``failures``, ``summaries``, ``versions``, ``downloads``, ``build_errors``, ``job_error``.

    ``downloads`` maps each downloaded wheel filename to the time it was fetched,
    which pins down which nightly wheel (e.g. scikit-learn) a job installed.
    ``build_errors`` lists each file with a compiler error and its first error line, sorted by file.
    """
    suite = None
    in_summary = False
    failures = []
    gtest_failed = set()
    summaries = []
    versions = {}
    error_lines = []
    infra = False
    downloads = {}
    build_errors = {}

    for ts, line in clean_lines(text):
        box = BOX_TITLE.match(line)
        if box:
            suite = box.group(1)
            continue
        if "short test summary info" in line:
            in_summary = True
            continue
        final = PYTEST_FINAL.match(line)
        if final:
            in_summary = False
            summaries.append({"suite": suite, "summary": final.group(1)})
            continue
        if in_summary:
            m = PYTEST_RESULT.match(line)
            if m:
                kind, nodeid, message = m.groups()
                failures.append(
                    {
                        "kind": kind,
                        "suite": suite,
                        "root": suite_root(suite),
                        "nodeid": nodeid,
                        "message": message or "",
                        "signature": signature(message or "", nodeid),
                    }
                )
            continue
        g = GTEST_FAILED.match(line)
        if g:
            gtest_failed.add(g.group(1))
            continue
        c = CONDA_PKG.match(line)
        if c:
            name, version, build, channel = c.groups()
            versions[name] = {"version": version, "build": build, "channel": channel}
            continue
        w = WHEEL_DOWNLOAD.match(line)
        if w:
            downloads.setdefault(w.group(1), ts)
            continue
        p = PIP_INSTALLED.match(line)
        if p:
            for spec in p.group(1).split():
                name, _, version = spec.rpartition("-")
                if name:
                    versions[name] = {"version": version, "build": None, "channel": "pypi"}
            continue
        # conda builds (rattler-build) prefix their output with box-drawing characters.
        e = COMPILER_ERROR.match(line.lstrip("│ \t"))
        if e:
            build_errors.setdefault(build_file(e.group(1)), line.lstrip("│ \t")[:300])
            continue
        if any(pat in line for pat in JOB_ERROR_PATTERNS):
            error_lines.append(line.strip())
            infra = infra or any(pat in line for pat in INFRA_PATTERNS)

    for name in sorted(gtest_failed):
        failures.append(
            {
                "kind": "GTEST",
                "suite": "Run gtests",
                "root": "cpp",
                "nodeid": name,
                "message": "",
                "signature": f"gtest | {name.split('.')[0]}",
            }
        )

    job_error = None
    if not failures and error_lines:
        # Keep the first few distinct lines; the first one is usually the cause.
        seen = []
        for line in error_lines:
            if line not in seen:
                seen.append(line)
        job_error = {"infra": infra, "lines": seen[:8]}

    return {
        "failures": failures,
        "summaries": summaries,
        "versions": versions,
        "downloads": downloads,
        "build_errors": [{"file": f, "error": build_errors[f]} for f in sorted(build_errors)],
        "job_error": job_error,
    }


def test_key(failure):
    """Key identifying a test across jobs: ``<root>::<nodeid>``."""
    return f"{failure['root'] or failure['suite']}::{failure['nodeid']}"


if __name__ == "__main__":
    import json
    import sys

    result = parse_job_log(open(sys.argv[1], errors="replace").read())
    print(json.dumps({"summaries": result["summaries"], "job_error": result["job_error"],
                      "failures": [{"test": test_key(f), "signature": f["signature"]} for f in result["failures"]]},
                     indent=1))
