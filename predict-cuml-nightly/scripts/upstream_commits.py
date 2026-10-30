#!/usr/bin/env python
"""Upstream commit triage for /predict-cuml-nightly: what merged upstream since the last nightly.

The method is the same for every upstream; only step 1 differs:

1. Find the upstream commit the last cuML nightly used (the *base*).
   - scikit-learn: nightly wheels (``1.10.dev0``, no hash) are built by the scheduled
     "Publish sdist and wheels" run in scikit-learn/scikit-learn-release, whose sdist
     job logs the scikit-learn ``main`` SHA. The nightly-deps job used the wheel from
     the latest run that finished before it downloaded the wheel.
   - RAPIDS libraries: conda build strings embed the source SHA
     (``cuda13_261011054545_24b0b170``); read it from a conda job in the baseline run.
   - treelite: nightlies install *releases*, so the base is the release tag the
     baseline installed and the head is the newest release on conda-forge/PyPI
     (release tags live on release branches, not ``mainline``).
   - rapids-cmake: every RAPIDS build (libcuml included) fetches the branch head at
     configure time, and no build string records which commit. The base is the
     branch commit as of the earliest RAPIDS build time (``261011054545`` in the
     build string) among the baseline's packages.
2. List every commit on the upstream branch from the base to its current head.
3. Commits touching no source (docs, CI, tests, benchmarks) are auto-``safe``.
   The rest are annotated with their links to cuML, strongest first, and left for
   review (one subagent per upstream; see SKILL.md).

Usage:
  upstream_commits.py --upstream scikit-learn --baseline baseline.json --out items.json
  upstream_commits.py --upstream cuvs --baseline baseline.json --out items.json [--branch main]
  upstream_commits.py --upstream treelite --baseline baseline.json --out items.json
  upstream_commits.py --upstream rapids-cmake --baseline baseline.json --out items.json [--branch main]
  upstream_commits.py --upstream raft --base <sha> --head <sha> --out items.json   # explicit range
  upstream_commits.py --list
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

CACHE = Path.home() / ".cache/predict-cuml-nightly"
CUML_REPO = Path.home() / "git/cuml"
CUML = CUML_REPO / "python/cuml/cuml"
GH = os.environ.get("GH") or shutil.which("gh") or "gh"
PR_NUMBER = re.compile(r"\(#(\d+)\)")

SKLEARN_NON_SOURCE = re.compile(
    r"^(doc/|examples/|\.github/|build_tools/|maint_tools/|benchmarks/|asv_benchmarks/|\.[^/]+$|[^/]+\.(md|rst|txt|cfg|toml|lock)$)"
    r"|/tests/|^sklearn/conftest\.py$|^sklearn/externals/_packaging/"
)
SKLEARN_CORE = re.compile(r"^sklearn/(base\.py|_config\.py|exceptions\.py|utils/|callback/|_loss/|metrics/_scorer\.py)")
RAPIDS_NON_SOURCE = re.compile(
    r"^(docs?/|\.github/|ci/|notebooks/|java/|cpp/examples/|cpp/tests?/|cpp/bench(mark)?s?/|\.[^/]+$|[^/]+\.md$)"
    r"|/tests?/|/bench(mark)?s?/|\.md$"
)
RAPIDS_PACKAGING = re.compile(r"(^|/)(CMakeLists\.txt|cmake/|dependencies\.yaml|pyproject\.toml|conda/recipes/)")

# upstream -> GitHub repo, conda package whose build string carries the SHA, include dir, Python packages.
RAPIDS = {
    "rmm": ("rapidsai/rmm", "librmm", "rmm", ["rmm"]),
    "raft": ("rapidsai/raft", "libraft", "raft", ["pylibraft", "raft_dask"]),
    "cuvs": ("rapidsai/cuvs", "libcuvs", "cuvs", ["cuvs"]),
    "cudf": ("rapidsai/cudf", "libcudf", "cudf", ["cudf", "pylibcudf", "dask_cudf"]),
    "nvforest": ("rapidsai/nvforest", "libnvforest", "nvforest", ["nvforest"]),
    "kvikio": ("rapidsai/kvikio", "libkvikio", "kvikio", ["kvikio"]),
}
TREELITE_NON_SOURCE = re.compile(r"^(docs/|tests/|ops/|dev/|\.github/|\.[^/]+$|[^/]+\.md$)")
RAPIDS_CMAKE_NON_SOURCE = re.compile(r"^(docs/|testing/|ci/|examples/|\.github/|\.agents/|\.[^/]+$|[^/]+\.md$|dependencies\.yaml$)")
UPSTREAMS = ["scikit-learn", *RAPIDS, "treelite", "rapids-cmake"]


# --- git -------------------------------------------------------------------------

def git(clone, *args):
    return subprocess.run(["git", f"--git-dir={clone}", *args], check=True, capture_output=True, text=True).stdout


def ensure_clone(name, repo, branch):
    """Blobless bare clone; RAPIDS clones are shallow (90 days) since only recent history is needed.

    treelite is a full clone with all tags: its base is a release tag that may be older than 90 days.
    """
    clone = CACHE / f"{name}.git"
    since = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
    shallow = [] if name in ("scikit-learn", "treelite") else [f"--shallow-since={since}"]
    tags = ["+refs/tags/*:refs/tags/*"] if name == "treelite" else []
    if not clone.exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--bare", "--filter=blob:none", "--single-branch", "--branch", branch,
                        *shallow, f"https://github.com/{repo}", str(clone)], check=True)
    git(clone, "fetch", "--quiet", *shallow, "origin", f"+refs/heads/{branch}:refs/heads/{branch}", *tags)
    return clone


def gh_json(*args):
    out = subprocess.run([GH, *args], check=True, capture_output=True, text=True)
    return json.loads(out.stdout)


# --- step 1: base commit -----------------------------------------------------------

def all_jobs(baseline):
    return baseline["failed_jobs"] + baseline.get("other_jobs", [])


def sklearn_base(baseline):
    """sklearn commit of the nightly wheel the nightly-deps job installed."""
    times = [ts for job in all_jobs(baseline) for name, ts in job.get("downloads", {}).items()
             if name.startswith("scikit_learn-") and ".dev" in name]
    if not times:
        sys.exit(f"{baseline['branch']}: no sklearn dev wheel download in the baseline (nightly-deps job log missing?)")
    install_time = min(times)
    runs = gh_json("api", "repos/scikit-learn/scikit-learn-release/actions/workflows/wheels.yml/runs"
                   "?event=schedule&status=success&per_page=30")["workflow_runs"]
    run = next((r for r in runs if r["updated_at"] <= install_time), None)
    if run is None:
        sys.exit(f"no successful scikit-learn-release run finished before {install_time}")
    path = CACHE / "sklearn-release" / f"{run['id']}.sha"
    if not path.exists():
        jobs = gh_json("api", f"repos/scikit-learn/scikit-learn-release/actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
        job = next(j for j in jobs if j["name"] == "Source distribution")
        log = subprocess.run([GH, "api", f"repos/scikit-learn/scikit-learn-release/actions/jobs/{job['id']}/logs"],
                             check=True, capture_output=True, text=True).stdout
        # The second checkout is scikit-learn itself; its "Checking out the ref" group ends with the SHA.
        after = log.split("repository: scikit-learn/scikit-learn\n", 1)[1]
        sha = re.search(r"Checking out the ref.*?\b([0-9a-f]{40})\b", after, re.S).group(1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sha)
    return path.read_text().strip(), f"wheel installed {install_time} built by {run['html_url']}"


def rapids_base(baseline, package):
    """Short source SHA from the conda build string of ``package`` in the baseline's conda jobs."""
    for job in all_jobs(baseline):
        build = (job.get("versions", {}).get(package) or {}).get("build") or ""
        m = re.search(r"_([0-9a-f]{8})$", build)
        if m:
            return m.group(1), f"{package} {job['versions'][package]['version']} {build} in '{job['name']}'"
    sys.exit(f"{baseline['branch']}: no conda build string for {package} in the baseline")


RELEASE = re.compile(r"^\d+(\.\d+)+$")


def version_key(v):
    return tuple(int(p) for p in v.split("."))


def treelite_range(baseline):
    """(base tag, head tag): newest treelite the baseline installed -> newest release on conda-forge/PyPI."""
    installed = {(job.get("versions", {}).get("treelite") or {}).get("version") for job in all_jobs(baseline)}
    installed = [v for v in installed if v and RELEASE.match(v)]
    if not installed:
        sys.exit(f"{baseline['branch']}: no treelite version in the baseline")
    base = max(installed, key=version_key)

    def fetch(url):
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.load(r)

    released = {*fetch("https://api.anaconda.org/package/conda-forge/treelite")["versions"],
                *fetch("https://pypi.org/pypi/treelite/json")["releases"]}
    head = max((v for v in released if RELEASE.match(v)), key=version_key)
    return base, head, f"treelite {base} installed by the baseline, newest release {head}"


def rapids_cmake_base(baseline, clone, branch):
    """Branch commit as of the earliest RAPIDS nightly package build in the baseline."""
    stamps = {}
    for job in all_jobs(baseline):
        for name, v in job.get("versions", {}).items():
            m = re.search(r"_(\d{12})_[0-9a-f]{8}$", v.get("build") or "")
            if m and v["channel"] == "rapidsai-nightly":
                stamps.setdefault(m.group(1), f"{name} {v['build']}")
    if not stamps:
        sys.exit(f"{baseline['branch']}: no RAPIDS nightly build strings in the baseline")
    first = min(stamps)
    when = datetime.strptime(first, "%y%m%d%H%M%S").replace(tzinfo=timezone.utc).isoformat()
    base = git(clone, "rev-list", "-1", "--first-parent", f"--before={when}", branch).strip()
    return base, f"{branch} as of {when}, the earliest RAPIDS build in the baseline ({stamps[first]})"


# --- step 3: links to cuML -----------------------------------------------------------

def sklearn_links():
    proxied, patched, vendored = {}, set(), set()
    for py in [*CUML.rglob("*.py"), *CUML.rglob("*.pyx")]:
        for m in re.finditer(r'_cpu_class_path\s*=\s*"(sklearn\.[\w.]+)\.(\w+)"', py.read_text(errors="replace")):
            proxied.setdefault(m.group(1), set()).add(m.group(2))
    for py in (CUML / "accel/_patches/sklearn").rglob("*.py"):
        if py.name != "__init__.py":
            patched.add(py.relative_to(CUML / "accel/_patches").as_posix())
    for py in (CUML / "_thirdparty/sklearn").rglob("*.py"):
        if py.name != "__init__.py":
            vendored.add(py.relative_to(CUML / "_thirdparty").as_posix())

    def annotate(f):
        mod = ".".join(f.removesuffix(".py").split("/")[:2])
        tiers = ([], [], [])
        if f in patched:
            tiers[0].append(f"patched by cuml.accel: {f}")
        if f in vendored:
            tiers[0].append(f"vendored in cuml/_thirdparty: {f}")
        if mod in proxied:
            tiers[1].append(f"{mod} has cuml.accel-proxied estimators {sorted(proxied[mod])}")
        if SKLEARN_CORE.search(f):
            tiers[2].append(f"core API used by all estimators: {f}")
        return tiers

    return annotate


def rapids_links(include_dir, py_packages):
    """Headers cuML includes and Python modules it imports from this library."""
    headers = set()
    for path in [*(CUML_REPO / "cpp").rglob("*"), *(CUML_REPO / "python").rglob("*")]:
        if path.suffix in {".hpp", ".cuh", ".h", ".cpp", ".cu", ".pyx", ".pxd"} and path.is_file():
            for m in re.finditer(rf'#include\s*[<"]({include_dir}/[^>"]+)[>"]|cdef extern from "({include_dir}/[^"]+)"',
                                 path.read_text(errors="replace")):
                headers.add(m.group(1) or m.group(2))
    components = {h.split("/")[1] for h in headers if h.count("/") >= 2}
    modules = set()
    pkg_re = "|".join(py_packages)
    for path in [*CUML.rglob("*.py"), *CUML.rglob("*.pyx"), *CUML.rglob("*.pxd")]:
        for m in re.finditer(rf"^\s*(?:from|import|cimport)\s+((?:{pkg_re})(?:\.\w+)*)", path.read_text(errors="replace"), re.M):
            modules.add(m.group(1))

    def annotate(f):
        tiers = ([], [], [])
        inc = re.search(rf"cpp/include/({include_dir}/.+)$", f)
        if inc and inc.group(1) in headers:
            tiers[0].append(f"header included by cuML: {inc.group(1)}")
        elif inc and inc.group(1).count("/") >= 2 and inc.group(1).split("/")[1] in components:
            tiers[1].append(f"header in component cuML uses ({inc.group(1).split('/')[1]}): {inc.group(1)}")
        elif inc and headers:
            tiers[2].append(f"header of a library cuML includes (possibly transitively): {inc.group(1)}")
        src = re.search(r"cpp/src/([^/]+)/", f)
        if src and src.group(1) in components:
            tiers[1].append(f"implementation of component cuML uses ({src.group(1)}): {f}")
        py = re.search(r"python/[^/]+/((?:" + pkg_re + r")/.+)\.(py|pyx|pxd)$", f)
        if py:
            mod = py.group(1).replace("/", ".").removesuffix(".__init__")
            # cuML imports this module or one of its submodules (importing only the parent package doesn't count)
            used = sorted(m for m in modules if m == mod or m.startswith(mod + "."))
            if used:
                tiers[0].append(f"Python module cuML imports: {mod} (cuML uses {used[:3]})")
        if RAPIDS_PACKAGING.search(f):
            tiers[2].append(f"packaging/build change: {f}")
        return tiers

    return annotate


def treelite_links():
    """treelite modules cuML imports, plus the xfail-list entries conditioned on treelite."""
    modules = set()
    for path in [*CUML.rglob("*.py"), *CUML.rglob("*.pyx")]:
        for m in re.finditer(r"^\s*(?:from|import)\s+(treelite(?:\.\w+)*)", path.read_text(errors="replace"), re.M):
            modules.add(m.group(1))
    xfails = sorted(
        f"{p.relative_to(CUML_REPO)}: {m.group(1)}"
        for p in (CUML_REPO / "python/cuml/cuml_accel_tests").rglob("xfail-list.yaml")
        for m in re.finditer(r"^\s*-?\s*(?:reason|condition):\s*(.*treelite.*)$", p.read_text(errors="replace"), re.M | re.I)
    )

    def annotate(f):
        tiers = ([], [], [])
        py = re.match(r"python/(treelite/.+)\.py$", f)
        if py:
            mod = py.group(1).replace("/", ".").removesuffix(".__init__")
            used = sorted(m for m in modules if m == mod or m.startswith(mod + ".") or mod.startswith(m + "."))
            if used:
                tiers[0].append(f"Python module cuML imports: {mod} (cuML uses {used[:3]})")
        if f.startswith(("src/", "include/")):
            tiers[1].append(f"libtreelite (used by the treelite Python package cuML calls, and by libcuml): {f}")
        if re.search(r"(^|/)(CMakeLists\.txt|cmake/|pyproject\.toml)", f):
            tiers[2].append(f"packaging/build change: {f}")
        if any(tiers):
            tiers[0].extend(f"xfail conditioned on treelite: {x}" for x in xfails)
        return tiers

    return annotate


def rapids_cmake_links():
    """rapids_* functions cuML's CMake calls; rapids-cmake/<module>/<name>.cmake defines rapids_<module>_<name>."""
    files = subprocess.run(["git", "-C", str(CUML_REPO), "ls-files", "--", "*CMakeLists.txt", "*.cmake"],
                           check=True, capture_output=True, text=True).stdout.split()
    calls = set()
    for f in files:
        calls.update(re.findall(r"\b(rapids_\w+)\s*\(", (CUML_REPO / f).read_text(errors="replace")))
    cpm = sorted(c.removeprefix("rapids_cpm_") for c in calls if c.startswith("rapids_cpm_"))

    def annotate(f):
        tiers = ([], [], [])
        if f == "rapids-cmake/cpm/versions.json":
            tiers[0].append(f"CPM version pins; cuML calls rapids_cpm_{{{', '.join(cpm)}}}")
        patch = re.match(r"rapids-cmake/cpm/patches/([\w-]+)/", f)
        if patch and f"rapids_cpm_{patch.group(1)}" in calls:
            tiers[0].append(f"patch for a CPM package cuML fetches (rapids_cpm_{patch.group(1)}): {f}")
        m = re.match(r"rapids-cmake/([\w-]+)/(?:detail/)?([\w-]+)\.cmake$", f)
        if m:
            module = m.group(1).removesuffix("-core")
            func = f"rapids_{module}_{m.group(2)}"
            if func in calls:
                tiers[0].append(f"defines {func}, which cuML calls: {f}")
            elif any(c.startswith(f"rapids_{module}_") for c in calls):
                tiers[1].append(f"module cuML uses ({module}): {f}")
        if not any(tiers):
            tiers[2].append(f"rapids-cmake code used by every RAPIDS build: {f}")
        return tiers

    return annotate


# --- step 2: commits -------------------------------------------------------------------

def commit_items(name, repo, clone, base, head, non_source, annotate, branch):
    log = git(clone, "log", "--first-parent", "--no-renames", "--name-only",
              "--format=%x00%H%x01%cI%x01%s%x02", f"{base}..{head}")
    items = []
    for chunk in log.split("\x00")[1:]:
        header, _, names = chunk.partition("\x02")
        sha, date, subject = header.split("\x01", 2)
        files = [n for n in names.split("\n") if n.strip()]
        pr = PR_NUMBER.search(subject)
        item = {
            "id": f"{name}-{sha[:10]}",
            "source": name,
            "branch": branch,
            "ref": f"{repo}@{sha[:10]}" + (f" (#{pr.group(1)})" if pr else ""),
            "title": subject,
            "url": f"https://github.com/{repo}/pull/{pr.group(1)}" if pr else f"https://github.com/{repo}/commit/{sha}",
            "jobs_affected": [],
            "tests": [],
            "signature": None,
            "detail": {"sha": sha, "date": date, "files": files},
        }
        source = [f for f in files if not non_source.search(f)]
        if name == "scikit-learn":
            source = [f for f in source if f.startswith("sklearn/")]
            item["detail"]["changelog_fragments"] = [f for f in files if f.startswith("doc/whats_new/upcoming_changes/")]
        if source:
            tiers = ([], [], [])
            for f in source:
                for t, reasons in zip(tiers, annotate(f)):
                    t.extend(reasons)
            links = [r for t in tiers for r in dict.fromkeys(t)]
            item["detail"]["cuml_links"] = links or ["source change with no direct link found (still review)"]
        else:
            item["verdict"] = "safe"
            item["evidence"] = "auto: touches no source (only docs/CI/tests/benchmarks)"
        items.append(item)
    return list(reversed(items))  # oldest first


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--upstream", choices=UPSTREAMS)
    ap.add_argument("--list", action="store_true", help="list supported upstreams")
    ap.add_argument("--baseline", help="baseline.json from last_nightly.py")
    ap.add_argument("--branch", default="main", help="cuML branch whose baseline to use (RAPIDS follow it)")
    ap.add_argument("--base", help="explicit base commit")
    ap.add_argument("--head", help="explicit head commit (default: upstream branch head)")
    ap.add_argument("--out")
    args = ap.parse_args()
    if args.list:
        print("\n".join(UPSTREAMS))
        return
    if not args.upstream or not args.out or not (args.base or args.baseline):
        ap.error("need --upstream, --out and --baseline or --base")

    if args.upstream == "scikit-learn":
        repo, upstream_branch = "scikit-learn/scikit-learn", "main"
        non_source, annotate = SKLEARN_NON_SOURCE, sklearn_links()
    elif args.upstream == "treelite":
        repo, upstream_branch = "dmlc/treelite", "mainline"
        non_source, annotate = TREELITE_NON_SOURCE, treelite_links()
    elif args.upstream == "rapids-cmake":
        repo, upstream_branch = "rapidsai/rapids-cmake", args.branch  # cuML's RAPIDS_BRANCH matches its branch
        non_source, annotate = RAPIDS_CMAKE_NON_SOURCE, rapids_cmake_links()
    else:
        repo, package, include_dir, py_packages = RAPIDS[args.upstream]
        upstream_branch = args.branch  # RAPIDS repos use the same main / release/YY.MM scheme as cuML
        non_source, annotate = RAPIDS_NON_SOURCE, rapids_links(include_dir, py_packages)
    clone = ensure_clone(args.upstream, repo, upstream_branch)

    head_ref = upstream_branch
    if args.base:
        base, how = args.base, "explicit"
    else:
        baseline = next((b for b in json.loads(Path(args.baseline).read_text()) if b["branch"] == args.branch), None)
        if baseline is None:
            sys.exit(f"no baseline for branch {args.branch}")
        if args.upstream == "scikit-learn":
            base, how = sklearn_base(baseline)
        elif args.upstream == "treelite":
            base, head_ref, how = treelite_range(baseline)
        elif args.upstream == "rapids-cmake":
            base, how = rapids_cmake_base(baseline, clone, upstream_branch)
        else:
            base, how = rapids_base(baseline, package)
    head = git(clone, "rev-parse", args.head or head_ref).strip()
    base = git(clone, "rev-parse", base).strip()

    # sklearn nightlies and treelite releases are the same for every cuML branch; RAPIDS follow the cuML branch.
    item_branch = "*" if args.upstream in ("scikit-learn", "treelite") else args.branch
    items = commit_items(args.upstream, repo, clone, base, head, non_source, annotate, item_branch)
    Path(args.out).write_text(json.dumps(items, indent=1))
    n_review = sum(1 for i in items if "verdict" not in i)
    print(f"{args.upstream} {upstream_branch}: base {base[:10]} ({how})")
    print(f"head {head[:10]}: {len(items)} commits, {len(items) - n_review} auto-safe, {n_review} to review")
    for i in items:
        mark = "safe" if "verdict" in i else "REVIEW"
        print(f"  {mark:6} {i['ref']:40} {i['title'][:80]}")
        for r in i["detail"].get("cuml_links", [])[:3]:
            print(f"           - {r[:120]}")


if __name__ == "__main__":
    main()
