---
name: predict-nightly
description: Predict which cuML nightly CI jobs will fail before the nightly runs, using only information available beforehand (last nightly's failures and what changed since), then diagnose them and draft issues. Use when the user asks to "predict the nightly", "what will fail tonight", "run predict-nightly", or to score yesterday's prediction.
---

# predict-nightly

Predicts the **next** cuML nightly (`NVIDIA/cuml`, workflows `build.yaml` then `test.yaml`) for `main` and
the active `release/YY.MM`, and has a diagnosis and an issue (existing or drafted) ready before the nightly
reports the failure. Both build failures and test failures are predicted.

Hard rule: use only information that existed **before the predicted nightly started**. The nightly starts
when `build.yaml` is dispatched, not `test.yaml`. If the nightly for a branch is already running or queued,
its outcome is no longer a prediction: say so and stop for that branch, or wait until it finishes and
predict the one after.

Implemented sources: **carry-over** (build and test failures), and **upstream commits** merged since the
last nightly in scikit-learn, the RAPIDS libraries (rmm, raft, cuvs, cudf, nvforest, kvikio) and
rapids-cmake, and **new releases** of treelite. Not yet: the dependency dry-run diff (other non-RAPIDS
releases such as numpy, scipy, cupy, xgboost). Say in the report that it was not checked.

The aim is to catch most failures, not every one. Commit review is the main tool, and local test runs are
an optional check on a specific verdict.

## Background

- **Schedule (UTC):**
  - sklearn nightly wheels are built from sklearn `main` at ~01:35 and uploaded ~02:20 by
    scikit-learn/scikit-learn-release.
  - RAPIDS nightly packages are built ~05:00–06:30. Each build fetches rapids-cmake's branch head when it
    configures.
  - cuML's `build.yaml` is dispatched ~06:20–06:50 (it varies), and `test.yaml` ~30 min later, for `main`
    and the active release branch. The prediction must be finished before `build.yaml` starts.
  - `test.yaml` only runs if `build.yaml` succeeded. A failed build means no test run that day.
- **Gating:** `wheel-tests-cuml-nightly-dependencies-versions` (sklearn nightly + cupy pre-releases) and
  `wheel-tests-integrations` are `continue-on-error`. They can fail while the run reports success. Predict
  them anyway; they are where sklearn breakage shows up first. `ledger.py score` also reports the predicted
  vs actual run conclusion.
- **Attempts:** a nightly is judged by attempt 1. Manual reruns can turn a failed run green and hide its
  failures, so `last_nightly.py` reads attempt 1's jobs and conclusion.
- **A nightly** is the day's first `build.yaml` dispatch plus that day's `test.yaml` run. Baseline fields:
  - `created_at`: the build dispatch;
  - `build_run_id` / `run_id`: the build and test runs;
  - `conclusion`: failure if either run failed.

  When the latest build failed, the test part comes from the latest earlier test run (`test_created_at` is
  older). Its failures still carry over, and upstream bases point at what was last tested.

## Setup

```bash
source ~/miniforge3/etc/profile.d/conda.sh && conda activate <newest cuml-YYYYMMDD env>
SK=~/.claude/skills/predict-nightly/scripts
OUT=~/git/cuml/agents/nightly-predictions
D=$(date -u +%F)        # ledger name = UTC date of the prediction
W=<scratchpad>/predict-$D   # scratch dir for intermediate JSON
```

Scripts are stdlib-only. They call `gh` at `~/miniforge3/bin/gh` (override with `$GH`). Caches live in
`~/.cache/predict-nightly/`: job logs, blobless clones of each upstream, built wheels (per SHA) and sccache.

`$SK/docker_run.sh` (optional) runs specific cuML tests locally in the RAPIDS CI images on the A6000
(GPU 1):
- **Build:** builds the `cuml` Python wheel at `--ref` (default `upstream/main`, cached per SHA, ~4 min
  cold) against the newest nightly `libcuml`. `--sklearn <sha>` builds that sklearn commit.
- **Test:** installs with the `dependencies.yaml` constraints for `--dependencies` (nightly/latest/oldest)
  and runs the given `<root>::<nodeid>` targets. `python $SK/nightly_logs.py <dir>/test.log` lists the
  failures.
- **Limits:** CUDA 13.3 / py3.14 / amd64 only. It can't test RAPIDS commits that haven't been built into a
  nightly package yet.

## Workflow

1. **Collect the baseline.** `python $SK/last_nightly.py --branch auto --out $W/baseline.json --items $W/carry.json`
   This takes the latest *completed* nightly per branch: its build failed, or its test run completed. It
   warns when a newer nightly has already started (`build.yaml` dispatched). If it warns, today's nightly
   can't be predicted (see the hard rule). Check the time too: `build.yaml` may start in the next minutes, so
   `gh run list --repo NVIDIA/cuml --workflow build.yaml --limit 4`.

2. **Score previous predictions.** A ledger predicts the nightly of its own UTC date (`created[:10]`) and is
   scored against that day's run, no matter when scoring happens. For every `$OUT/*.json` with
   `"score": null` whose date's nightly has completed:
   ```
   python $SK/last_nightly.py --branch auto --date <ledger date> --out $W/scored-<ledger date>.json
   python $SK/ledger.py score <ledger> $W/scored-<ledger date>.json --scorecard $OUT/scorecard.md
   ```
   `score` refuses a nightly from any other date. If a branch had no nightly that day, it stays unscored.
   Build items are scored against `build.yaml` jobs and test items against `test.yaml` jobs. When the build
   failed and there was no test run, test items are `unobserved`.
   Outcomes:
   - `hit`: predicted failure that failed;
   - `false_alarm`: predicted failure that passed;
   - `miss`: a failure no item predicted, or an item judged `safe`/`fixed` that failed;
   - `risky_failed` / `risky_passed`: how `risky` items turned out;
   - `infra`: unpredicted infrastructure failure, out of scope;
   - `unobserved`: the item's workflow didn't run, or every job it concerns was cancelled.

   The score warns if the ledger was written after the nightly started (`build.yaml` dispatch).
   For every `miss` and `false_alarm`, write one line on why in the report under "Lessons". If the lesson
   generalises, propose an edit to this skill. Do not silently edit it.

3. **Start today's ledger.** A ledger belongs to one set of baselines. If `$OUT/$D.json` already exists
   from an earlier run today, and was scored in step 2 or has different baselines, use `D=$D-2` (then `-3`, …).
   If it has the same baselines, skip `init` and continue it; `add` skips items already present.
   ```
   python $SK/ledger.py init $OUT/$D.json --baseline $W/baseline.json
   python $SK/ledger.py add $OUT/$D.json $W/carry.json
   ```

4. **Carry-over: decide each cluster.** Run `python $SK/changed_since.py $W/baseline.json --out $W/changes.json`.
   It lists the cuML commits since each run's SHA, the commits related to each cluster, and whether the
   packages named in the error have newer releases. Then give each carry-over item a verdict:
   - `infra`: job-level failure matching infra patterns (index timeouts, missing channel subdir, runner loss).
     These are out of scope; no action needed. An env-setup failure that names a RAPIDS package (e.g.
     `while linking package '<pkg>'`) is not infra. It points to a broken package, so add an item for it
     (`carry-over`, or `risky` if other jobs with the same build passed).
   - A failed job with no test failures becomes a `job | ...` item. Bracketed paths are stripped from its signature
     so it stays stable across nights. If the signature reads `no recognised error line`, read the log tail
     (`~/.cache/predict-nightly/logs/<job id>.log`) and apply the rule above.
   - A `build.yaml` job with compiler errors becomes a `[build] build: <file>` item. Its `tests` are
     `build::<file>` for every file with an error:
     - cuML files are repo-relative (`cpp/src/...`);
     - CPM-fetched sources are `<dep>:<path>`;
     - installed headers are `include/<path>`.

     Jobs cluster by their alphabetically first error file, so one root cause can produce several items
     (e.g. CUDA 12 vs 13 jobs failing in different files). Decide them together. `fixed` needs a commit or
     package that resolves every listed file.
   - `carry-over`: nothing relevant changed, or the change doesn't address the failure. Read the
     commit diff (`gh api repos/NVIDIA/cuml/commits/<sha>`) before deciding; a commit "touching the test
     file" may be unrelated.
   - `fixed`: a merged commit plausibly fixes it (read the diff, check the fix covers **every** failing test in
     the cluster and every affected job), or a newer dependency release fixes it. Evidence must name
     the commit/PR or release.
   - Before predicting a single-test failure repeats, check whether it is flaky. Look at the 2–3 previous
     nightlies for the same branch (`last_nightly.py --run-id <id> --out ...`). If it failed in only some
     of them, use `risky` with the evidence "intermittent".
   - Optionally (step 6), rerun the cluster's tests at HEAD to confirm a claimed fix:
     `$SK/docker_run.sh --out $W/verify-<id> --dependencies <job's *-deps suffix> -- <tests>`. Only amd64
     clusters can be checked this way.
   The heuristics in `changed_since.py` produce false positives (e.g. a squash message that lists unrelated
   test names). Every flagged commit gets read.

   Record verdicts by writing a dispositions JSON and running `python $SK/ledger.py apply $OUT/$D.json <file>`.
   Each entry is `{"id", "verdict", "evidence", "test_run"?, "action"?, "tests"?, "signature"?}`.

5. **Upstream commits: triage everything merged since the last nightly.** For each upstream
   (`python $SK/upstream_commits.py --list`) and each baseline branch, run
   `python $SK/upstream_commits.py --upstream <name> --baseline $W/baseline.json --branch <branch> --out $W/<name>-<branch>.json`,
   then `ledger.py add` it. sklearn and treelite are the same for every branch, so run them once.
   - The script finds the exact upstream commit the last nightly used:
     - sklearn: from the scikit-learn-release run that built the installed nightly wheel;
     - RAPIDS: from the SHA in the conda build string;
     - treelite: the release tag of the newest treelite the last nightly installed;
     - rapids-cmake: no build string records it, so the base is the branch commit as of the earliest RAPIDS
       build time (from the build strings) in the baseline. The range can include commits some of last
       night's packages already used, so review them all anyway.
     It then lists every commit from there to the upstream branch head. For treelite, the head is the
     newest release on conda-forge/PyPI, so the range is empty unless a new treelite was released.
   - Commits touching no source (docs, CI, tests, benchmarks) arrive already `safe`.
   - The rest carry `detail.cuml_links`, strongest first:
     - sklearn: patched/vendored by cuML > module with cuml.accel-proxied estimators > core API.
     - RAPIDS: header cuML includes / Python module cuML imports > header or `cpp/src` of a component cuML
       uses > any header of the library, or packaging/CMake.
     - treelite: Python module cuML imports, plus the xfail-list entries that name treelite > `src/` or
       `include/` (libtreelite) > packaging/CMake.
     - rapids-cmake: defines a `rapids_*` function cuML's CMake calls, `cpm/versions.json`, or a CPM patch for
       a package cuML fetches > another file of a module cuML uses > anything else (used by every RAPIDS
       build).
   - "No direct link found" still gets reviewed.
   - Hand each upstream's unreviewed items to **one subagent** (general-purpose), running them in parallel.
     Use the prompt below, plus the upstream-specific hints. Apply each subagent's JSON with `ledger.py apply`.
     Do not review the commits in the main context.

   Subagent prompt (fill in upstream, items file, cuML path `~/git/cuml`):
   > For each <upstream> commit in `<items file>`, decide whether it will make tonight's cuML nightly CI fail.
   > Read each diff with `gh pr diff <n> --repo <repo>` or `git --git-dir ~/.cache/predict-nightly/<upstream>.git
   > show <sha>`, then look at how cuML (`~/git/cuml`) uses what changed. `detail.cuml_links` says where to start,
   > but don't stop there. Do not trust PR titles or labels; read the diff. Do not run tests, Docker or builds,
   > and do not modify files.
   > Return only a JSON array, one entry per commit:
   > `{"id", "verdict": "safe"|"risky"|"breaks", "evidence": "<what changed and what in cuML it touches, with
   > file:line>", "tests": ["<root>::<nodeid>", ...], "signature": "<expected error, if known>"}`.
   > - `tests` are the cuML tests you expect to fail. Roots are `python/cuml/tests`,
   >   `python/cuml/cuml_accel_tests`, or `cpp` with gtest names. A test name without `[params]` covers all
   >   its parametrizations.
   > - risky/breaks need `tests` or a `signature`.
   > - A predicted **build** failure of cuML (`build.yaml`) lists the files expected to fail to compile as
   >   `"tests": ["build::<file>", ...]`:
   >   - cuML files repo-relative (`cpp/src/...`);
   >   - CPM-fetched sources as `<dep>:<path>`;
   >   - installed headers as `include/<path>`.
   >
   >   If the failing file can't be named (CMake/configure or packaging breaks), use `"signature": "build"`,
   >   which matches any failed `build.yaml` job.
   > - "safe" needs a reason, not just "unrelated".

   Upstream-specific hints to append:
   - **scikit-learn:** the nightly-deps job runs `python/cuml/tests` and `python/cuml/cuml_accel_tests`
     against the sklearn nightly. Check the wrappers/proxies (`cuml/accel/`, `_cpu_class_path`),
     `as_sklearn`/`from_sklearn` interop (`_params_to_cpu`, `_params_from_cpu`, `_attrs_from_cpu`), vendored
     code (`cuml/_thirdparty/sklearn`), and tests that compare against sklearn estimators. **New
     deprecations/FutureWarnings break**, because cuML's test config turns warnings into errors. Read the
     changelog fragments in `detail.changelog_fragments`. New or changed tests in sklearn's own test suite
     are not run tonight: `conda-python-scikit-learn-accel-tests` runs `--pyargs sklearn` against the
     *released* conda sklearn, and `conda-python-cuml-accel-upstream-tests` runs only the UMAP suite. Such
     tests can't be a predicted failure; mention them as a note for the next sklearn release.
   - **RAPIDS (rmm/raft/cuvs/cudf/nvforest/kvikio):** every nightly job builds and tests cuML against
     tonight's RAPIDS nightlies. Look for:
     - C++ API changes in headers cuML includes (removed/renamed functions, changed signatures or
       templates): these are **build** failures of libcuml;
     - behaviour or accuracy changes in algorithms cuML calls (e.g. cuvs neighbors/cluster, raft linalg,
       nvforest inference): these show up as test tolerance failures;
     - Python API changes or deprecations in modules cuML imports (pylibraft, rmm, cudf, cuvs);
     - packaging changes that alter wheel/conda dependencies.
   - **treelite:** a release reaches every job whose treelite run pin allows it. Check the run pin in
     `conda/recipes/*/conda_build_config.yaml` and `dependencies.yaml`; a release outside it has no effect.
     cuML builds against a fixed treelite (`==`), but at runtime uses the installed `treelite` Python package
     and its libtreelite. That covers RF/IsolationForest `as_sklearn`/`from_sklearn` and `cuml.accel`
     (`treelite.sklearn.import_model`/`export_model`, `Model.deserialize_bytes` of libcuml-serialized models in
     `cuml/ensemble/randomforest_common.pyx`, `isolation_forest.pyx`) and TreeSHAP (`treelite.frontend`).
     - A treelite **bug fix** can turn a strict xfail into **XPASS(strict)**, which fails the job.
       `conda-python-scikit-learn-accel-tests` runs sklearn's forest/tree tests through that export path.
       Search `python/cuml/cuml_accel_tests/upstream/*/xfail-list.yaml` for the tests that exercise the
       fixed behaviour (e.g. a depth fix → `max_depth`/`max_leaf_nodes` tests), not only for entries that
       mention treelite.
     - Serialization-format or export changes can break round-trips between libcuml's treelite and the
       installed Python one.
     - A behaviour change can also make xfails conditioned on a treelite version start or stop applying.
   - **rapids-cmake:** every RAPIDS build fetches the branch head when it configures, including libcuml in
     cuML's `build.yaml` (`RAPIDS_BRANCH`, no SHA pin) and cuML's Python build. Breakage shows up as a
     **build** failure (CMake configure, compile or packaging), not a test failure. Look for:
     - `cpm/versions.json` bumps (CCCL, rmm, rapids_logger, gtest, gbench, …). A new CCCL compiles all of
       cuML's C++, so check cuML's CCCL/CUB/Thrust usage against the release's breaking changes;
     - changes to functions cuML calls (`git grep -n 'rapids_' -- '*CMakeLists.txt' '*.cmake'` in cuML);
     - CUDA architecture or compiler-flag defaults (`rapids_cuda_*`);
     - export, install or Cython-build changes that alter what ends up in packages. A packaging regression in
       another RAPIDS package can break cuML's test environment setup (e.g. rapidsai/rapids-cmake#1106 fixed
       `__pycache__` files that leaked into libnvforest).

6. **Optional local check.** If a verdict hinges on whether a specific test fails, and the change can be
   tested locally (sklearn commits, or cuML commits for carry-overs), run
   `$SK/docker_run.sh --out $W/check-<id> [--sklearn <sha>] -- <tests>` and record `test_run`. Skip it
   otherwise. A clear diff review is enough.

7. **Diagnose predicted failures (`carry-over`, `breaks`).** The output is a diagnosis, not a fix.
   Writing the fix is a separate, later task.
   - Dedupe first: `python $SK/find_existing.py '<test name>' '<error regex>' --state all`. It lists and greps
     issues/PRs locally because `gh search` / `--search` return nothing for NVIDIA/cuml. For upstream causes,
     run it against the upstream repo too (`--repo scikit-learn/scikit-learn`).
   - Record what you find as `action: {"existing_issue": url}` or `{"existing_pr": url}`.
   - If nothing exists, write a draft issue to `$OUT/$D-drafts/<item id>.md` and record it as
     `action: {"draft_issue": path}`. The draft covers:
     - the failing jobs/tests and the error;
     - the cause, pinned to a commit/PR/release;
     - the evidence, including the local run;
     - the suggested direction for a fix, in one or two sentences, without implementing it.
   - If the cause is an upstream bug (not an intended API change), also write an upstream issue draft and
     record it as `upstream_issue`.

8. **Gate.** `python $SK/ledger.py check $OUT/$D.json` must exit 0. If it doesn't, keep working through the items it
   lists. Do not skip items and do not write the report while the check fails.

9. **Report.** Write `$OUT/$D.md`, then summarise it to the user. Contents:
   - The baselines (branch → build and test run links, SHA) and, for each upstream, the commit range
     triaged (base → head).
   - The score of the previous prediction and its Lessons.
   - **Predicted failures**: job(s), tests, diagnosis, and the issue (existing issue/PR link or draft path).
   - Risky items.
   - Items judged fixed, with the fixing commit.
   - Sources not checked (see "Implemented sources").
   - Known blind spots:
     - commits merged after the prediction but before the nightly starts are not seen;
     - a build break in another RAPIDS library only stops its new nightly package being published, so cuML
       tests against the previous one. Score sees nothing, and a correct prediction of it counts as a
       `false_alarm`;
     - build predictions can't be checked locally: `docker_run.sh` doesn't build libcuml;
     - local runs cover amd64 / CUDA 13 only.

10. **Stop.** Do not write fixes, commit, push, or post issues. Tell the user which drafts are ready to post.
