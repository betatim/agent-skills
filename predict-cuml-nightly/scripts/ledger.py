#!/usr/bin/env python
"""Prediction ledger for /predict-nightly.

Every collected item (carry-over cluster, sklearn commit, RAPIDS PR, changed
package) gets a disposition. ``check`` fails until all of them have one, so
the report cannot be written while anything is unexamined.

Verdicts:
  carry-over  last night's failure will repeat (predicts failure)
  breaks      this change will make the next nightly fail (predicts failure)
  risky       plausible breakage, not confirmed (predicts nothing; scored separately)
  safe        examined, no effect expected
  fixed       last night's failure will not repeat
  infra       infrastructure failure, out of scope

Usage:
  ledger.py init LEDGER --baseline baseline.json
  ledger.py add LEDGER items.json
  ledger.py set LEDGER ID --verdict V --evidence TEXT [--test-run JSON] [--action JSON]
  ledger.py apply LEDGER dispositions.json      # [{"id", "verdict", "evidence", ...}, ...]

Items with branch "*" (e.g. sklearn commits) apply to every baseline branch.
A predicted build failure names ``tests: ["build::<file>", ...]`` and/or ``signature: "build: <file>"``,
or ``signature: "build"`` when the file is unknown (matches any failed build.yaml job).
  ledger.py show LEDGER [--pending]
  ledger.py check LEDGER
  ledger.py score LEDGER next_baseline.json --scorecard scorecard.md
                                                # next_baseline: last_nightly.py --date <ledger's UTC date>
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PREDICTS_FAILURE = {"carry-over", "breaks"}
VERDICTS = PREDICTS_FAILURE | {"risky", "safe", "fixed", "infra"}
ACTION_KEYS = {"existing_issue", "existing_pr", "draft_issue", "upstream_issue"}


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(path):
    return json.loads(Path(path).read_text())


def save(path, ledger):
    Path(path).write_text(json.dumps(ledger, indent=1) + "\n")


def by_id(ledger):
    return {item["id"]: item for item in ledger["items"]}


def cmd_init(args):
    if Path(args.ledger).exists():
        sys.exit(f"{args.ledger} already exists")
    baselines = load(args.baseline)
    ledger = {
        "created": now(),
        "baselines": {
            b["branch"]: {"run_id": b["run_id"], "build_run_id": b["build_run_id"], "sha": b["sha"],
                          "created_at": b["created_at"]}
            for b in baselines
        },
        "items": [],
        "score": None,
    }
    Path(args.ledger).parent.mkdir(parents=True, exist_ok=True)
    save(args.ledger, ledger)
    print(f"initialised {args.ledger} with baselines {ledger['baselines']}")


def cmd_add(args):
    ledger = load(args.ledger)
    existing = by_id(ledger)
    added = 0
    for item in load(args.items):
        if item["id"] in existing:
            continue
        item.setdefault("verdict", None)
        item.setdefault("evidence", None)
        item.setdefault("test_run", None)
        item.setdefault("action", None)
        ledger["items"].append(item)
        added += 1
    save(args.ledger, ledger)
    print(f"added {added} items ({len(ledger['items'])} total)")


def set_disposition(item, d):
    verdict = d["verdict"]
    if verdict not in VERDICTS:
        sys.exit(f"{item['id']}: unknown verdict {verdict!r}, expected one of {sorted(VERDICTS)}")
    item["verdict"] = verdict
    item["evidence"] = d["evidence"]
    # tests/signature/jobs_affected let a predicted breakage be scored against the next nightly.
    for key in ("test_run", "action", "tests", "signature", "jobs_affected"):
        if d.get(key) is not None:
            item[key] = d[key]
    item["disposed_at"] = now()


def cmd_set(args):
    ledger = load(args.ledger)
    items = by_id(ledger)
    if args.id not in items:
        sys.exit(f"no item {args.id}")
    set_disposition(items[args.id], {
        "verdict": args.verdict,
        "evidence": args.evidence,
        "test_run": json.loads(args.test_run) if args.test_run else None,
        "action": json.loads(args.action) if args.action else None,
    })
    save(args.ledger, ledger)


def cmd_apply(args):
    ledger = load(args.ledger)
    items = by_id(ledger)
    unknown = []
    for d in load(args.dispositions):
        if d["id"] not in items:
            unknown.append(d["id"])
            continue
        set_disposition(items[d["id"]], d)
    save(args.ledger, ledger)
    if unknown:
        sys.exit(f"unknown item ids (not applied): {unknown}")


def problems(item):
    out = []
    if item["verdict"] is None:
        out.append("no verdict")
    elif not item.get("evidence"):
        out.append("no evidence")
    if item["verdict"] in PREDICTS_FAILURE:
        action = item.get("action") or {}
        if not ACTION_KEYS & set(action):
            out.append(f"predicts failure but action lacks one of {sorted(ACTION_KEYS)}")
        if not item.get("tests") and not item.get("signature"):
            out.append("predicts failure but names no tests or signature, so it cannot be scored")
    return out


def cmd_show(args):
    ledger = load(args.ledger)
    for item in ledger["items"]:
        if args.pending and not problems(item):
            continue
        print(f"{item['id']:28} {item['source']:10} {item['branch']:14} "
              f"{item['verdict'] or '-':10} {item['title'][:90]}")


def cmd_check(args):
    ledger = load(args.ledger)
    bad = [(item["id"], problems(item)) for item in ledger["items"] if problems(item)]
    for item_id, p in bad:
        print(f"{item_id}: {'; '.join(p)}")
    counts = {}
    for item in ledger["items"]:
        counts[item["verdict"]] = counts.get(item["verdict"], 0) + 1
    print(f"{len(ledger['items'])} items, verdicts {counts}, {len(bad)} incomplete")
    sys.exit(1 if bad or not ledger["items"] else 0)


def test_matches(predicted, actual):
    """A predicted key without parameters covers every parametrization of that test."""
    return predicted == actual or ("[" not in predicted and actual.startswith(predicted + "["))


def is_build_item(item):
    return (item.get("signature") or "").startswith("build") or any(
        t.startswith("build::") for t in item.get("tests") or ())


def matches(item, cluster):
    if any(test_matches(p, a) for p in item.get("tests") or () for a in cluster["tests"]):
        return True
    sig = item.get("signature")
    if not sig:
        return False
    if sig == "build":
        return cluster.get("workflow") == "build"
    if sig.startswith("build: ") and f"build::{sig.removeprefix('build: ')}" in cluster["tests"]:
        return True
    return sig == cluster["signature"]


# These jobs are continue-on-error: they can fail while the run reports success.
NON_GATING = ("wheel-tests-cuml-nightly-dependencies-versions", "wheel-tests-integrations")


def predicts_run_failure(item):
    """A predicted failure fails the run unless every job it concerns is non-gating."""
    if item["verdict"] not in PREDICTS_FAILURE:
        return False
    jobs = item.get("jobs_affected") or ()
    return not jobs or not all(j.startswith(NON_GATING) for j in jobs)


def cmd_score(args):
    ledger = load(args.ledger)
    next_runs = {b["branch"]: b for b in load(args.next_baseline)}
    rows = []
    summary = {"hit": 0, "false_alarm": 0, "miss": 0, "infra": 0, "risky_failed": 0, "risky_passed": 0,
               "unobserved": 0}
    for branch, base in ledger["baselines"].items():
        nxt = next_runs.get(branch)
        if nxt is None:
            rows.append(f"- {branch}: no next run supplied, not scored")
            continue
        if nxt["created_at"] <= base["created_at"]:
            sys.exit(f"{branch}: nightly {nxt['created_at']} is not after the baseline nightly {base['created_at']}")
        if nxt["created_at"][:10] != ledger["created"][:10]:
            sys.exit(f"{branch}: nightly started {nxt['created_at'][:10]}, but this ledger predicts "
                     f"the {ledger['created'][:10]} nightly; collect it with last_nightly.py --date")
        # The nightly starts with build.yaml; created_at is the build dispatch.
        if ledger["created"] >= nxt["created_at"]:
            rows.append(f"- {branch}: WARNING ledger created {ledger['created']} after the nightly started "
                        f"{nxt['created_at']}; prediction may have used post-start info")
        observed = {w for w, run in (("build", nxt["build_run_id"]), ("test", nxt["run_id"])) if run}
        if "test" not in observed:
            rows.append(f"- {branch}: no test.yaml run (build {nxt['build_conclusion']}); "
                        "test predictions are unobserved")
        items = [i for i in ledger["items"] if i["branch"] in (branch, "*")]
        predicted = "failure" if any(predicts_run_failure(i) for i in items) else "success"
        rows.append(f"- {branch} run conclusion (attempt 1): predicted {predicted}, actual {nxt['conclusion']}"
                    + ("" if predicted == nxt["conclusion"] else " **MISMATCH**"))
        finished = {j["name"] for j in nxt["jobs"] if j["conclusion"] in ("success", "failure")}
        matched_clusters = set()
        for item in items:
            hit_clusters = [k for k, c in enumerate(nxt["clusters"]) if matches(item, c)]
            matched_clusters.update(hit_clusters)
            jobs = set(item.get("jobs_affected") or ())
            workflow = "build" if is_build_item(item) else "test"
            if not hit_clusters and (workflow not in observed or jobs and not jobs & finished):
                outcome = "unobserved"  # its workflow did not run, or every job it concerns was cancelled/skipped
            elif item["verdict"] in PREDICTS_FAILURE:
                outcome = "hit" if hit_clusters else "false_alarm"
            elif item["verdict"] == "risky":
                outcome = "risky_failed" if hit_clusters else "risky_passed"
            elif hit_clusters:
                outcome = "miss"  # examined and judged safe/fixed, but it failed
            else:
                continue
            summary[outcome] += 1
            item["outcome"] = outcome
            rows.append(f"- {branch} {outcome}: `{item['id']}` {item['title'][:100]}")
        for k, c in enumerate(nxt["clusters"]):
            if k in matched_clusters:
                continue
            outcome = "infra" if c.get("infra") else "miss"
            summary[outcome] += 1
            rows.append(f"- {branch} {outcome} (unpredicted): [{c['suite'] or 'job'}] {c['signature'][:100]} "
                        f"({len(c['tests'])} tests, {len(c['jobs'])} jobs)")
    ledger["score"] = {
        "scored_at": now(),
        "against": {b: {"run_id": r["run_id"], "build_run_id": r["build_run_id"], "url": r["url"]}
                    for b, r in next_runs.items()},
        "summary": summary,
    }
    save(args.ledger, ledger)

    header = f"## {Path(args.ledger).stem} (scored {ledger['score']['scored_at']})\n\n"
    against = ", ".join(f"[{b} build {r['build_run_id']} / test {r['run_id']}]({r['url']})" for b, r in next_runs.items())
    text = (header + f"Against {against}. "
            + ", ".join(f"{k} {v}" for k, v in summary.items()) + "\n\n" + "\n".join(rows) + "\n\n")
    print(text)
    if args.scorecard:
        with open(args.scorecard, "a") as fh:
            fh.write(text)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("ledger"); p.add_argument("--baseline", required=True)
    p.set_defaults(func=cmd_init)
    p = sub.add_parser("add"); p.add_argument("ledger"); p.add_argument("items"); p.set_defaults(func=cmd_add)
    p = sub.add_parser("set"); p.add_argument("ledger"); p.add_argument("id")
    p.add_argument("--verdict", required=True); p.add_argument("--evidence", required=True)
    p.add_argument("--test-run"); p.add_argument("--action"); p.set_defaults(func=cmd_set)
    p = sub.add_parser("apply"); p.add_argument("ledger"); p.add_argument("dispositions")
    p.set_defaults(func=cmd_apply)
    p = sub.add_parser("show"); p.add_argument("ledger"); p.add_argument("--pending", action="store_true")
    p.set_defaults(func=cmd_show)
    p = sub.add_parser("check"); p.add_argument("ledger"); p.set_defaults(func=cmd_check)
    p = sub.add_parser("score"); p.add_argument("ledger"); p.add_argument("next_baseline")
    p.add_argument("--scorecard"); p.set_defaults(func=cmd_score)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
