"""Offline tests for visiopatch.perceive — no network, no API key.

Covers the cache key, the tracks-doc schema validation (against a recorded
fixture), and the stream shim that synthesizes response.output_text.done.
"""

import json
from pathlib import Path
from types import SimpleNamespace

from visiopatch.perceive import (
    cache_key,
    cache_stem,
    estimate_cost_usd,
    records_to_doc,
    shim_events,
    validate_tracks_doc,
)

FIXTURE = Path(__file__).parent / "fixtures" / "tracks_sample.json"


def test_cache_key_stable_and_sensitive() -> None:
    assert cache_key("aaa", "box") == cache_key("aaa", "box")
    assert cache_key("aaa", "box") != cache_key("aaa", "panel")
    assert cache_key("aaa", "box") != cache_key("bbb", "box")
    assert cache_stem("aaa", "Box!") != cache_stem("aaa", "panel")


def test_fixture_validates_clean() -> None:
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert validate_tracks_doc(doc) == []
    assert doc["track_count"] >= 1


def test_empty_doc_is_valid_but_flagged_downstream() -> None:
    doc = {"model": "sam-3.1", "noun": "panel", "mp4": "fix.mp4",
           "sha256": "x" * 64, "frame_count": 0, "track_count": 0,
           "tracks": [], "frames": []}
    assert validate_tracks_doc(doc) == []


def test_validation_rejects_bad_docs() -> None:
    assert validate_tracks_doc({}) != []  # missing keys
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    bad = json.loads(json.dumps(doc))
    bad["frames"][0]["bbox"] = [10, 10, 5, 5]  # right<left
    assert validate_tracks_doc(bad) != []
    bad = json.loads(json.dumps(doc))
    bad["track_count"] = 99
    assert validate_tracks_doc(bad) != []
    bad = json.loads(json.dumps(doc))
    bad["frames"][0]["mask_area"] = -1
    assert validate_tracks_doc(bad) != []


def test_cost_estimate_matches_rate_card() -> None:
    assert estimate_cost_usd(1000) == 0.20
    assert estimate_cost_usd(150) == 0.03


def test_shim_synthesizes_output_text_done(tmp_path) -> None:
    lane = {"item_id": "i", "output_index": 0, "content_index": 0,
            "type": "response.content_part.done",
            "part": {"type": "output_text", "text": "frame 0: box x1=1"}}
    events = shim_events([lane], tmp_path / "raw.jsonl")
    dones = [e for e in events if e["type"] == "response.output_text.done"]
    assert len(dones) == 1 and dones[0]["text"] == "frame 0: box x1=1"
    # A duplicate done lane must not double-close.
    events2 = shim_events(
        [lane, {**lane, "type": "response.output_text.done",
                "text": "frame 0: box x1=1"}],
        tmp_path / "raw2.jsonl")
    assert sum(1 for e in events2
               if e["type"] == "response.output_text.done") == 1


def _box(frame: int, oid: str, l=10.0, t=20.0, r=30.0, b=40.0):
    return SimpleNamespace(kind="box", object_id=oid,
                           frame=SimpleNamespace(frame_index=frame),
                           left=l, top=t, right=r, bottom=b)


def test_records_to_doc_groups_frames_and_tracks(tmp_path) -> None:
    mp4 = tmp_path / "buggy.mp4"
    mp4.write_bytes(b"\x00" * 64)
    result = SimpleNamespace(
        outcome=SimpleNamespace(status="completed"), diagnostics=[],
        records=[_box(0, "a"), _box(1, "a"),
                 SimpleNamespace(kind="text", object_id="",
                                 frame=None)],
    )
    doc = records_to_doc(mp4, "box", result)
    assert doc["frame_count"] == 2
    assert doc["track_count"] == 1
    assert validate_tracks_doc(doc) == []
    assert all(e["mask_area"] is None for e in doc["frames"])


def test_records_to_doc_rejects_bad_outcome(tmp_path) -> None:
    import pytest as _pytest

    mp4 = tmp_path / "buggy.mp4"
    mp4.write_bytes(b"\x00" * 64)
    result = SimpleNamespace(
        outcome=SimpleNamespace(status="incomplete"), diagnostics=[],
        records=[])
    with _pytest.raises(ValueError):
        records_to_doc(mp4, "box", result)
