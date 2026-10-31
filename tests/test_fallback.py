"""Offline tests for the noun-fallback chain in scripts/run_perceive.py.

perceive_job is exercised with a mocked perceive_fn: no browser, no API.
"""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "run_perceive", Path(__file__).resolve().parent.parent / "scripts" / "run_perceive.py")
run_perceive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_perceive)


def make_job(candidates):
    return {"case": "k1", "noun": "panel", "candidates": candidates,
            "mp4s": {"buggy": Path("b.mp4"), "fix": Path("f.mp4")},
            "duration_s": 5.0}


def fake_perceive(tracks_by_noun, log):
    def call(mp4, noun, dest, requested):
        log.append((str(mp4), noun, str(dest), requested))
        return {"frames": 83 if tracks_by_noun.get(noun, 0) else 0,
                "tracks": tracks_by_noun.get(noun, 0),
                "cost_usd": 0.0166, "cached": False, "noun": noun}
    return call


def test_stops_at_first_hit_in_order(tmp_path) -> None:
    log: list = []
    job = make_job(["panel", "rectangle", "frame"])
    winner, rows = run_perceive.perceive_job(
        job, tmp_path, fake_perceive({"frame": 2}, log), lambda n, e: None)
    assert winner == "frame"
    assert [r[1] for r in rows] == ["panel", "rectangle", "frame", "frame"]
    assert [r[0] for r in rows] == ["buggy", "buggy", "buggy", "fix"]


def test_fix_uses_winner_and_primary_filename(tmp_path) -> None:
    log: list = []
    job = make_job(["panel", "rectangle"])
    winner, rows = run_perceive.perceive_job(
        job, tmp_path, fake_perceive({"rectangle": 1}, log), lambda n, e: None)
    assert winner == "rectangle"
    fix_calls = [c for c in log if c[0] == "f.mp4"]
    assert len(fix_calls) == 1 and fix_calls[0][1] == "rectangle"
    assert fix_calls[0][2].endswith("fix.panel.json")
    assert all(c[3] == "panel" for c in log)  # requested noun passed through


def test_all_miss_means_no_fix_call(tmp_path) -> None:
    log: list = []
    job = make_job(["panel", "rectangle", "frame"])
    winner, rows = run_perceive.perceive_job(
        job, tmp_path, fake_perceive({}, log), lambda n, e: None)
    assert winner is None
    assert len(rows) == 3
    assert all(mp4 == "b.mp4" for mp4, _, _, _ in log)


def test_budget_guard_stops_before_call(tmp_path) -> None:
    import pytest as _pytest

    def guard(noun, est):
        raise run_perceive.BudgetExceeded("too rich")

    job = make_job(["panel"])
    with _pytest.raises(run_perceive.BudgetExceeded):
        run_perceive.perceive_job(job, tmp_path,
                                  fake_perceive({"panel": 1}, []), guard)
