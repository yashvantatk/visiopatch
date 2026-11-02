#!/usr/bin/env python3
"""Gate 1: Wonder root cause + generated Playwright test per bench case.

Usage: .venv/bin/python scripts/run_gate1.py [--only id1,id2]

Per case: Wonder pick_files + diagnose (visiopatch.rootcause), then
Wonder writes one Playwright test (visiopatch.testgen). Gate 1 runs
that test against the current revision (it must FAIL) and against the
later revision served through the same URL (it must PASS). Only this
harness switches served state; Wonder never sees the later revision.
On a gate failure the test is rewritten with the test-runner output
from the current-revision run only, at most 3 attempts total per case.

Writes runs/<id>/rootcause.json and runs/<id>/gate1.json (committed;
raw model replies stay in the gitignored cache/). Stops before the
projected Wonder cost goes over $10 for the step.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from visiopatch.bench import (  # noqa: E402
    BENCH,
    ServedCase,
    case_git_dir,
    case_ids,
    read_meta,
    restore_buggy,
    sh,
    switch_to_fix,
)
from visiopatch.rootcause import (  # noqa: E402
    BudgetExceeded,
    diagnose,
    pick_files,
)
from visiopatch.testgen import (  # noqa: E402
    generate_test,
    validate_generated_code,
    write_generated_test,
)
from visiopatch.repair import sanitize_record  # noqa: E402

BUDGET_USD = 10.0
MAX_ATTEMPTS = 3
PYTEST_TIMEOUT_S = 180
FEEDBACK_CHARS = 4000


def _run_pytest(test_path: Path, url: str) -> tuple[int, str]:
    env = dict(os.environ, VISIOPATCH_CASE_URL=url)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(test_path)],
            cwd=REPO_ROOT, capture_output=True, text=True,
            timeout=PYTEST_TIMEOUT_S, env=env)
        return proc.returncode, (proc.stdout + proc.stderr)
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode() if isinstance(
            exc.stdout, bytes) else (exc.stdout or "")
        return 124, out + f"\npytest timed out after {PYTEST_TIMEOUT_S}s"


def suspect_file_hit(case: Path, meta: dict, picked_files: list,
                       diagnosed_file: str | None) -> tuple[bool, list[str]]:
    """Scoring only: did Wonder name a file the later revision changed?

    Uses git diff between the two revisions in the case repo. This runs
    in the harness only; its result never enters any prompt.
    """
    repo = case_git_dir(case)
    changed = sh("git", "-C", str(repo), "diff", "--name-only",
                 meta["buggy_sha"], meta["fix_sha"]).splitlines()
    suspects = {f["path"] for f in picked_files if isinstance(f, dict)}
    if diagnosed_file:
        suspects.add(diagnosed_file)
    return bool(suspects & set(changed)), sorted(changed)


def _ensure_buggy(case: Path, meta: dict) -> None:
    """Serve-side state must start at the current (first) revision."""
    if (case / "app" / ".git").is_dir():
        restore_buggy(case, meta, None)


def run_case(case_id: str, spent: list[float]) -> dict:
    case = BENCH / case_id
    meta = read_meta(case_id)
    url = str(meta["url"])
    duration_s = float(meta["duration_s"])

    def guard(est: float) -> None:
        if spent[0] + est > BUDGET_USD:
            raise BudgetExceeded(
                f"projected cost ${spent[0] + est:.4f} exceeds "
                f"${BUDGET_USD:.2f} budget")

    picked = pick_files(case_id, budget_check=guard)
    spent[0] += picked["cost_usd"]
    diagnosis = diagnose(case_id, picked["files"], budget_check=guard)
    spent[0] += diagnosis["cost_usd"]
    hit, changed_files = suspect_file_hit(
        case, meta, picked["files"], diagnosis.get("file"))
    rootcause_doc = {
        "case_id": case_id,
        "pick": picked,
        "diagnosis": diagnosis,
        "suspect_file_hit": hit,
        "changed_files": changed_files,
        "cost_usd": picked["cost_usd"] + diagnosis["cost_usd"],
    }
    runs_dir = REPO_ROOT / "runs" / case_id
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "rootcause.json").write_text(
        json.dumps(rootcause_doc, indent=2) + "\n", encoding="utf-8")
    print(f"[rootcause] {case_id}: files={[f['path'] for f in picked['files']]} "
          f"cause={diagnosis['root_cause'][:100]!r} "
          f"cost=${rootcause_doc['cost_usd']:.4f}", flush=True)

    attempts: list[dict] = []
    feedback: str | None = None
    gate_pass = False
    test_path: Path | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        rec: dict = {"attempt": attempt}
        try:
            gen = generate_test(case_id, diagnosis, budget_check=guard,
                                feedback=feedback)
        except ValueError as exc:
            rec.update({"stage": "validation", "gate_pass": False,
                        "error": str(exc)})
            attempts.append(rec)
            feedback = str(exc)
            print(f"[gate1] {case_id} attempt {attempt}: invalid test: "
                  f"{exc}", flush=True)
            continue
        spent[0] += gen["cost_usd"]
        rec["testgen_cost_usd"] = gen["cost_usd"]
        rec["testgen_cached"] = gen["cached"]
        test_path = write_generated_test(case_id, gen["code"])
        errs = validate_generated_code(gen["code"], duration_s)
        if errs:
            rec.update({"stage": "validation", "gate_pass": False,
                        "error": "; ".join(errs)})
            attempts.append(rec)
            feedback = "; ".join(errs)
            continue

        _ensure_buggy(case, meta)
        saved = None
        with ServedCase(case):
            buggy_exit, buggy_out = _run_pytest(test_path, url)
            rec["buggy_exit"] = buggy_exit
            rec["buggy_output_tail"] = buggy_out[-2000:]
            fix_exit = None
            fix_out = ""
            if buggy_exit != 0:
                saved = switch_to_fix(case, meta)
                try:
                    fix_exit, fix_out = _run_pytest(test_path, url)
                finally:
                    restore_buggy(case, meta, saved)
            rec["fix_exit"] = fix_exit
        rec["gate_pass"] = buggy_exit != 0 and fix_exit == 0
        rec["stage"] = "gate"
        attempts.append(rec)
        print(f"[gate1] {case_id} attempt {attempt}: "
              f"buggy_exit={buggy_exit} fix_exit={fix_exit} "
              f"pass={rec['gate_pass']} cost=${spent[0]:.4f}", flush=True)
        if rec["gate_pass"]:
            gate_pass = True
            break
        # Retry feedback comes from the current-revision run only.
        feedback = buggy_out[-FEEDBACK_CHARS:]

    gate_doc = {
        "case_id": case_id,
        "gate_pass": gate_pass,
        "attempts": attempts,
        "attempt_count": len(attempts),
        "test_file": str(test_path.relative_to(REPO_ROOT)) if test_path else None,
        "rootcause_cost_usd": rootcause_doc["cost_usd"],
        "total_cost_usd": rootcause_doc["cost_usd"] + sum(
            a.get("testgen_cost_usd", 0.0) for a in attempts),
    }
    gate_doc = sanitize_record(gate_doc)
    (runs_dir / "gate1.json").write_text(
        json.dumps(gate_doc, indent=2) + "\n", encoding="utf-8")
    return gate_doc


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Gate 1 run")
    parser.add_argument("--only", default="",
                        help="comma-separated case ids (default: all)")
    args = parser.parse_args()
    try:
        ids = case_ids(args.only)
    except ValueError as exc:
        print(f"{exc}", flush=True)
        return 1
    spent = [0.0]
    results = {}
    for case_id in ids:
        try:
            results[case_id] = run_case(case_id, spent)
        except BudgetExceeded as exc:
            print(f"STOP: {exc}", flush=True)
            return 1
        if spent[0] > BUDGET_USD:
            print(f"STOP: spent ${spent[0]:.4f} exceeds ${BUDGET_USD:.2f}",
                  flush=True)
            return 1
    print(f"\n{'case':<18} {'gate1':<6} {'attempts':<9} cost")
    for case_id in ids:
        r = results[case_id]
        print(f"{case_id:<18} {str(r['gate_pass']):<6} "
              f"{r['attempt_count']:<9} ${r['total_cost_usd']:.4f}")
    print(f"\nSpent this run: ${spent[0]:.4f} (budget ${BUDGET_USD:.2f})")
    return 0 if all(r["gate_pass"] for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
