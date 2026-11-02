"""Offline tests for visiopatch.rootcause — fake Wonder, no network.

Every Wonder reply comes from a fake callable with the same
(prompt, effort) -> (text, usage) shape as the live client.
"""

import json
import re
from pathlib import Path

import pytest

from visiopatch import rootcause
from visiopatch.rootcause import (
    build_diagnose_prompt,
    build_pick_prompt,
    diagnose,
    estimate_tokens,
    pick_files,
    sanitize_reported,
    trim_contents,
    validate_diagnose_doc,
    validate_pick_doc,
)

REPO = Path(__file__).resolve().parent.parent

BANNED_SUBSTRINGS = ["buggy", "not conserved", "cropped", "cut off",
                     "momentum", "z-index", "stacking context", "rounding"]

TREE = ["src/a.ts", "src/b.ts", "src/c.ts"]
REPORT = "the two squares move oddly after they touch"
REPORTED = {"anomaly": True, "frame": 45, "time_s": 2.8,
            "track_ids": ["0"], "bbox": [1, 2, 3, 4],
            "observed": "one square stops", "expected": "it keeps moving",
            "confidence": 0.9, "case_id": "kX", "state": "buggy",
            "mode": "reported", "model": "m", "cost_usd": 1.0}


def _assert_no_leak(prompt: str) -> None:
    low = prompt.lower()
    assert not re.search(r"\bfix(ed)?\b", low)
    assert not re.search(r"\bbug(gy)?\b", low)
    assert not re.search(r"[0-9a-f]{40}", low)
    for term in BANNED_SUBSTRINGS:
        assert term not in low
    for repro in (REPO / "bench").glob("*/repro.json"):
        meta = json.loads(repro.read_text(encoding="utf-8"))
        assert meta["buggy_sha"] not in prompt
        assert meta["fix_sha"] not in prompt
        assert repro.parent.name not in prompt


def test_sanitize_reported_strips_state_keys() -> None:
    clean = sanitize_reported(REPORTED)
    assert set(clean) == {"anomaly", "frame", "time_s", "track_ids",
                          "bbox", "observed", "expected", "confidence"}
    assert "kX" not in json.dumps(clean)


def test_prompts_contain_no_leak_terms() -> None:
    picked = [{"path": "src/a.ts", "why": "moves the squares"}]
    contents = {"src/a.ts": "--- src/a.ts (lines 1-2 of 2) ---\n1: x\n2: y"}
    for prompt in (build_pick_prompt(REPORT, sanitize_reported(REPORTED), TREE),
                   build_diagnose_prompt(REPORT, sanitize_reported(REPORTED),
                                         picked, contents)):
        _assert_no_leak(prompt)


def test_real_case_prompts_carry_no_shas_or_ids() -> None:
    # Real inputs: sanitized reported docs + real reports must not carry
    # revision hashes or case ids into a prompt (tree paths are inputs,
    # not scaffolding, so they are not word-checked here).
    for repro in sorted((REPO / "bench").glob("*/repro.json")):
        meta = json.loads(repro.read_text(encoding="utf-8"))
        reported = rootcause.load_reported(repro.parent.name)
        prompt = build_pick_prompt(str(meta["report"]), reported,
                                   ["src/a.ts"])
        assert meta["buggy_sha"] not in prompt
        assert meta["fix_sha"] not in prompt
        assert repro.parent.name not in prompt
        assert "buggy" not in json.dumps(reported).lower()


def test_pick_validation() -> None:
    good = {"files": [{"path": "src/a.ts", "why": "logic"}]}
    assert validate_pick_doc(good, TREE) == []
    assert validate_pick_doc({"files": []}, TREE) != []
    assert validate_pick_doc(
        {"files": [{"path": "src/zz.ts", "why": "x"}]}, TREE) != []
    assert validate_pick_doc(
        {"files": [{"path": "src/a.ts", "why": "x"}] * 2}, TREE) != []
    many = {"files": [{"path": p, "why": "x"} for p in TREE * 2]}
    assert validate_pick_doc(many, TREE) != []


STRATEGIES = [
    {"name": "s1", "files": ["src/a.ts"], "approach": "change the transfer"},
    {"name": "s2", "files": ["src/a.ts"], "approach": "exchange velocities"},
    {"name": "s3", "files": ["src/a.ts"], "approach": "resolve jointly"},
]


def _good_diag(**over) -> dict:
    doc = {"root_cause": "c", "file": "src/a.ts", "line_range": [2, 4],
           "suspect_function": "resolve", "mechanism": "m",
           "test_spec": "stationary object gains motion after contact",
           "fix_strategies": STRATEGIES, "confidence": 0.5}
    doc.update(over)
    return doc


def test_diagnose_validation() -> None:
    picked = ["src/a.ts"]
    counts = {"src/a.ts": 10}
    good = _good_diag()
    assert validate_diagnose_doc(good, picked, counts) == []
    assert validate_diagnose_doc(
        dict(good, file="src/b.ts"), picked, counts) != []
    assert validate_diagnose_doc(
        dict(good, line_range=[9, 99]), picked, counts) != []
    assert validate_diagnose_doc(
        dict(good, confidence=1.5), picked, counts) != []
    assert validate_diagnose_doc({"root_cause": "c"}, picked, counts) != []
    assert validate_diagnose_doc(
        dict(good, fix_strategies=STRATEGIES[:2]), picked, counts) != []
    dup = [dict(STRATEGIES[0]), dict(STRATEGIES[0]), dict(STRATEGIES[1])]
    assert validate_diagnose_doc(
        dict(good, fix_strategies=dup), picked, counts) != []
    bad_files = [dict(s, files=["src/zz.ts"]) for s in STRATEGIES]
    assert validate_diagnose_doc(
        dict(good, fix_strategies=bad_files), picked, counts) != []
    assert validate_diagnose_doc(
        dict(good, suspect_function=""), picked, counts) != []
    assert validate_diagnose_doc(
        dict(good, test_spec=""), picked, counts) != []


def test_trim_contents_stays_under_budget_and_keeps_order() -> None:
    contents = {f"src/f{i}.ts": "\n".join(f"line {j}" for j in range(500))
                for i in range(4)}
    trimmed = trim_contents(contents, token_budget=2000)
    assert list(trimmed) == list(contents)
    joined = "\n\n".join(trimmed.values())
    assert estimate_tokens(joined) <= 2100
    assert "lines 1-" in trimmed["src/f0.ts"]


def _fake(reply: str, log: list):
    def call(prompt: str, effort: str):
        log.append((prompt, effort))
        return reply, {"input_tokens": 100, "output_tokens": 50}
    return call


def _patch_case(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(rootcause, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(rootcause, "read_meta",
                        lambda case_id: {"report": REPORT})
    monkeypatch.setattr(rootcause, "load_reported",
                        lambda case_id: sanitize_reported(REPORTED))
    monkeypatch.setattr(rootcause, "source_tree", lambda case_id: TREE)


def test_pick_files_uses_medium_and_caches(tmp_path, monkeypatch) -> None:
    _patch_case(monkeypatch, tmp_path)
    log: list = []
    reply = json.dumps({"files": [{"path": "src/a.ts", "why": "logic"}]})
    doc = pick_files("kX", wonder_fn=_fake(reply, log))
    assert doc["files"][0]["path"] == "src/a.ts"
    assert log[0][1] == "medium"
    again = pick_files("kX", wonder_fn=_fake("garbage", log))
    assert again["files"] == doc["files"] and len(log) == 1
    assert again["cached"] is True and again["cost_usd"] == 0.0


def test_pick_files_rejects_invalid_reply(tmp_path, monkeypatch) -> None:
    _patch_case(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        pick_files("kX", wonder_fn=lambda p, e: ('{"files": []}', None))


def test_diagnose_uses_high_and_validates(tmp_path, monkeypatch) -> None:
    _patch_case(monkeypatch, tmp_path)
    body = "\n".join(f"code {i}" for i in range(20))
    monkeypatch.setattr(rootcause, "file_contents",
                        lambda case_id, paths: {p: body for p in paths})
    picked = [{"path": "src/a.ts", "why": "logic"}]
    log: list = []
    reply = json.dumps(_good_diag())
    doc = diagnose("kX", picked, wonder_fn=_fake(reply, log))
    assert doc["root_cause"] == "c" and log[0][1] == "high"
    assert doc["suspect_function"] == "resolve"
    assert len(doc["fix_strategies"]) == 3
    other = [{"path": "src/a.ts", "why": "other reason"}]
    with pytest.raises(ValueError):
        diagnose("kX", other, wonder_fn=lambda p, e: (
            json.dumps(_good_diag(line_range=[2, 999])), None))
