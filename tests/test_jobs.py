"""Offline tests for visiopatch.jobs — no model or network calls."""

import json
import time

import pytest

from visiopatch import jobs as JOB


def test_create_and_load_roundtrip():
    rec = JOB.create_job("d1-modal-zindex", "diagnose", {"target": "x"})
    assert rec["status"] == "running"
    assert rec["job_id"].startswith("d1-modal-zindex.")
    loaded = JOB.load_job(rec["job_id"])
    assert loaded["job_id"] == rec["job_id"]
    assert loaded["result"] is None


def test_parse_job_id_rejects_malformed():
    with pytest.raises(ValueError):
        JOB.parse_job_id("nohex")


def test_load_unknown_job_raises():
    with pytest.raises(KeyError):
        JOB.load_job("d1-modal-zindex.deadbeef0000")


def test_finish_and_fail_persist_to_disk():
    rec = JOB.create_job("d1-modal-zindex", "diagnose", {})
    JOB.finish_job(rec, {"answer": 1})
    reloaded = JOB.load_job(rec["job_id"])
    assert reloaded["status"] == "done"
    assert reloaded["result"] == {"answer": 1}
    rec2 = JOB.create_job("d1-modal-zindex", "diagnose", {})
    JOB.fail_job(rec2, "boom")
    assert JOB.load_job(rec2["job_id"])["status"] == "failed"
    assert "boom" in JOB.load_job(rec2["job_id"])["error"]


def test_add_pids_dedupes_and_persists():
    rec = JOB.create_job("d1-modal-zindex", "repair_and_verify", {})
    JOB.add_pids(rec, [111, 222])
    JOB.add_pids(rec, [222, 333])
    assert JOB.load_job(rec["job_id"])["pids"] == [111, 222, 333]


def test_run_in_background_success_and_failure():
    rec = JOB.create_job("d1-modal-zindex", "diagnose", {})
    thread = JOB.run_in_background(rec, lambda: {"ok": True})
    thread.join(timeout=10)
    assert JOB.load_job(rec["job_id"])["status"] == "done"
    rec2 = JOB.create_job("d1-modal-zindex", "diagnose", {})
    def _boom():
        raise RuntimeError("kaput")
    thread2 = JOB.run_in_background(rec2, _boom)
    thread2.join(timeout=10)
    failed = JOB.load_job(rec2["job_id"])
    assert failed["status"] == "failed"
    assert "kaput" in failed["error"]


def test_needs_background_minimum_set():
    for tool in ("diagnose", "generate_visual_test", "repair_and_verify"):
        assert JOB.needs_background(tool)
    for tool in ("capture_bug", "track_objects", "find_anomaly",
                 "job_status", "job_result"):
        assert not JOB.needs_background(tool)


def test_tool_timeout_env(monkeypatch):
    monkeypatch.delenv("VISIOPATCH_TOOL_TIMEOUT", raising=False)
    assert JOB.tool_timeout_s() is None
    monkeypatch.setenv("VISIOPATCH_TOOL_TIMEOUT", "90")
    assert JOB.tool_timeout_s() == 90.0
    monkeypatch.setenv("VISIOPATCH_TOOL_TIMEOUT", "nonsense")
    assert JOB.tool_timeout_s() is None


def test_job_survives_restart():
    rec = JOB.create_job("d1-modal-zindex", "diagnose", {"a": 1})
    JOB.finish_job(rec, {"cache": "hit"})
    raw = json.loads((JOB.jobs_dir("d1-modal-zindex")
                      / f"{rec['job_id']}.json").read_text())
    assert raw["status"] == "done"
    assert time.time() - raw["updated_at"] < 60
