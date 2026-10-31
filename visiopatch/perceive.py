"""SAM 3.1 perception for VisioPatch captures — one noun phrase per request.

``perceive(mp4, noun, dest)`` calls SAM 3.1 exactly the way
``scripts/smoke.py`` does (``purpose=user_data`` upload, streamed response,
``meta_sam_parser`` with the shimmed ``output_text.done`` lane), caches by
``sha256(mp4) + noun`` so a clip is never paid for twice, validates the
response, and writes per-frame ``{track id, bbox, mask area}`` tracks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from meta_sam_parser import (
    decode_mask_to_raster,
    parse_responses_stream,
    video_segmentation_format,
)
from openai import OpenAI

from visiopatch.bench import REPO_ROOT
from visiopatch.capture import sha256_of

CACHE_DIR = REPO_ROOT / "cache"
UPLOADS_PATH = CACHE_DIR / "uploads.json"
BASE_URL = "https://api.meta.ai/v1"
SAM_MODEL = "sam-3.1"
TIMEOUT_S = 300.0
SAM_USD_PER_1K_FRAMES = 0.20


def estimate_cost_usd(frames: int) -> float:
    return frames / 1000 * SAM_USD_PER_1K_FRAMES


def cache_key(mp4_sha256: str, noun: str) -> str:
    """Stable cache key for one (clip, noun) request."""
    return hashlib.sha256(f"{mp4_sha256}|{noun}".encode()).hexdigest()


def _slug(noun: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", noun.strip().lower()).strip("-")
    return slug or "noun"


def cache_stem(mp4_sha256: str, noun: str) -> str:
    return f"{mp4_sha256[:16]}_{_slug(noun)}"


def make_client(timeout: float = TIMEOUT_S) -> OpenAI:
    import os
    key = os.environ.get("MODEL_API_KEY")
    if not key:
        raise RuntimeError("MODEL_API_KEY not set (expected in .env)")
    return OpenAI(base_url=BASE_URL, api_key=key, timeout=timeout)


def get_file_id(client: OpenAI, mp4: Path) -> str:
    """Upload once per clip; reuse the file_id on matching sha256."""
    digest = sha256_of(mp4)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache: dict[str, Any] = {}
    if UPLOADS_PATH.exists():
        try:
            cache = json.loads(UPLOADS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}
    entry = cache.get(digest)
    if isinstance(entry, dict) and str(entry.get("file_id", "")).startswith("file-"):
        return str(entry["file_id"])
    with mp4.open("rb") as f:
        uploaded = client.files.create(
            file=(mp4.name, f, "video/mp4"), purpose="user_data")
    if not uploaded.id.startswith("file-"):
        raise ValueError(f"Files API returned invalid file id: {uploaded.id!r}")
    cache[digest] = {"file_id": uploaded.id, "filename": mp4.name}
    UPLOADS_PATH.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")
    return uploaded.id


def shim_events(stream: Any, raw_path: Path) -> list[dict[str, Any]]:
    """Port of cookbook sam_api.stream_events (sync form): the API streams
    response.content_part.done but not response.output_text.done, which the
    parser needs to close a lane, so synthesize the latter."""
    events: list[dict[str, Any]] = []
    completed: set[tuple[Any, Any, Any]] = set()
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with raw_path.open("w", encoding="utf-8") as raw:
        for event in stream:
            data = event.model_dump() if hasattr(event, "model_dump") else dict(event)
            raw.write(json.dumps(data, default=str) + "\n")
            lane = (data.get("item_id"), data.get("output_index"),
                    data.get("content_index"))
            if data.get("type") == "response.output_text.done":
                if lane not in completed:
                    completed.add(lane)
                    events.append(data)
                continue
            events.append(data)
            part = data.get("part")
            if (data.get("type") == "response.content_part.done"
                    and lane not in completed
                    and isinstance(part, dict)
                    and part.get("type") == "output_text"
                    and isinstance(part.get("text"), str)):
                completed.add(lane)
                events.append({
                    "type": "response.output_text.done",
                    "item_id": data.get("item_id"),
                    "output_index": data.get("output_index"),
                    "content_index": data.get("content_index"),
                    "text": part["text"],
                })
    return events


async def _parse(events: list[dict[str, Any]]) -> Any:
    async def gen():  # noqa: ANN202
        for e in events:
            yield e

    parsed = parse_responses_stream(gen(), video_segmentation_format())
    async with parsed:
        async for _snapshot in parsed:
            pass
        return await parsed.final_result()


def records_to_doc(mp4: Path, noun: str, result: Any,
                   requested: str | None = None) -> dict[str, Any]:
    """Fold parser records into the validated tracks-doc shape."""
    if result.outcome.status != "completed":
        raise ValueError(f"SAM outcome={result.outcome.status!r}")
    errors = [d for d in result.diagnostics if d.severity == "error"]
    if errors:
        raise ValueError("parser errors: "
                         + "; ".join(f"line {d.line}: {d.message}" for d in errors))
    per_frame: dict[tuple[int, str], dict[str, Any]] = {}
    for rec in result.records:
        if getattr(rec, "kind", "") == "text" or rec.frame is None:
            continue
        key = (rec.frame.frame_index, rec.object_id)
        slot = per_frame.setdefault(key, {"bbox": None, "mask_area": None})
        if getattr(rec, "kind", "") == "box":
            slot["bbox"] = [rec.left, rec.top, rec.right, rec.bottom]
        elif getattr(rec, "kind", "") == "mask":
            if slot["bbox"] is None:
                slot["bbox"] = [rec.bounds.left, rec.bounds.top,
                                rec.bounds.right, rec.bounds.bottom]
            try:
                raster = decode_mask_to_raster(rec.mask)
                slot["mask_area"] = sum(raster)
            except Exception:
                pass
    frames = sorted(
        ({"frame": f, "track_id": oid,
          "bbox": slot["bbox"], "mask_area": slot["mask_area"]}
         for (f, oid), slot in per_frame.items() if slot["bbox"] is not None),
        key=lambda e: (e["frame"], e["track_id"]))
    by_track: dict[str, set[int]] = {}
    for e in frames:
        by_track.setdefault(e["track_id"], set()).add(e["frame"])
    return {
        "model": SAM_MODEL,
        "noun": noun,
        "requested_noun": requested if requested is not None else noun,
        "mp4": mp4.name,
        "sha256": sha256_of(mp4),
        "frame_count": len({e["frame"] for e in frames}),
        "track_count": len(by_track),
        "tracks": [{"object_id": oid, "visible_frames": len(fs),
                    "first_frame": min(fs), "last_frame": max(fs)}
                   for oid, fs in sorted(by_track.items())],
        "frames": frames,
    }


def validate_tracks_doc(doc: Any) -> list[str]:
    """Schema check for a tracks doc. Empty (0 tracks) is valid — empties
    are flagged by the runner, not rejected here."""
    errs: list[str] = []
    if not isinstance(doc, dict):
        return ["tracks doc must be an object"]
    for key in ("noun", "mp4", "sha256", "frame_count", "track_count", "tracks", "frames"):
        if key not in doc:
            errs.append(f"missing key: {key}")
    if errs:
        return errs
    if not isinstance(doc["frames"], list):
        return ["'frames' must be a list"]
    seen_tracks: set[str] = set()
    seen_frames: set[int] = set()
    for i, e in enumerate(doc["frames"]):
        if not isinstance(e, dict):
            errs.append(f"frames[{i}] must be an object")
            continue
        f, tid, bbox, area = e.get("frame"), e.get("track_id"), e.get("bbox"), e.get("mask_area")
        if not isinstance(f, int) or f < 0:
            errs.append(f"frames[{i}].frame must be a non-negative int")
        else:
            seen_frames.add(f)
        if not isinstance(tid, str) or not tid:
            errs.append(f"frames[{i}].track_id must be a non-empty string")
        else:
            seen_tracks.add(tid)
        if (not isinstance(bbox, list) or len(bbox) != 4
                or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in bbox)):
            errs.append(f"frames[{i}].bbox must be 4 finite numbers")
        elif not (bbox[2] > bbox[0] and bbox[3] > bbox[1]):
            errs.append(f"frames[{i}].bbox must have right>left and bottom>top")
        if area is not None and (not isinstance(area, int) or area < 0):
            errs.append(f"frames[{i}].mask_area must be a non-negative int or null")
    if doc["track_count"] != len(seen_tracks):
        errs.append(f"track_count {doc['track_count']} != distinct track_ids {len(seen_tracks)}")
    if doc["frame_count"] != len(seen_frames):
        errs.append(f"frame_count {doc['frame_count']} != distinct frames {len(seen_frames)}")
    return errs


def perceive(mp4: Path, noun: str, dest: Path,
             requested_noun: str | None = None) -> dict[str, Any]:
    """Run SAM 3.1 for one noun on one clip (cached), validate, write dest.

    ``requested_noun`` is the primary noun when ``noun`` is a fallback hit;
    both are recorded in the tracks file. Returns a summary
    ``{frames, tracks, cost_usd, cached, noun}``.
    """
    load_dotenv(REPO_ROOT / ".env")
    digest = sha256_of(mp4)
    stem = cache_stem(digest, noun)
    raw_path = CACHE_DIR / f"{stem}_raw.jsonl"
    doc_path = CACHE_DIR / f"{stem}_doc.json"
    if doc_path.exists():
        try:
            doc = json.loads(doc_path.read_text(encoding="utf-8"))
            if doc.get("sha256") == digest and not validate_tracks_doc(doc):
                return _write(doc, dest, cached=True)
        except json.JSONDecodeError:
            pass
    client = make_client()
    file_id = get_file_id(client, mp4)
    stream = client.responses.create(
        model=SAM_MODEL, stream=True,
        input=[{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": noun},
            {"type": "input_video", "file_id": file_id},
        ]}],
    )
    events = shim_events(stream, raw_path)
    result = asyncio.run(_parse(events))
    doc = records_to_doc(mp4, noun, result, requested_noun)
    errs = validate_tracks_doc(doc)
    if errs:
        raise ValueError("invalid tracks doc: " + "; ".join(errs))
    doc_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return _write(doc, dest, cached=False)


def _write(doc: dict[str, Any], dest: Path, cached: bool) -> dict[str, Any]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return {"frames": doc["frame_count"], "tracks": doc["track_count"],
            "cost_usd": estimate_cost_usd(doc["frame_count"]), "cached": cached,
            "noun": doc["noun"]}
