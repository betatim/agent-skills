#!/bin/bash
# Run cuML tests at a given ref inside RAPIDS CI images, against nightly-style deps.
#
# Stage 1 (rapidsai/ci-wheel): build the `cuml` Python wheel at REF against the
#   newest nightly `libcuml` wheel (C++ is not rebuilt), and optionally a
#   scikit-learn wheel at a given commit. Wheels are cached per SHA.
# Stage 2 (rapidsai/citestwheel): install them the way ci/test_wheel.sh does
#   (constraints from dependencies.yaml for RAPIDS_DEPENDENCIES) and run the
#   given tests.
#
# Usage:
#   docker_run.sh --out DIR [--ref upstream/main] [--sklearn nightly|<sha>]
#                 [--dependencies nightly|latest|oldest] [--gpu 1]
#                 -- <root>::<nodeid> ...
#
# Test targets use the ledger's key format, e.g.
#   python/cuml/tests::test_logistic_regression.py::test_logistic_regression
#
# Writes DIR/test.log (pytest output with suite banners that nightly_logs.py
# understands), DIR/junit-*.xml and DIR/run.json (what was run).
set -euo pipefail

REPO="${CUML_REPO:-${HOME}/git/cuml}"
CACHE="${HOME}/.cache/predict-cuml-nightly"
REF="upstream/main"
SKLEARN="nightly"
DEPENDENCIES="nightly"
GPU="${GPU:-1}"
CUDA="13.3.0"
PY="3.14"
OUT=""
TARGETS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref) REF="$2"; shift 2 ;;
    --sklearn) SKLEARN="$2"; shift 2 ;;
    --dependencies) DEPENDENCIES="$2"; shift 2 ;;
    --gpu) GPU="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --) shift; TARGETS=("$@"); break ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ -n "${OUT}" ]] || { echo "--out is required" >&2; exit 2; }
[[ ${#TARGETS[@]} -gt 0 ]] || { echo "need test targets" >&2; exit 2; }

if [[ "${REF}" == upstream/* ]]; then
  git -C "${REPO}" fetch -q upstream "${REF#upstream/}"
fi
SHA=$(git -C "${REPO}" rev-parse "${REF}")
RAPIDS_VERSION=$(git -C "${REPO}" show "${SHA}:VERSION" | cut -d. -f1,2)
TAG="${RAPIDS_VERSION}-cuda${CUDA}"
BUILD_IMAGE="rapidsai/ci-wheel:${TAG}-rockylinux8-py${PY}"
TEST_IMAGE="rapidsai/citestwheel:${TAG}-ubuntu26.04-py${PY}"
CUDA_SUFFIX="cu${CUDA%%.*}"

WHEELS="${CACHE}/wheels/cuml-${SHA}"
mkdir -p "${WHEELS}" "${CACHE}/sccache" "${CACHE}/pip" "${OUT}"
OUT=$(realpath "${OUT}")

SKLEARN_WHEELS=""
if [[ "${SKLEARN}" != "nightly" ]]; then
  SKLEARN_WHEELS="${CACHE}/wheels/sklearn-${SKLEARN}"
  mkdir -p "${SKLEARN_WHEELS}"
fi

# Both containers run as root (the test stage installs into the image's Python);
# hand everything written to mounted dirs back to the invoking user at the end.
OWNER="$(id -u):$(id -g)"

if ! ls "${WHEELS}"/cuml_*.whl >/dev/null 2>&1 || { [[ -n "${SKLEARN_WHEELS}" ]] && ! ls "${SKLEARN_WHEELS}"/*.whl >/dev/null 2>&1; }; then
  echo ">>> building wheels for cuml@${SHA:0:10} in ${BUILD_IMAGE}"
  docker run --rm \
    -v "${REPO}:/repo:ro" -v "${WHEELS}:/wheels" -v "${CACHE}/sccache:/sccache" -v "${CACHE}/pip:/pipcache" \
    ${SKLEARN_WHEELS:+-v "${SKLEARN_WHEELS}:/sklearn-wheels"} \
    -e SHA="${SHA}" -e RAPIDS_VERSION="${RAPIDS_VERSION}" -e CUDA_SUFFIX="${CUDA_SUFFIX}" \
    -e SKLEARN="${SKLEARN}" -e OWNER="${OWNER}" -e PIP_CACHE_DIR=/pipcache \
    -e SCCACHE_DIR=/sccache -e SCCACHE_CACHE_SIZE=50G \
    "${BUILD_IMAGE}" bash -euo pipefail -c '
      trap "chown -R ${OWNER} /wheels /sccache /pipcache /sklearn-wheels 2>/dev/null || true" EXIT
      chown -R root /pipcache  # pip ignores a cache dir owned by another user
      git config --global --add safe.directory "*"
      mkdir -p /work && git -C /repo archive "${SHA}" | tar -x -C /work
      cd /work
      if ! ls /wheels/cuml_*.whl >/dev/null 2>&1; then
        pip download --no-deps --pre -d /wheels "libcuml-${CUDA_SUFFIX}==${RAPIDS_VERSION}.*"
        export PIP_CONSTRAINT=/tmp/constraints.txt
        echo "libcuml-${CUDA_SUFFIX} @ file://$(ls /wheels/libcuml_*.whl)" > "${PIP_CONSTRAINT}"
        export SKBUILD_CMAKE_ARGS="-DDISABLE_DEPRECATION_WARNINGS=ON;-DSINGLEGPU=OFF;-DUSE_LIBCUML_WHEEL=ON;-DCMAKE_C_COMPILER_LAUNCHER=sccache;-DCMAKE_CXX_COMPILER_LAUNCHER=sccache;-DCMAKE_CUDA_COMPILER_LAUNCHER=sccache"
        export CMAKE_BUILD_PARALLEL_LEVEL=$(nproc)
        python -m pip wheel ./python/cuml --no-deps -w /wheels \
          --config-settings=skbuild.wheel.py-api="cp${RAPIDS_PY_VERSION//./}"
        sccache --show-stats 2>/dev/null | sed -n 1,5p
      fi
      if [[ "${SKLEARN}" != nightly ]] && ! ls /sklearn-wheels/*.whl >/dev/null 2>&1; then
        python -m pip wheel --no-deps -w /sklearn-wheels \
          "scikit-learn @ git+https://github.com/scikit-learn/scikit-learn@${SKLEARN}"
      fi
    '
fi

ls "${WHEELS}"/cuml_*.whl "${WHEELS}"/libcuml_*.whl >/dev/null

cat > "${OUT}/run.json" <<EOF
{"ref": "${REF}", "sha": "${SHA}", "sklearn": "${SKLEARN}", "dependencies": "${DEPENDENCIES}",
 "targets": $(printf '%s\n' "${TARGETS[@]}" | python3 -c 'import json,sys; print(json.dumps([l for l in sys.stdin.read().splitlines() if l]))'),
 "build_image": "${BUILD_IMAGE}", "test_image": "${TEST_IMAGE}", "gpu": "${GPU}",
 "libcuml": "$(basename "$(ls "${WHEELS}"/libcuml_*.whl)")"}
EOF

echo ">>> testing cuml@${SHA:0:10} (sklearn=${SKLEARN}, dependencies=${DEPENDENCIES}) in ${TEST_IMAGE}"
set +e
docker run --rm --gpus "device=${GPU}" --shm-size=8g \
  -v "${REPO}:/repo:ro" -v "${WHEELS}:/wheels:ro" -v "${OUT}:/out" -v "${CACHE}/pip:/pipcache" \
  ${SKLEARN_WHEELS:+-v "${SKLEARN_WHEELS}:/sklearn-wheels:ro"} \
  -e SHA="${SHA}" -e RAPIDS_DEPENDENCIES="${DEPENDENCIES}" -e SKLEARN="${SKLEARN}" \
  -e OWNER="${OWNER}" -e PIP_CACHE_DIR=/pipcache -e COLUMNS=250 \
  "${TEST_IMAGE}" bash -uo pipefail -c '
    trap "chown -R ${OWNER} /out /pipcache 2>/dev/null || true" EXIT
    chown -R root /pipcache  # pip ignores a cache dir owned by another user
    banner() { echo "|    $1    |"; }
    git config --global --add safe.directory "*"
    mkdir -p /work && git -C /repo archive "${SHA}" | tar -x -C /work
    cd /work
    source rapids-init-pip
    rapids-generate-pip-constraints test_python "${PIP_CONSTRAINT}"
    if [[ "${SKLEARN}" != nightly ]]; then
      echo "scikit-learn @ file://$(ls /sklearn-wheels/*.whl)" >> "${PIP_CONSTRAINT}"
    fi
    rapids-pip-retry install --prefer-binary --constraint "${PIP_CONSTRAINT}" \
      /wheels/libcuml_*.whl "$(ls /wheels/cuml_*.whl)[test]" || exit 3
    python -c "import sklearn, cuml; print(\"sklearn\", sklearn.__version__, \"cuml\", cuml.__version__)"
    pip list 2>/dev/null | grep -iE "^(scikit-learn|numpy|scipy|cupy|xgboost|cudf|libcuml|cuml) " || true
    EXIT=0
    # Group "<root>::<nodeid>" targets by root and run each group from its root.
    declare -A groups
    for t in "$@"; do root="${t%%::*}"; groups["$root"]+=" ${t#*::}"; done
    for root in "${!groups[@]}"; do
      banner "targeted ${root}"
      (cd "/work/${root}" && python -m pytest -p no:cacheprovider -rfE -v ${groups[$root]} \
        --junitxml="/out/junit-$(echo "${root}" | tr / _).xml") || EXIT=1
    done
    exit ${EXIT}
  ' bash "${TARGETS[@]}" 2>&1 | tee "${OUT}/test.log"
STATUS=${PIPESTATUS[0]}
set -e
echo ">>> exit ${STATUS}; log ${OUT}/test.log"
exit "${STATUS}"
