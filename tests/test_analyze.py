"""Offline tests for visiopatch.analyze — recorded fixtures, fake Spark.

No network, no API key: every Spark reply comes from a fixture or a fake
callable with the same (mp4, prompt) -> (text, usage) shape.
"""

import json
import re
from pathlib import Path

import pytest

from visiopatch import analyze
from visiopatch.analyze import (
    build_anomaly_prompt,
    build_noun_prompt,
    extract_json,
    find_anomaly,
    frame_to_page_time,
    propose_nouns,
    summarize_tracks,
    validate_anomaly_doc,
    validate_nouns_doc,
)

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parent.parent

BANNED_SUBSTRINGS = ["buggy", "not conserved", "cropped", "cut off"]


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --- schema validation ------------------------------------------------------

def test_fixtures_validate_clean() -> None:
    assert validate_nouns_doc(_load("spark_nouns_sample.json")) == []
    assert validate_anomaly_doc(_load("spark_anomaly_sample.json")) == []
    assert validate_anomaly_doc(
        _load("spark_anomaly_negative_sample.json")) == []


def test_nouns_validation_rejects_bad_docs() -> None:
    assert validate_nouns_doc({}) != []
    assert validate_nouns_doc({"nouns": ["box", "ball"]}) != []  # need 3
    assert validate_nouns_doc(
        {"nouns": ["box", "box", "ball"]}) != []  # duplicate
    assert validate_nouns_doc(
        {"nouns": ["a very long noun phrase here", "ball", "box"]}) != []


def test_anomaly_validation_rejects_bad_docs() -> None:
    good = _load("spark_anomaly_sample.json")
    assert validate_anomaly_doc({}) != []
    bad = dict(good, confidence=1.5)
    assert validate_anomaly_doc(bad) != []
    bad = dict(good, bbox=[1200, 700, 200, 100])  # leaves 1280x720
    assert validate_anomaly_doc(bad) != []
    bad = dict(good, frame=None)
    assert validate_anomaly_doc(bad) != []
    neg = _load("spark_anomaly_negative_sample.json")
    bad = dict(neg, bbox=[0, 0, 10, 10])  # must be null when no anomaly
    assert validate_anomaly_doc(bad) != []


def test_extract_json_tolerates_fences() -> None:
    assert extract_json('```json\n{"nouns": []}\n```') == {"nouns": []}
    with pytest.raises(ValueError):
        extract_json("not json at all")


def test_extract_json_finds_fenced_block_in_prose() -> None:
    """Live replies often wrap the JSON in prose ("Here is..."). The
    parser must find the fenced block anywhere, not only when the whole
    reply is the fence (Step 7 live-path failure)."""
    reply = ("Here is the result:\n```json\n{\"nouns\": [\"a\"]}\n```\n"
             "Hope this helps.")
    assert extract_json(reply) == {"nouns": ["a"]}


def test_extract_json_empty_reply_raises() -> None:
    for bad in ("", "   ", "```\n```"):
        with pytest.raises(ValueError):
            extract_json(bad)


# --- prompts: no leakage, explicit negative control -------------------------

def _assert_no_leak(prompt: str) -> None:
    low = prompt.lower()
    assert not re.search(r"\bfix(ed)?\b", low)
    assert not re.search(r"\bbug(gy)?\b", low)
    assert not re.search(r"[0-9a-f]{40}", low)  # no commit SHAs
    for term in BANNED_SUBSTRINGS:
        assert term not in low
    for repro in (REPO / "bench").glob("*/repro.json"):
        meta = json.loads(repro.read_text(encoding="utf-8"))
        assert meta["buggy_sha"] not in prompt
        assert meta["fix_sha"] not in prompt
        assert repro.parent.name not in prompt


def test_prompts_contain_no_leak_terms() -> None:
    summary = {"tracks": [{"track_id": "0", "noun": "square", "samples": [
        {"frame": 0, "bbox": [10, 20, 30, 40], "area": 400}]}]}
    for prompt in (build_noun_prompt(), build_anomaly_prompt(summary),
                   build_anomaly_prompt(None)):
        _assert_no_leak(prompt)


def test_anomaly_prompt_allows_negative_answer() -> None:
    prompt = build_anomaly_prompt(None).lower()
    assert "nothing looking wrong is a valid answer" in prompt
    assert "anomaly as false" in prompt
    assert "must be wrong" not in prompt.replace(
        "do not assume something must be wrong", "")


def test_blind_prompt_v2_rules() -> None:
    prompt = build_anomaly_prompt(None).lower()
    assert "persists across" in prompt  # persistence, not a single moment
    assert "agree with the tracked-object" in prompt  # tracks agreement
    assert "trust" in prompt and "motion" in prompt  # motion over numbers


def test_reported_prompt_quotes_report_and_allows_no() -> None:
    report = "the popup's top part is hidden when it opens"
    prompt = build_anomaly_prompt(None, report=report)
    assert f"A user reported: {report}" in prompt
    low = prompt.lower()
    assert "or say it does not happen" in low
    assert "does not happen is a valid answer" in low
    _assert_no_leak(prompt)


def test_noun_prompt_asks_exactly_three() -> None:
    assert "exactly 3" in build_noun_prompt()


# --- time maths ---------------------------------------------------------------

def test_frame_to_page_time_uses_load_offset() -> None:
    assert frame_to_page_time(30, 0.14) == 1.86  # 30/15 - 0.14
    assert frame_to_page_time(0, 0.14) == -0.14  # before page load
    assert frame_to_page_time(83, 0.0) == round(83 / 15, 3)


# --- tracks summary -----------------------------------------------------------

def test_summarize_tracks_is_per_second_not_per_frame() -> None:
    doc = {"noun": "square", "frames": [
        {"frame": f, "track_id": "0", "bbox": [f, 10, f + 20, 40],
         "mask_area": 600} for f in range(31)]}
    summary = summarize_tracks([doc])
    samples = summary["tracks"][0]["samples"]
    assert [s["frame"] for s in samples] == [0, 15, 30]  # 15 fps buckets
    assert samples[0]["bbox"] == [0, 10, 20, 30]  # [l,t,r,b] -> [x,y,w,h]
    assert samples[0]["area"] == 600


# --- propose_nouns / find_anomaly with fake Spark ------------------------------

def _fake_spark(reply: str, log: list):
    def call(mp4: Path, prompt: str):
        log.append((str(mp4), prompt))
        return reply, {"input_tokens": 100, "output_tokens": 50}
    return call


def test_propose_nouns_validates_and_caches(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(analyze, "CACHE_DIR", tmp_path)
    mp4 = tmp_path / "clip.mp4"
    mp4.write_bytes(b"\x00" * 32)
    log: list = []
    reply = json.dumps(_load("spark_nouns_sample.json"))
    doc = propose_nouns(mp4, spark_fn=_fake_spark(reply, log))
    assert doc == {"nouns": ["square", "rectangle", "circle"]}
    again = propose_nouns(mp4, spark_fn=_fake_spark("garbage", log))
    assert again == doc and len(log) == 1  # second call served by cache


def test_propose_nouns_rejects_invalid_reply(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(analyze, "CACHE_DIR", tmp_path)
    mp4 = tmp_path / "clip.mp4"
    mp4.write_bytes(b"\x00" * 32)
    with pytest.raises(ValueError):
        propose_nouns(mp4, spark_fn=lambda m, p: ('{"nouns": ["x"]}', None))


def _setup_case(tmp_path, monkeypatch, with_tracks: bool) -> Path:
    cap = tmp_path / "captures" / "k1"
    cap.mkdir(parents=True)
    mp4 = cap / "buggy.mp4"
    mp4.write_bytes(b"\x00" * 32)
    (cap / "buggy.meta.json").write_text(json.dumps(
        {"load_offset_s": 0.14, "frames": 83}))
    if with_tracks:
        per = tmp_path / "perception" / "k1"
        per.mkdir(parents=True)
        doc = {"model": "sam-3.1", "noun": "square", "mp4": "buggy.mp4",
               "sha256": "x" * 64, "frame_count": 31, "track_count": 1,
               "tracks": [{"object_id": "a", "visible_frames": 31,
                           "first_frame": 0, "last_frame": 30}],
               "frames": [
                   {"frame": f, "track_id": "a",
                    "bbox": [100.0, 200.0, 164.0, 264.0], "mask_area": 4096}
                   for f in range(31)]}
        (per / "buggy.square.json").write_text(json.dumps(doc))
    monkeypatch.setattr(analyze, "CAPTURES_DIR", tmp_path / "captures")
    monkeypatch.setattr(analyze, "PERCEPTION_DIR", tmp_path / "perception")
    monkeypatch.setattr(analyze, "CACHE_DIR", tmp_path / "cache")
    return mp4


def test_find_anomaly_with_tracks_uses_sam_bbox(tmp_path, monkeypatch) -> None:
    _setup_case(tmp_path, monkeypatch, with_tracks=True)
    log: list = []
    reply = json.dumps(_load("spark_anomaly_sample.json"))
    result = find_anomaly("k1", "buggy", spark_fn=_fake_spark(reply, log))
    assert result["anomaly"] is True
    assert result["localization"] == "sam"
    assert result["time_s"] == 1.86  # recomputed: 30/15 - 0.14
    # SAM bbox at frame 30 is [100,200,164,264] -> [x,y,w,h]
    assert result["bbox"] == [100.0, 200.0, 64.0, 64.0]
    assert validate_anomaly_doc(result) == []
    assert "per second" in log[0][1] or "sample per second" in log[0][1]


def test_find_anomaly_without_tracks_uses_spark_bbox(
        tmp_path, monkeypatch) -> None:
    _setup_case(tmp_path, monkeypatch, with_tracks=False)
    reply = json.dumps(_load("spark_anomaly_sample.json"))
    result = find_anomaly("k1", "buggy", spark_fn=_fake_spark(reply, []))
    assert result["localization"] == "spark"
    assert result["bbox"] == [10, 20, 20, 20]


def test_find_anomaly_negative_control(tmp_path, monkeypatch) -> None:
    _setup_case(tmp_path, monkeypatch, with_tracks=True)
    reply = json.dumps(_load("spark_anomaly_negative_sample.json"))
    result = find_anomaly("k1", "buggy", spark_fn=_fake_spark(reply, []))
    assert result["anomaly"] is False
    assert result["frame"] is None and result["bbox"] is None


def test_find_anomaly_reported_mode_is_cached_separately(
        tmp_path, monkeypatch) -> None:
    _setup_case(tmp_path, monkeypatch, with_tracks=False)
    report = "the popup's top part is hidden when it opens"
    log: list = []
    reply = json.dumps(_load("spark_anomaly_sample.json"))
    result = find_anomaly("k1", "buggy", spark_fn=_fake_spark(reply, log),
                          report=report)
    assert result["mode"] == "reported"
    assert result["bbox"] == [10, 20, 20, 20]
    assert "A user reported:" in log[0][1]
    assert validate_anomaly_doc(result) == []
    # Blind cache does not serve the reported mode; reported cache does.
    blind = find_anomaly("k1", "buggy", spark_fn=_fake_spark(reply, log))
    assert blind["mode"] == "blind"
    assert "A user reported:" not in log[1][1]
    again = find_anomaly("k1", "buggy", spark_fn=_fake_spark("garbage", log),
                         report=report)
    assert again["anomaly"] is True and len(log) == 2


def test_find_anomaly_rejects_out_of_range_frame(
        tmp_path, monkeypatch) -> None:
    _setup_case(tmp_path, monkeypatch, with_tracks=False)
    bad = dict(_load("spark_anomaly_sample.json"), frame=999)
    with pytest.raises(ValueError):
        find_anomaly("k1", "buggy",
                     spark_fn=lambda m, p: (json.dumps(bad), None))
