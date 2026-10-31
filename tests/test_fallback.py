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


def fake_spark(nouns, log):
    def call(mp4):
        log.append(str(mp4))
        return {"nouns": nouns}
    return call


def test_spark_nouns_tried_only_after_fallbacks_run_out(tmp_path) -> None:
    log: list = []
    spark_log: list = []
    job = make_job(["panel", "rectangle"])
    winner, rows = run_perceive.perceive_job(
        job, tmp_path, fake_perceive({"square": 2}, log), lambda n, e: None,
        spark_fn=fake_spark(["square", "circle"], spark_log))
    assert winner == "square"
    assert spark_log == ["b.mp4"]  # asked once, about the buggy clip
    assert [(r[0], r[1], r[3]) for r in rows] == [
        ("buggy", "panel", "repro"), ("buggy", "rectangle", "fallback"),
        ("buggy", "square", "spark"), ("fix", "square", "spark")]


def test_spark_not_called_when_fallback_hits(tmp_path) -> None:
    spark_log: list = []
    job = make_job(["panel", "rectangle"])
    winner, rows = run_perceive.perceive_job(
        job, tmp_path, fake_perceive({"rectangle": 1}, []),
        lambda n, e: None,
        spark_fn=fake_spark(["square"], spark_log))
    assert winner == "rectangle"
    assert spark_log == []
    assert rows[-1][3] == "fallback"


def test_spark_nouns_deduped_and_first_hit_wins(tmp_path) -> None:
    log: list = []
    job = make_job(["panel"])
    winner, rows = run_perceive.perceive_job(
        job, tmp_path, fake_perceive({"circle": 1}, log),
        lambda n, e: None,
        spark_fn=fake_spark(["panel", "square", "circle"], []))
    assert winner == "circle"
    # "panel" from Spark was already tried as the repro noun: not retried.
    assert [c[1] for c in log].count("panel") == 1
    assert [(r[1], r[3]) for r in rows] == [
        ("panel", "repro"), ("square", "spark"),
        ("circle", "spark"), ("circle", "spark")]


def test_spark_all_miss_means_no_fix_call(tmp_path) -> None:
    log: list = []
    job = make_job(["panel"])
    winner, rows = run_perceive.perceive_job(
        job, tmp_path, fake_perceive({}, log), lambda n, e: None,
        spark_fn=fake_spark(["square"], []))
    assert winner is None
    assert all(r[0] == "buggy" for r in rows)


def test_budget_guard_stops_before_call(tmp_path) -> None:
    import pytest as _pytest

    def guard(noun, est):
        raise run_perceive.BudgetExceeded("too rich")

    job = make_job(["panel"])
    with _pytest.raises(run_perceive.BudgetExceeded):
        run_perceive.perceive_job(job, tmp_path,
                                  fake_perceive({"panel": 1}, []), guard)
