"""Parser tests on real job logs saved from NVIDIA/cuml nightly runs.

job-114819842595: 2026-10-12 main, nightly-deps wheel job (sklearn 1.10.dev0, LogReg C->alpha).
job-114819845949: 2026-10-12 main, wheel v100 job (xgboost without sm70 kernels).
job-114412022140: 2026-10-11 main, conda gh200 job (one flaky recall assertion).
job-122054487060: 2026-10-27 main, conda-cpp arm64 job (libnvforest link error during env setup).
job-98085997365: 2026-08-26 main, conda cpp-build job (rmm device_scalar API change, nvcc errors).
"""

import collections
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "scripts"))

from nightly_logs import build_file, parse_job_log, signature  # noqa: E402
from nightly_logs import test_key as key_of  # noqa: E402


def parse(job_id):
    return parse_job_log((HERE / "fixtures" / f"job-{job_id}.log").read_text())


def clusters(result):
    return collections.Counter((f["suite"], f["signature"]) for f in result["failures"])


def test_nightly_deps_logreg():
    r = parse(114819842595)
    assert clusters(r) == {
        ("pytest cuml accelerator", "TypeError | 'float' and 'str'"): 42,
        ("pytest cuml single GPU", "FutureWarning | 'C' was deprecated in version N.N and will be "
         "removed in N.N. Use alpha instead."): 2,
    }
    assert r["versions"]["scikit-learn"]["version"] == "1.10.dev0"
    assert r["job_error"] is None
    keys = {key_of(f) for f in r["failures"]}
    assert "python/cuml/cuml_accel_tests::test_pipeline.py::test_umap_with_logistic_regression" in keys


def test_v100_xgboost_single_cluster():
    r = parse(114819845949)
    assert clusters(r) == {
        ("pytest cuml single GPU",
         "xgboost._c_api.XGBoostError | no kernel image is available for execution on the device"): 12,
    }
    assert r["versions"]["xgboost-cu12"]["version"] == "3.4.1"


def test_conda_versions_and_flake():
    r = parse(114412022140)
    assert [f["nodeid"] for f in r["failures"]] == ["test_pickle.py::test_nearest_neighbors_pickle[ivfpq]"]
    cudf = r["versions"]["cudf"]
    assert cudf["channel"] == "rapidsai-nightly"
    assert cudf["build"].endswith("_5c2c5fe2")  # source SHA embedded in the build string


def test_signature_truncated_message_falls_back_to_file():
    assert signature("AssertionError:", "test_preprocessing.py::test_x[1]") == "AssertionError | test_preprocessing.py"
    assert signature("", "test_a.py::t") == "<no message> | test_a.py"


def test_job_error_without_failures():
    log = "\n".join([
        "2026-09-28T07:00:00.0Z WARNING: Retrying (Retry(total=0)) after connection broken by 'ReadTimeoutError'",
        "2026-09-28T07:00:01.0Z ERROR: ResolutionImpossible: for help visit ...",
        "2026-09-28T07:00:02.0Z ##[error]Process completed with exit code 1.",
    ])
    r = parse_job_log(log)
    assert r["failures"] == []
    assert r["job_error"]["infra"] is True


def test_env_setup_link_error_is_not_infra():
    r = parse(122054487060)
    assert r["failures"] == []
    assert r["job_error"]["infra"] is False
    assert "while linking package 'libnvforest'" in r["job_error"]["lines"][0]


def test_sklearn_nightly_download_time():
    r = parse(114819842595)
    name = "scikit_learn-1.10.dev0-cp314-cp314-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl"
    assert r["downloads"][name] == "2026-10-12T12:57:24.4940293Z"


def test_build_errors_from_conda_build():
    r = parse(98085997365)
    assert [e["file"] for e in r["build_errors"]] == ["cpp/src/solver/cd.cuh", "cpp/src/umap/simpl_set_embed/algo.cuh"]
    assert "device_scalar" in r["build_errors"][0]["error"]
    assert r["failures"] == []


def test_build_file_names():
    assert build_file("$SRC_DIR/cpp/tests/../src/umap/algo.cuh") == "cpp/src/umap/algo.cuh"
    assert build_file("/__w/cuml/cuml/cpp/src/hdbscan/runner.cuh") == "cpp/src/hdbscan/runner.cuh"
    assert build_file("$SRC_DIR/cpp/build/_deps/nvforest-src/cpp/include/nvforest/handle.hpp") == (
        "nvforest:cpp/include/nvforest/handle.hpp")
    assert build_file("$PREFIX/include/rmm/device_scalar.hpp") == "include/rmm/device_scalar.hpp"
