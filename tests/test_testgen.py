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
            assert page.evaluate("() => !!document.body")
            box = page.evaluate("() => document.body.getBoundingClientRect().x")
            assert box is not None
            print("VISIOPATCH_PHASE_A_OK")
            assert box >= 0, "VISIOPATCH_PHASE_B: body left edge moved"
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
        GOOD_CODE.replace('assert box >= 0, "VISIOPATCH_PHASE_B',
                          'pass  # VISIOPATCH_PHASE_B'), 5) != []
    assert validate_generated_code(
        GOOD_CODE.replace('print("VISIOPATCH_PHASE_A_OK")', "pass"), 5) != []
    one_assert = ("import os\n"
                  "URL = os.environ['VISIOPATCH_CASE_URL']\n"
                  "def test_reported_symptom():\n"
                  "    assert URL.startswith('http')\n")
    assert validate_generated_code(one_assert, 5) != []
    assert validate_generated_code(
        GOOD_CODE + "\nimport requests\n", 5) != []
    assert validate_generated_code(
        GOOD_CODE.replace('page.goto(url)',
                          'page.goto(url)\n            page.wait_for_timeout(99999)'),
        5) != []
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


def test_state_label_check_ignores_normal_words() -> None:
    """Acceptance attempt 2 false positive: 'bug' in an ordinary comment
    and 'fixed' as a CSS value are normal words, not state labels."""
    code = GOOD_CODE + (
        '# wait so the bug would appear\n'
        'pos = page.evaluate(\n'
        '    "() => getComputedStyle(document.body).position")\n'
        'assert pos in ("static", "fixed", "sticky"), \\\n'
        '    "VISIOPATCH_PHASE_B: unexpected position"\n'
    )
    assert validate_generated_code(code, 5) == []


def test_state_label_check_catches_labels() -> None:
    """Real leaks still fail, naming the word and its line."""
    leak_id = GOOD_CODE.replace("box = page.evaluate", "buggy = page.evaluate")
    errs = validate_generated_code(leak_id, 5)
    assert any("'buggy'" in e and "line" in e for e in errs), errs
    leak_phrase = GOOD_CODE.replace(
        "body left edge moved", "body left edge moved on fix build")
    errs = validate_generated_code(leak_phrase, 5)
    assert any("fix build" in e and "line" in e for e in errs), errs


def test_generate_test_rejects_invalid_code(tmp_path, monkeypatch) -> None:
    _patch_case(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        generate_test("k1174", DIAGNOSIS,
                      wonder_fn=lambda p, e: ("assert True\n", None))


# --- two-phase Gate 1 --------------------------------------------------------

def test_gate1_verdict_needs_phase_a_then_assertion_error() -> None:
    from visiopatch.testgen import gate1_verdict
    passing = "VISIOPATCH_PHASE_A_OK\n1 passed"
    assert gate1_verdict(0, passing)["gate_pass"] is False
    no_a = ("FAILED - AssertionError: VISIOPATCH_PHASE_B: x\n"
            "1 failed")
    assert gate1_verdict(1, no_a)["gate_pass"] is False
    timeout = ("VISIOPATCH_PHASE_A_OK\n"
               "E TimeoutError: waiting for selector\n"
               "1 failed")
    assert gate1_verdict(1, timeout)["gate_pass"] is False
    wrong_err = ("VISIOPATCH_PHASE_A_OK\n"
                 "E RuntimeError: boom\n"
                 "1 failed")
    assert gate1_verdict(1, wrong_err)["gate_pass"] is False
    good = ("VISIOPATCH_PHASE_A_OK\n"
            "E AssertionError: VISIOPATCH_PHASE_B: title hidden\n"
            "1 failed")
    verdict = gate1_verdict(1, good)
    assert verdict["gate_pass"] is True


def test_generate_repo_test_matches_step5_shape(tmp_path, monkeypatch) -> None:
    """The live MCP repo path must ask for Python code exactly like Step 5
    (code-shaped reply + extract_code), not a {"code": ...} JSON doc: the
    old _wonder_test raised "testgen-repo reply is not JSON" on the valid
    code reply every live run produced."""
    from visiopatch.testgen import generate_repo_test
    monkeypatch.setattr(testgen, "CACHE_DIR", tmp_path / "cache")
    log: list = []

    def fake(prompt: str, effort: str):
        log.append((prompt, effort))
        return "```python\n" + GOOD_CODE + "```", {
            "input_tokens": 100, "output_tokens": 50}

    doc = generate_repo_test(
        "the popup covers the button", {}, DIAGNOSIS,
        "http://localhost:9/", 5.0, "strategy", None,
        wonder_fn=fake, raw_dir=tmp_path / "jobs")
    assert doc["code"].startswith("import os")
    assert log[0][1] == "high"
    assert "VISIOPATCH_CASE_URL" in log[0][0]
    again = generate_repo_test(
        "the popup covers the button", {}, DIAGNOSIS,
        "http://localhost:9/", 5.0, "strategy", None,
        wonder_fn=lambda p, e: ("garbage", None),
        raw_dir=tmp_path / "jobs")
    assert again["cached"] is True and len(log) == 1


def test_generate_repo_test_logs_scrubbed_raw500(tmp_path, monkeypatch) -> None:
    """The first 500 chars of the raw reply land in the job dir with any
    secret redacted, so a failed live call can be diagnosed offline."""
    from visiopatch.testgen import generate_repo_test
    monkeypatch.setattr(testgen, "CACHE_DIR", tmp_path / "cache")
    secret = "MODEL_API_KEY=TOPSECRET123"
    reply = "```python\n" + GOOD_CODE.replace(
        "box >= 0", f"box >= 0  # {secret}") + "\n```"
    raw_dir = tmp_path / "jobs"
    with pytest.raises(ValueError):
        generate_repo_test(
            "rep", {}, DIAGNOSIS, "http://localhost:9/", 5.0, "s", None,
            wonder_fn=lambda p, e: (reply, None), raw_dir=raw_dir)
    raws = list(raw_dir.glob("*_raw500.txt"))
    assert len(raws) == 1
    body = raws[0].read_text(encoding="utf-8")
    assert len(body) <= 500
    assert "TOPSECRET123" not in body
    assert "MODEL_API_KEY" not in body
