"""Offline unit tests for scripts/bench.py (results.csv generation).

No network or model calls.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from scripts.bench import (
    CASES,
    FIELDNAMES,
    SOURCES,
    generate_benchmark_results,
    main,
    write_results_csv,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_cases_list():
    assert len(CASES) == 6
    assert CASES == [
        "k1174",
        "k950",
        "k864",
        "k658",
        "d1-modal-zindex",
        "d2-carousel-drift",
    ]


def test_generate_benchmark_results():
    rows = generate_benchmark_results(REPO_ROOT)
    assert len(rows) == 6

    by_case = {r["case"]: r for r in rows}

    # Diagnosis 6/6 and Suspect-file hit 6/6
    for cid in CASES:
        assert cid in by_case
        assert by_case[cid]["diagnosis match"] == "yes"
        assert by_case[cid]["suspect-file hit"] == "yes"
        assert by_case[cid]["source"] == SOURCES[cid]

    # Gate 1: 4/6 pass
    passing_gate1 = [cid for cid in CASES if by_case[cid]["Gate 1"] == "pass"]
    failing_gate1 = [cid for cid in CASES if by_case[cid]["Gate 1"] == "fail"]
    assert sorted(passing_gate1) == sorted(["k1174", "k950", "k864", "d1-modal-zindex"])
    assert sorted(failing_gate1) == sorted(["k658", "d2-carousel-drift"])

    # Gate 2: 4/4 of Gate 1 passes pass Gate 2
    for cid in passing_gate1:
        assert by_case[cid]["Gate 2"] == "pass"
        assert int(by_case[cid]["lines changed"]) > 0
        assert by_case[cid]["picked strategy"] != "none"

    for cid in failing_gate1:
        assert by_case[cid]["Gate 2"] == "n/a"
        assert by_case[cid]["lines changed"] == "0"
        assert by_case[cid]["picked strategy"] == "none"

    # Specific strategy checks
    assert "Momentum" in by_case["k1174"]["picked strategy"]
    assert by_case["k1174"]["lines changed"] == "35"

    assert "stacking context" in by_case["d1-modal-zindex"]["picked strategy"]
    assert by_case["d1-modal-zindex"]["lines changed"] == "2"

    assert "degenerate slice" in by_case["k864"]["picked strategy"]
    assert by_case["k864"]["lines changed"] == "2"

    assert "atlas rasterization" in by_case["k950"]["picked strategy"]
    assert by_case["k950"]["lines changed"] == "69"


def test_write_results_csv(tmp_path: Path):
    out_csv = tmp_path / "results.csv"
    rows = generate_benchmark_results(REPO_ROOT)
    res_path = write_results_csv(out_csv, rows)
    assert res_path.is_file()

    with open(res_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == FIELDNAMES
        read_rows = list(reader)
        assert len(read_rows) == 6
        assert [r["case"] for r in read_rows] == CASES


def test_main_cli(tmp_path: Path):
    target = tmp_path / "sub" / "custom_results.csv"
    ret = main(["--output", str(target)])
    assert ret == 0
    assert target.is_file()
