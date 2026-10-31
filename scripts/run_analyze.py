#!/usr/bin/env python3
"""Spark anomaly analysis for bench cases (buggy + fix negative control).

Usage: .venv/bin/python scripts/run_analyze.py [--only id1,id2]
       [--mode blind|reported|both]

Runs find_anomaly in blind mode on the buggy AND fix clips of each
case (the fix clip is a negative control: nothing should look wrong
there), and optionally in reported mode, where each case's repro.json
"report" line is given to Spark to localize, or say it does not
happen. Writes analysis/<id>/<state>.json (blind) and
analysis/<id>/<state>.reported.json (reported), and prints a summary
table per mode. Stops before the projected Spark cost goes over $3.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from visiopatch.analyze import (  # noqa: E402
    ANOMALY_PROMPT_VERSION,
    REPORTED_PROMPT_VERSION,
    SPARK_MODEL,
    BudgetExceeded,
    anomaly_is_cached,
    find_anomaly,
    validate_anomaly_doc,
)
from visiopatch.bench import case_ids, read_meta  # noqa: E402

ANALYSIS = REPO_ROOT / "analysis"
BUDGET_USD = 3.0
EST_PER_CALL_USD = 0.05


def report_for(case_id: str) -> str:
    return str(read_meta(case_id)["report"])


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Spark anomaly analysis")
    parser.add_argument("--only", default="",
                        help="comma-separated case ids to run (default: all)")
    parser.add_argument("--mode", default="both",
                        choices=("blind", "reported", "both"),
                        help="which analysis modes to run")
    args = parser.parse_args()
    try:
        ids = case_ids(args.only)
    except ValueError as exc:
        print(f"{exc}", flush=True)
        return 1

    modes = ("blind", "reported") if args.mode == "both" else (args.mode,)
    jobs = [(cid, state, mode) for mode in modes
            for cid in ids for state in ("buggy", "fix")]
    spent = 0.0
    results: dict[tuple[str, str, str], dict] = {}

    for idx, (case_id, state, mode) in enumerate(jobs):
        report = report_for(case_id) if mode == "reported" else None
        remaining = jobs[idx:]

        def guard(_est: float, _remaining=remaining) -> None:
            uncached = sum(
                0 if anomaly_is_cached(
                    c, s, report_for(c) if m == "reported" else None)
                else 1 for c, s, m in _remaining)
            projected = spent + uncached * EST_PER_CALL_USD
            if projected > BUDGET_USD:
                raise BudgetExceeded(
                    f"projected cost ${projected:.4f} exceeds "
                    f"${BUDGET_USD:.2f} budget")

        try:
            result = find_anomaly(case_id, state, budget_check=guard,
                                  report=report)
        except BudgetExceeded as exc:
            print(f"STOP: {exc}", flush=True)
            return 1
        errs = validate_anomaly_doc(result)
        if errs:
            print(f"STOP: invalid anomaly doc for {case_id}/{state}/{mode}: "
                  + "; ".join(errs), flush=True)
            return 1
        spent += result["cost_usd"]
        if spent > BUDGET_USD:
            print(f"STOP: spent ${spent:.4f} exceeds ${BUDGET_USD:.2f} budget",
                  flush=True)
            return 1
        results[(case_id, state, mode)] = result
        suffix = ".reported.json" if mode == "reported" else ".json"
        dest = ANALYSIS / case_id / f"{state}{suffix}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        out = dict(result)
        out.update({
            "case_id": case_id, "state": state, "mode": mode,
            "model": SPARK_MODEL,
            "prompt_version": (REPORTED_PROMPT_VERSION
                               if mode == "reported" else ANOMALY_PROMPT_VERSION),
        })
        dest.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        print(f"[analyze:{mode}] {case_id}/{state}: anomaly={result['anomaly']} "
              f"frame={result['frame']} conf={result['confidence']} "
              f"loc={result['localization']} "
              f"cost=${result['cost_usd']:.4f}"
              + (" (cached)" if result["cached"] else ""), flush=True)

    for mode in modes:
        print(f"\n[{mode}]")
        print(f"{'case':<18} {'loc':<6} {'buggy':<6} {'fix':<6} {'frame':<6} "
              f"{'conf':<5} {'cost':<8} observed (buggy)")
        total_cost = 0.0
        false_positives = 0
        for case_id in ids:
            b = results[(case_id, "buggy", mode)]
            f = results[(case_id, "fix", mode)]
            cost = b["cost_usd"] + f["cost_usd"]
            total_cost += cost
            if f["anomaly"]:
                false_positives += 1
            frame = str(b["frame"]) if b["frame"] is not None else "-"
            observed = (b["observed"] or "")[:60].replace("\n", " ")
            print(f"{case_id:<18} {b['localization']:<6} {str(b['anomaly']):<6} "
                  f"{str(f['anomaly']):<6} {frame:<6} {b['confidence']:<5} "
                  f"${cost:<7.4f} {observed}")
        print(f"\n[{mode}] total estimated cost: ${total_cost:.4f}")
        print(f"[{mode}] fix-clip false positives: "
              f"{false_positives}/{len(ids)}")
    print(f"\nSpent this run: ${spent:.4f} (budget ${BUDGET_USD:.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
