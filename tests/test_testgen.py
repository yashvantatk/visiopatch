"""Offline tests for visiopatch.testgen — fake Wonder, no network."""

import json
import re
from pathlib import Path

import pytest

from visiopatch import testgen
from visiopatch.testgen import (
    assertion_strategy,
    build_testgen_prompt,
    extract_code,
    generate_test,
    validate_generated_code,
)

REPO = Path(__file__).resolve().parent.parent

GOOD_CODE = '''import os
from playwright.sync_api import sync_playwright
from visiopatch.browser import launch_chromium


def test_reported_symptom():
    url = os.environ["VISIOPATCH_CASE_URL"]
    with sync_playwright() as pw:
        browser = launch_chromium(pw)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto(url)
            page.wait_for_timeout(500)
            box = page.evaluate("() => document.body.getBoundingClientRect().x")
            assert box >= 0
        finally:
            browser.close()
'''

REPORT = "the two squares move oddly after they touch"
REPORTED = {"anomaly": True, "frame": 45, "time_s": 2.8,
            "track_ids": ["0"], "bbox": [1, 2, 3, 4],
            "observed": "one square stops", "expected": "it keeps moving",
            "confidence": 0.9}
DIAGNOSIS = {"root_cause": "velocity is not transferred",
             "file": "src/a.ts", "line_range": [1, 3],
             "suspect_function": "resolve",
             "mechanism": "impulse is dropped",
             "test_spec": "stationary square gains rightward motion "
                          "after contact",
             "confidence": 0.8}


def test_prompt_contains_no_leak_terms() -> None:
    prompt = build_testgen_prompt(REPORT, REPORTED, DIAGNOSIS,
                                  "http://localhost:8901/", 5,
                                  assertion_strategy("k1174"))
    assert "test_spec" in prompt
    assert "stationary square gains" in prompt
    low = prompt.lower()
    assert not re.search(r"\bfix(ed)?\b", low)
    assert not re.search(r"\bbug(gy)?\b", low)
    assert not re.search(r"[0-9a-f]{40}", low)
    for repro in (REPO / "bench").glob("*/repro.json"):
        meta = json.loads(repro.read_text(encoding="utf-8"))
        assert meta["buggy_sha"] not in prompt
        assert meta["fix_sha"] not in prompt
        assert repro.parent.name not in prompt


def test_retry_prompt_carries_only_given_feedback() -> None:
    prompt = build_testgen_prompt(REPORT, REPORTED, DIAGNOSIS,
                                  "http://localhost:8901/", 5,
                                  assertion_strategy("k1174"),
                                  feedback="assert 1 == 2 failed")
    assert "assert 1 == 2 failed" in prompt


def test_strategy_families() -> None:
    assert "DOM geometry" in assertion_strategy("d1-modal-zindex")
    assert "canvas" in assertion_strategy("k1174")


def test_extract_code_strips_fences() -> None:
    assert extract_code("```python\nx = 1\n```") == "x = 1\n"
    assert extract_code("x = 1") == "x = 1\n"


def test_validate_generated_code_accepts_good() -> None:
    assert validate_generated_code(GOOD_CODE, 5) == []


def test_validate_generated_code_rejects_bad() -> None:
    assert validate_generated_code("def test_reported_symptom(:", 5) != []
    assert validate_generated_code(
        GOOD_CODE.replace("assert box >= 0", "pass"), 5) != []
    assert validate_generated_code(
        GOOD_CODE + "\nimport requests\n", 5) != []
    assert validate_generated_code(
        GOOD_CODE.replace("page.wait_for_timeout(500)",
                          "page.wait_for_timeout(99999)"), 5) != []
    assert validate_generated_code(
        GOOD_CODE.replace("launch_chromium(pw)", "pw.chromium.launch()"),
        5) != []
    assert validate_generated_code(
        GOOD_CODE.replace("launch_chromium(pw)",
                          "launch_chromium(pw.chromium)"), 5) != []
    assert validate_generated_code(
        GOOD_CODE.replace("def test_reported_symptom():",
                          "def test_other():"), 5) != []
    two = GOOD_CODE + "\n\ndef test_extra():\n    assert True\n"
    assert validate_generated_code(two, 5) != []


def _patch_case(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(testgen, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(testgen, "read_meta", lambda case_id: {
        "report": REPORT, "duration_s": 5, "url": "http://localhost:8901/"})
    monkeypatch.setattr(testgen, "load_reported", lambda case_id: REPORTED)


def test_generate_test_uses_high_and_caches(tmp_path, monkeypatch) -> None:
    _patch_case(monkeypatch, tmp_path)
    log: list = []

    def fake(prompt: str, effort: str):
        log.append((prompt, effort))
        return "```python\n" + GOOD_CODE + "```", {
            "input_tokens": 100, "output_tokens": 50}

    doc = generate_test("k1174", DIAGNOSIS, wonder_fn=fake)
    assert doc["code"].startswith("import os")
    assert log[0][1] == "high"
    again = generate_test("k1174", DIAGNOSIS,
                          wonder_fn=lambda p, e: ("garbage", None))
    assert again["cached"] is True and len(log) == 1


def test_generate_test_feedback_changes_call(tmp_path, monkeypatch) -> None:
    _patch_case(monkeypatch, tmp_path)
    log: list = []

    def fake(prompt: str, effort: str):
        log.append(prompt)
        return GOOD_CODE, {"input_tokens": 10, "output_tokens": 10}

    generate_test("k1174", DIAGNOSIS, wonder_fn=fake)
    generate_test("k1174", DIAGNOSIS, wonder_fn=fake,
                  feedback="assert failed on current page")
    assert len(log) == 2 and "assert failed" in log[1]


def test_generate_test_rejects_invalid_code(tmp_path, monkeypatch) -> None:
    _patch_case(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        generate_test("k1174", DIAGNOSIS,
                      wonder_fn=lambda p, e: ("assert True\n", None))
