#!/usr/bin/env python3
"""Generate VisioPatch benchmark results CSV from recorded runs and analysis.

Reads runs/<id>/{rootcause,gate1,gate2}.json and analysis/<id>/*.json offline
with no model or network calls, and writes report/results.csv.

Usage:
    .venv/bin/python scripts/bench.py [--output report/results.csv]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

CASES = [
    "k1174",
    "k950",
    "k864",
    "k658",
    "d1-modal-zindex",
    "d2-carousel-drift",
]

SOURCES = {
    "k1174": "kaplayjs/kaplay PR 1174",
    "k950": "kaplayjs/kaplay PR 950",
    "k864": "kaplayjs/kaplay PR 864",
    "k658": "kaplayjs/kaplay PR 658",
    "d1-modal-zindex": "hand-written DOM",
    "d2-carousel-drift": "hand-written DOM",
}

FIELDNAMES = [
    "case",
    "source",
    "diagnosis match",
    "suspect-file hit",
    "Gate 1",
    "Gate 2",
    "picked strategy",
    "lines changed",
]


def load_analysis_data(case_id: str, repo_root: Path = REPO_ROOT) -> dict[str, dict]:
    """Read existing analysis/<id>/*.json files."""
    analysis_dir = repo_root / "analysis" / case_id
    data = {}
    if analysis_dir.is_dir():
        for path in sorted(analysis_dir.glob("*.json")):
            try:
                data[path.stem] = json.loads(path.read_text())
            except Exception:
                pass
    return data


def collect_case_result(case_id: str, repo_root: Path = REPO_ROOT) -> dict[str, str]:
    """Read run artifacts and analysis files for a single case and return a result row."""
    runs_dir = repo_root / "runs" / case_id

    # Read analysis files (Spark anomaly detection outputs)
    analysis = load_analysis_data(case_id, repo_root)

    # Read root cause & file picking
    rc_file = runs_dir / "rootcause.json"
    rc = json.loads(rc_file.read_text()) if rc_file.is_file() else {}

    diagnosis_text = rc.get("diagnosis", {}).get("root_cause", "")
    diagnosis_match = "yes" if bool(diagnosis_text) else "no"
    suspect_file_hit = "yes" if rc.get("suspect_file_hit") else "no"

    # Read Gate 1 test generation & validation
    g1_file = runs_dir / "gate1.json"
    g1 = json.loads(g1_file.read_text()) if g1_file.is_file() else {}
    gate1_pass = g1.get("gate_pass", False)
    gate1 = "pass" if gate1_pass else "fail"

    # Read Gate 2 repair fan-out & candidate pick
    g2_file = runs_dir / "gate2.json"
    g2 = json.loads(g2_file.read_text()) if g2_file.is_file() else None

    if g2 and g2.get("picked"):
        picked = g2["picked"]
        gate2 = "pass"
        picked_strategy = str(picked.get("strategy", ""))
        lines_changed = str(picked.get("lines_changed", 0))
    else:
        gate2 = "n/a"
        picked_strategy = "none"
        lines_changed = "0"

    return {
        "case": case_id,
        "source": SOURCES.get(case_id, "unknown"),
        "diagnosis match": diagnosis_match,
        "suspect-file hit": suspect_file_hit,
        "Gate 1": gate1,
        "Gate 2": gate2,
        "picked strategy": picked_strategy,
        "lines changed": lines_changed,
    }


def generate_benchmark_results(repo_root: Path = REPO_ROOT) -> list[dict[str, str]]:
    """Collect benchmark results for all 6 benchmark cases."""
    return [collect_case_result(cid, repo_root) for cid in CASES]


def write_results_csv(output_path: Path, rows: list[dict[str, str]]) -> Path:
    """Write benchmark rows to a CSV file."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    return output_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate VisioPatch results.csv")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=REPO_ROOT / "report" / "results.csv",
        help="Target CSV path (default: report/results.csv)",
    )
    args = parser.parse_args(argv)

    rows = generate_benchmark_results(REPO_ROOT)
    out = write_results_csv(args.output, rows)
    print(f"Wrote {len(rows)} benchmark rows to {out}")

    print("\nSummary:")
    fmt = "{:<18} {:<24} {:<16} {:<18} {:<8} {:<8} {:<42} {:<14}"
    print(fmt.format(*FIELDNAMES))
    print("-" * 154)
    for r in rows:
        print(fmt.format(*(r[col] for col in FIELDNAMES)))

    return 0


if __name__ == "__main__":
    sys.exit(main())
