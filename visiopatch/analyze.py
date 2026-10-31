"""Spark 1.3 analysis for VisioPatch captures.

``propose_nouns(mp4)`` asks Spark for exactly 3 concrete nouns for the
main visual objects in a clip. ``find_anomaly(case, state)`` asks Spark
to find visual behaviour that looks wrong in a clip, given a compact
per-second SAM tracks summary when tracks exist.

Both call Spark exactly the way ``scripts/smoke.py`` does (sync openai
SDK, ``input_file`` part beside ``input_text``), cache by sha256 plus a
prompt version so a clip is never paid for twice, and validate every
JSON response against a schema before trusting it.

No leakage: prompts receive only the clip, the tracks summary and a
neutral instruction. Case ids, states, PR/issue text, diffs, SHAs and
state labels never enter a prompt.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from openai import OpenAI

from visiopatch.bench import REPO_ROOT
from visiopatch.capture import FPS, sha256_of
from visiopatch.perceive import CACHE_DIR, get_file_id, validate_tracks_doc

CAPTURES_DIR = REPO_ROOT / "captures"
PERCEPTION_DIR = REPO_ROOT / "perception"
BASE_URL = "https://api.meta.ai/v1"
SPARK_MODEL = "muse-spark-1.3"
TIMEOUT_S = 180.0
WIDTH, HEIGHT = 1280, 720

NOUN_PROMPT_VERSION = 1
ANOMALY_PROMPT_VERSION = 2
REPORTED_PROMPT_VERSION = 1

# PROJECT.md decision 6: USD per 1M tokens (in/out) for Spark.
SPARK_PRICES = (1.25, 4.25)

SparkFn = Callable[[Path, str], tuple[str, dict[str, Any] | None]]


class BudgetExceeded(Exception):
    """Raised by a budget callback before a call that would overspend."""


def make_client(timeout: float = TIMEOUT_S) -> OpenAI:
    import os
    key = os.environ.get("MODEL_API_KEY")
    if not key:
        raise RuntimeError("MODEL_API_KEY not set (expected in .env)")
    return OpenAI(base_url=BASE_URL, api_key=key, timeout=timeout)


def usage_dict(resp: Any) -> dict[str, Any] | None:
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None
    if hasattr(usage, "model_dump"):
        return usage.model_dump()
    return dict(usage)


def token_cost_usd(usage: dict[str, Any] | None) -> float:
    if not usage:
        return 0.0
    in_rate, out_rate = SPARK_PRICES
    inp = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    out = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    return (inp * in_rate + out * out_rate) / 1_000_000


def _call_spark(mp4: Path, prompt: str) -> tuple[str, dict[str, Any] | None]:
    """One live Spark video call, shaped exactly like scripts/smoke.py."""
    load_dotenv(REPO_ROOT / ".env")
    client = make_client()
    file_id = get_file_id(client, mp4)
    resp = client.responses.create(
        model=SPARK_MODEL,
        input=[{"role": "user", "content": [
            {"type": "input_file", "file_id": file_id},
            {"type": "input_text", "text": prompt},
        ]}],
    )
    return (resp.output_text or "").strip(), usage_dict(resp)


def extract_json(text: str) -> Any:
    """Parse a model reply that must be one JSON value (fences allowed)."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t.strip())
    return json.loads(t)


# --- prompts (generic only; never tuned per case) ---------------------------

def build_noun_prompt() -> str:
    return (
        "Watch the attached video clip. List exactly 3 short, concrete "
        "nouns naming the main visual objects in the clip, in order of "
        "prominence. Use words a person would use to point at the object; "
        "shape or colour words are fine. Each noun is one or two words, "
        "lowercase, no articles. Reply with JSON only, no other text: "
        '{"nouns": ["...", "...", "..."]}'
    )


def build_anomaly_prompt(tracks_summary: dict[str, Any] | None,
                         report: str | None = None) -> str:
    if tracks_summary and tracks_summary.get("tracks"):
        tracks_block = (
            "Tracked objects (bounding boxes as [x, y, w, h] in a "
            f"{WIDTH}x{HEIGHT} frame, one sample per second):\n"
            + json.dumps(tracks_summary, indent=None)
        )
    else:
        tracks_block = "No object tracks are available for this clip."
    if report:
        task_block = (
            f"A user reported: {report}. Find where and when this "
            "happens in the clip, or say it does not happen.\n"
            "Saying it does not happen is a valid answer: if the "
            "reported behaviour does not happen in this clip, return "
            "anomaly as false. Do not assume the report must be right.\n"
        )
    else:
        task_block = (
            "Watch the attached video clip and find visual behaviour "
            "that looks wrong.\n"
            "Nothing looking wrong is a valid answer: if nothing looks "
            "wrong, say so by returning anomaly as false. Do not assume "
            "something must be wrong.\n"
        )
    return (
        f"{task_block}"
        "Report an anomaly only if the behaviour persists across "
        "several sampled seconds, not at a single moment.\n"
        "The behaviour you report must agree with the tracked-object "
        "samples when tracks are available; do not claim a size or "
        "position change the samples do not show.\n"
        "Judge from observed object motion. When numbers shown on "
        "screen appear to conflict with the motion you observe, trust "
        "the motion; do not report an anomaly based on numbers alone.\n"
        f"{tracks_block}\n"
        f"The frame is {WIDTH}x{HEIGHT} pixels. Frames are numbered "
        f"from 0 at {FPS} frames per second.\n"
        "Reply with JSON only, no other text, with exactly these keys: "
        '{"anomaly": true or false, "frame": frame number where the '
        'behaviour is clearest or null, "time_s": seconds or null, '
        '"track_ids": [ids of tracked objects involved], '
        '"bbox": [x, y, w, h] of the region involved or null, '
        '"observed": what is seen, "expected": what would be expected '
        'instead, "confidence": a number from 0 to 1}. '
        "When anomaly is false, set frame, time_s and bbox to null and "
        "track_ids to an empty list."
    )


# --- schema validation ------------------------------------------------------

def validate_nouns_doc(doc: Any) -> list[str]:
    errs: list[str] = []
    if not isinstance(doc, dict):
        return ["nouns doc must be an object"]
    nouns = doc.get("nouns")
    if not isinstance(nouns, list) or len(nouns) != 3:
        return ["'nouns' must be a list of exactly 3 nouns"]
    seen: set[str] = set()
    for i, n in enumerate(nouns):
        if not isinstance(n, str) or not n.strip():
            errs.append(f"nouns[{i}] must be a non-empty string")
        elif len(n) > 30 or len(n.split()) > 2:
            errs.append(f"nouns[{i}] must be short (<=2 words, <=30 chars)")
        elif n.strip().lower() in seen:
            errs.append(f"nouns[{i}] is a duplicate")
        else:
            seen.add(n.strip().lower())
    return errs


def validate_anomaly_doc(doc: Any) -> list[str]:
    errs: list[str] = []
    if not isinstance(doc, dict):
        return ["anomaly doc must be an object"]
    for key in ("anomaly", "frame", "time_s", "track_ids", "bbox",
                "observed", "expected", "confidence"):
        if key not in doc:
            errs.append(f"missing key: {key}")
    if errs:
        return errs
    if not isinstance(doc["anomaly"], bool):
        errs.append("'anomaly' must be a boolean")
    conf = doc["confidence"]
    if (not isinstance(conf, (int, float)) or isinstance(conf, bool)
            or not math.isfinite(conf) or not 0.0 <= conf <= 1.0):
        errs.append("'confidence' must be a number from 0 to 1")
    if not isinstance(doc["track_ids"], list) or not all(
            isinstance(t, str) for t in doc["track_ids"]):
        errs.append("'track_ids' must be a list of strings")
    for key in ("observed", "expected"):
        if not isinstance(doc[key], str):
            errs.append(f"'{key}' must be a string")
    frame, time_s, bbox = doc["frame"], doc["time_s"], doc["bbox"]
    if doc["anomaly"] is True:
        if not isinstance(frame, int) or isinstance(frame, bool) or frame < 0:
            errs.append("'frame' must be a non-negative int when anomaly")
        if (not isinstance(time_s, (int, float)) or isinstance(time_s, bool)
                or not math.isfinite(time_s)):
            errs.append("'time_s' must be a number when anomaly")
        if (not isinstance(bbox, list) or len(bbox) != 4
                or not all(isinstance(v, (int, float)) and math.isfinite(v)
                           for v in bbox)):
            errs.append("'bbox' must be [x, y, w, h] numbers when anomaly")
        elif not (bbox[0] >= 0 and bbox[1] >= 0 and bbox[2] > 0
                  and bbox[3] > 0 and bbox[0] + bbox[2] <= WIDTH
                  and bbox[1] + bbox[3] <= HEIGHT):
            errs.append("'bbox' must lie inside the 1280x720 frame")
        if isinstance(doc.get("observed"), str) and not doc["observed"].strip():
            errs.append("'observed' must be non-empty when anomaly")
    else:
        if frame is not None or time_s is not None or bbox is not None:
            errs.append("frame/time_s/bbox must be null when not anomaly")
        if doc["track_ids"]:
            errs.append("'track_ids' must be empty when not anomaly")
    return errs


# --- tracks summary ---------------------------------------------------------

def _xywh(bbox: list[float]) -> list[float]:
    left, top, right, bottom = bbox
    return [left, top, right - left, bottom - top]


def summarize_tracks(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """Per track: one bbox+area sample per video second (first frame)."""
    tracks: list[dict[str, Any]] = []
    for doc in docs:
        by_track: dict[str, list[dict[str, Any]]] = {}
        for e in doc.get("frames", []):
            by_track.setdefault(e["track_id"], []).append(e)
        for tid in sorted(by_track):
            entries = sorted(by_track[tid], key=lambda e: e["frame"])
            samples = []
            seen_seconds: set[int] = set()
            for e in entries:
                second = e["frame"] // FPS
                if second in seen_seconds:
                    continue
                seen_seconds.add(second)
                samples.append({"frame": e["frame"],
                                "bbox": _xywh(e["bbox"]),
                                "area": e["mask_area"]})
            tracks.append({"track_id": tid, "noun": doc.get("noun"),
                           "samples": samples})
    return {"tracks": tracks}


def load_track_docs(case_id: str, state: str) -> list[dict[str, Any]]:
    """Validated perception docs with >=1 track for one case/state."""
    docs: list[dict[str, Any]] = []
    case_dir = PERCEPTION_DIR / case_id
    if not case_dir.is_dir():
        return docs
    for path in sorted(case_dir.glob(f"{state}.*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not validate_tracks_doc(doc) and doc.get("track_count", 0) > 0:
            docs.append(doc)
    return docs


def frame_to_page_time(frame: int, load_offset_s: float) -> float:
    """Page time: video time (frame/FPS) minus the pre-load offset."""
    return round(frame / FPS - load_offset_s, 3)


def _track_bbox_at(docs: list[dict[str, Any]], track_ids: list[str],
                   frame: int) -> list[float] | None:
    """SAM bbox [x,y,w,h] for a named track, exact frame else nearest."""
    for doc in docs:
        entries = [e for e in doc.get("frames", [])
                   if e["track_id"] in track_ids]
        if not entries:
            continue
        exact = [e for e in entries if e["frame"] == frame]
        pick = exact[0] if exact else min(
            entries, key=lambda e: abs(e["frame"] - frame))
        return _xywh(pick["bbox"])
    return None


# --- caching ----------------------------------------------------------------

def _noun_cache_path(mp4_sha256: str) -> Path:
    return CACHE_DIR / f"{mp4_sha256[:16]}_spark-nouns-v{NOUN_PROMPT_VERSION}.json"


def _anomaly_cache_path(mp4_sha256: str, summary: dict[str, Any],
                        report: str | None = None) -> Path:
    shash = hashlib.sha256(
        json.dumps(summary, sort_keys=True).encode()).hexdigest()[:12]
    if report:
        rhash = hashlib.sha256(report.encode()).hexdigest()[:12]
        return CACHE_DIR / (
            f"{mp4_sha256[:16]}_spark-reported-v{REPORTED_PROMPT_VERSION}"
            f"-{shash}-{rhash}.json")
    return CACHE_DIR / (
        f"{mp4_sha256[:16]}_spark-anomaly-v{ANOMALY_PROMPT_VERSION}-{shash}.json")


def _read_cache(path: Path, validator: Callable[[Any], list[str]]) -> dict | None:
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return doc if not validator(doc) else None


# --- public API ---------------------------------------------------------------

def propose_nouns(mp4: Path, spark_fn: SparkFn = _call_spark) -> dict[str, Any]:
    """Ask Spark for exactly 3 concrete nouns for the clip's main objects.

    Cached by sha256(mp4) + prompt version. Returns {"nouns": [...]}.
    """
    digest = sha256_of(mp4)
    path = _noun_cache_path(digest)
    cached = _read_cache(path, validate_nouns_doc)
    if cached is not None:
        return {"nouns": list(cached["nouns"])}
    text, _usage = spark_fn(mp4, build_noun_prompt())
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{path.stem}_raw.txt").write_text(
        text + "\n", encoding="utf-8")
    try:
        doc = extract_json(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Spark nouns reply is not JSON: {exc}") from exc
    errs = validate_nouns_doc(doc)
    if errs:
        raise ValueError("invalid nouns doc: " + "; ".join(errs))
    doc = {"nouns": [n.strip().lower() for n in doc["nouns"]]}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return doc


def find_anomaly(case_id: str, state: str, spark_fn: SparkFn = _call_spark,
                 budget_check: Callable[[float], None] | None = None,
                 report: str | None = None,
                 ) -> dict[str, Any]:
    """Spark anomaly verdict for one captured clip.

    ``case_id``/``state`` only locate local files; neither reaches the
    prompt. With ``report`` (a one-line user report), Spark is asked to
    find where and when that behaviour happens, or say it does not
    happen; that mode has its own prompt version and cache. Returns the
    validated anomaly doc plus ``localization`` ("sam" when the bbox
    comes from a SAM track, else "spark"), ``mode``, ``cost_usd`` and
    ``cached``.
    """
    if state not in ("buggy", "fix"):
        raise ValueError(f"state must be 'buggy' or 'fix', got {state!r}")
    mp4 = CAPTURES_DIR / case_id / f"{state}.mp4"
    meta = json.loads(
        (CAPTURES_DIR / case_id / f"{state}.meta.json").read_text(
            encoding="utf-8"))
    load_offset_s = float(meta["load_offset_s"])
    total_frames = int(meta["frames"])

    docs = load_track_docs(case_id, state)
    summary = summarize_tracks(docs)
    digest = sha256_of(mp4)
    path = _anomaly_cache_path(digest, summary, report)
    mode = "reported" if report else "blind"

    cached = _read_cache(path, validate_anomaly_doc)
    if cached is not None:
        result = dict(cached)
        result.update({"localization": _localization(result, docs),
                       "mode": mode,
                       "cost_usd": 0.0, "cached": True, "usage": None})
        return result

    if budget_check is not None:
        budget_check(0.05)  # conservative per-call allowance, USD
    text, usage = spark_fn(mp4, build_anomaly_prompt(summary, report))
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{path.stem}_raw.txt").write_text(
        text + "\n", encoding="utf-8")
    try:
        raw = extract_json(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Spark anomaly reply is not JSON: {exc}") from exc
    result = _normalize(raw, load_offset_s, total_frames)
    errs = validate_anomaly_doc(result)
    if errs:
        raise ValueError("invalid anomaly doc: " + "; ".join(errs))
    if result["anomaly"] and result["track_ids"]:
        sam_bbox = _track_bbox_at(docs, result["track_ids"], result["frame"])
        if sam_bbox is not None:
            result["bbox"] = sam_bbox
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    result = dict(result)
    result.update({"localization": _localization(result, docs),
                   "mode": mode,
                   "cost_usd": token_cost_usd(usage), "cached": False,
                   "usage": usage})
    return result


def _localization(result: dict[str, Any], docs: list[dict]) -> str:
    if (result.get("anomaly") and result.get("track_ids") and docs
            and _track_bbox_at(docs, result["track_ids"],
                               result["frame"]) is not None):
        return "sam"
    return "spark"


def _normalize(raw: Any, load_offset_s: float,
               total_frames: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("anomaly doc must be an object")
    raw_ids = raw.get("track_ids")
    out = {
        "anomaly": raw.get("anomaly"),
        "frame": raw.get("frame"),
        "time_s": raw.get("time_s"),
        # SAM track ids are strings; Spark may echo them as numbers.
        "track_ids": [str(t) for t in raw_ids
                      if isinstance(t, (str, int)) and not isinstance(t, bool)]
        if isinstance(raw_ids, list) else [],
        "bbox": raw.get("bbox"),
        "observed": raw.get("observed") if isinstance(
            raw.get("observed"), str) else "",
        "expected": raw.get("expected") if isinstance(
            raw.get("expected"), str) else "",
        "confidence": raw.get("confidence"),
    }
    if out["anomaly"] is not True:
        out.update({"anomaly": False if out["anomaly"] is False else out["anomaly"],
                    "frame": None, "time_s": None, "bbox": None,
                    "track_ids": []})
        return out
    frame = out["frame"]
    if isinstance(frame, int) and not isinstance(frame, bool):
        if not 0 <= frame < total_frames:
            raise ValueError(
                f"anomaly frame {frame} outside clip (0..{total_frames - 1})")
        out["time_s"] = frame_to_page_time(frame, load_offset_s)
    return out


def anomaly_is_cached(case_id: str, state: str,
                      report: str | None = None) -> bool:
    """True when find_anomaly(case_id, state) needs no live Spark call."""
    mp4 = CAPTURES_DIR / case_id / f"{state}.mp4"
    if not mp4.exists():
        return False
    summary = summarize_tracks(load_track_docs(case_id, state))
    return _read_cache(
        _anomaly_cache_path(sha256_of(mp4), summary, report),
        validate_anomaly_doc) is not None
