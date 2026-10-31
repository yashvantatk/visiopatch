#!/usr/bin/env python3
"""Live smoke test — NOT a pytest. Calls real APIs; run directly:

    .venv/bin/python scripts/smoke.py

Checks (each independent; one failure never stops the rest):
  1. SAM 3.1 video segmentation of pingpong.mp4 ("ping pong ball")
  2. Spark 1.3 video question on the same upload
  3. Wonder short text call (reasoning effort high)
  4. `muse exec` headless in a temp dir with one trivial passing test

Exit 1 if any check FAILs, 0 only if all PASS (PROJECT.md decision 2).
Outputs land in smoke_out/ (gitignored). Uploads are cached by file
sha256 in smoke_out/cache.json so reruns don't re-upload.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from meta_sam_parser import parse_responses_stream, video_segmentation_format
from openai import OpenAI

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "smoke_out"
CACHE_PATH = OUT_DIR / "cache.json"
BASE_URL = "https://api.meta.ai/v1"

SAM_MODEL = "sam-3.1"
SPARK_MODEL = "muse-spark-1.3"
WONDER_MODEL = "muse-wonder-1.0"
NOUN_PHRASE = "ping pong ball"
SPARK_QUESTION = "At what second does something first look wrong, and what is it?"
WONDER_PROMPT = "In one sentence, what does a smoke test verify?"

TIMEOUTS = {"sam": 300.0, "spark": 180.0, "wonder": 180.0, "muse_exec": 300}

# PROJECT.md decision 6: USD per 1M tokens (in/out); SAM per 1k frames.
PRICES = {"spark": (1.25, 4.25), "wonder": (4.0, 20.0)}
SAM_USD_PER_1K_FRAMES = 0.20

RESULTS: list[dict[str, Any]] = []


def record(name: str, ok: bool, detail: str, usage: Any = None,
           cost_usd: float | None = None) -> bool:
    RESULTS.append({"check": name, "pass": ok, "detail": detail,
                    "usage": usage, "cost_usd": cost_usd})
    usage_s = json.dumps(usage) if usage else "n/a"
    cost_s = f"${cost_usd:.4f}" if cost_usd is not None else "n/a"
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail} "
          f"| tokens: {usage_s} | est. cost: {cost_s}", flush=True)
    return ok


def make_client(timeout: float) -> OpenAI:
    import os
    key = os.environ.get("MODEL_API_KEY")
    if not key:
        raise RuntimeError("MODEL_API_KEY not set (expected in .env)")
    return OpenAI(base_url=BASE_URL, api_key=key, timeout=timeout)


def find_video() -> Path:
    search_root = REPO_ROOT.parent / "meta-model-cookbook" / "07_sam"
    hits = sorted(search_root.rglob("pingpong.mp4")) if search_root.is_dir() else []
    if not hits:
        raise FileNotFoundError(f"pingpong.mp4 not found under {search_root}")
    return hits[0]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def get_file_id(client: OpenAI, video: Path) -> str:
    digest = sha256_of(video)
    cache: dict[str, Any] = {}
    if CACHE_PATH.exists():
        try:
            cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}
    entry = cache.get(digest)
    if isinstance(entry, dict) and str(entry.get("file_id", "")).startswith("file-"):
        print(f"  cache hit: sha256={digest[:12]}… -> {entry['file_id']}", flush=True)
        return str(entry["file_id"])
    print(f"  uploading {video.name} (sha256={digest[:12]}…)…", flush=True)
    with video.open("rb") as f:
        uploaded = client.files.create(
            file=(video.name, f, "video/mp4"), purpose="user_data")
    if not uploaded.id.startswith("file-"):
        raise ValueError(f"Files API returned invalid file id: {uploaded.id!r}")
    cache[digest] = {"file_id": uploaded.id, "filename": video.name}
    CACHE_PATH.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")
    print(f"  uploaded as {uploaded.id} (cached)", flush=True)
    return uploaded.id


def usage_dict(resp: Any) -> dict[str, Any] | None:
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None
    if hasattr(usage, "model_dump"):
        return usage.model_dump()
    return dict(usage)  # type: ignore[arg-type]


def token_cost(model_key: str, usage: dict[str, Any] | None) -> float | None:
    if not usage:
        return None
    in_rate, out_rate = PRICES[model_key]
    inp = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    out = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    return (inp * in_rate + out * out_rate) / 1_000_000


# --- Check 1: SAM 3.1 -------------------------------------------------------

def shim_events(stream: Any, raw_path: Path) -> list[dict[str, Any]]:
    """Port of cookbook sam_api.stream_events (sync form): the API streams
    response.content_part.done but not response.output_text.done, which the
    parser needs to close a lane, so synthesize the latter."""
    events: list[dict[str, Any]] = []
    completed: set[tuple[Any, Any, Any]] = set()
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


def check_sam() -> bool:
    client = make_client(TIMEOUTS["sam"])
    video = find_video()
    file_id = get_file_id(client, video)
    stream = client.responses.create(
        model=SAM_MODEL, stream=True,
        input=[{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": NOUN_PHRASE},
            {"type": "input_video", "file_id": file_id},
        ]}],
    )
    events = shim_events(stream, OUT_DIR / "sam_raw.jsonl")
    result = asyncio.run(_parse(events))
    if result.outcome.status != "completed":
        raise RuntimeError(f"SAM outcome={result.outcome.status!r}")
    errors = [d for d in result.diagnostics if d.severity == "error"]
    if errors:
        raise RuntimeError("parser errors: "
                           + "; ".join(f"line {d.line}: {d.message}" for d in errors))
    frames: set[int] = set()
    tracks: dict[str, set[int]] = {}
    for rec in result.records:
        if rec.frame is not None:
            frames.add(rec.frame.frame_index)
            tracks.setdefault(rec.object_id, set()).add(rec.frame.frame_index)
    summary = {
        "model": SAM_MODEL, "concept": NOUN_PHRASE, "file_id": file_id,
        "video": str(video), "frame_count": len(frames),
        "track_count": len(tracks),
        "tracks": [{"object_id": oid, "visible_frames": len(fs),
                    "first_frame": min(fs), "last_frame": max(fs)}
                   for oid, fs in sorted(tracks.items())],
        "records": len(result.records),
    }
    (OUT_DIR / "sam_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    cost = len(frames) / 1000 * SAM_USD_PER_1K_FRAMES
    ok = summary["frame_count"] >= 130 and summary["track_count"] == 1
    return record("sam", ok,
                  f"frames={summary['frame_count']} tracks={summary['track_count']} "
                  f"(need >=130 frames, exactly 1 track)",
                  usage=None, cost_usd=cost)


# --- Check 2: Spark 1.3 -----------------------------------------------------

def check_spark() -> bool:
    client = make_client(TIMEOUTS["spark"])
    file_id = get_file_id(client, find_video())
    resp = client.responses.create(
        model=SPARK_MODEL,
        input=[{"role": "user", "content": [
            {"type": "input_file", "file_id": file_id},
            {"type": "input_text", "text": SPARK_QUESTION},
        ]}],
    )
    (OUT_DIR / "spark_raw.json").write_text(
        resp.model_dump_json(indent=2) + "\n", encoding="utf-8")
    text = (resp.output_text or "").strip()
    (OUT_DIR / "spark_answer.txt").write_text(text + "\n", encoding="utf-8")
    usage = usage_dict(resp)
    return record("spark", bool(text),
                  f"answer={text[:120]!r}" if text else "empty output text",
                  usage=usage, cost_usd=token_cost("spark", usage))


# --- Check 3: Wonder --------------------------------------------------------

def check_wonder() -> bool:
    client = make_client(TIMEOUTS["wonder"])
    resp = client.responses.create(
        model=WONDER_MODEL, reasoning={"effort": "high"}, input=WONDER_PROMPT)
    (OUT_DIR / "wonder_raw.json").write_text(
        resp.model_dump_json(indent=2) + "\n", encoding="utf-8")
    text = (resp.output_text or "").strip()
    (OUT_DIR / "wonder_answer.txt").write_text(text + "\n", encoding="utf-8")
    usage = usage_dict(resp)
    return record("wonder", bool(text),
                  f"answer={text[:120]!r}" if text else "empty output text",
                  usage=usage, cost_usd=token_cost("wonder", usage))


# --- Check 4: muse exec -----------------------------------------------------

def check_muse_exec() -> bool:
    muse = shutil.which("muse")
    if not muse:
        raise RuntimeError("`muse` not found on PATH")
    # --no-session-log: the sandbox home is read-only for session logs.
    with tempfile.TemporaryDirectory(prefix="smoke-muse-") as tmp:
        tmpd = Path(tmp)
        (tmpd / "test_smoke_trivial.py").write_text(
            "def test_trivial():\n    assert 1 + 1 == 2\n", encoding="utf-8")
        proc = subprocess.run(
            [muse, "exec", "--json", "--disable-approval", "--no-session-log",
             "--max-model-steps", "10", "run pytest and report the result"],
            cwd=tmp, capture_output=True, text=True,
            timeout=TIMEOUTS["muse_exec"])
        rerun = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=tmp, capture_output=True, text=True, timeout=120)
    raw = (f"exit_code={proc.returncode}\n--- stdout ---\n{proc.stdout}\n"
           f"--- stderr ---\n{proc.stderr}\n"
           f"--- pytest rerun exit_code={rerun.returncode} ---\n"
           f"{rerun.stdout}\n{rerun.stderr}")
    (OUT_DIR / "muse_exec_raw.txt").write_text(raw, encoding="utf-8")
    ok = proc.returncode == 0 and rerun.returncode == 0
    return record("muse_exec", ok,
                  f"muse exec exit={proc.returncode}, "
                  f"pytest rerun exit={rerun.returncode}")


CHECKS = [("sam", check_sam), ("spark", check_spark),
          ("wonder", check_wonder), ("muse_exec", check_muse_exec)]


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    started = time.time()
    for name, fn in CHECKS:
        print(f"\n=== check: {name} ===", flush=True)
        try:
            fn()
        except Exception as exc:  # one failure must not stop the rest
            record(name, False, f"{type(exc).__name__}: {exc}")
    total_cost = sum(r["cost_usd"] for r in RESULTS if r["cost_usd"] is not None)
    print("\n=== summary ===")
    for r in RESULTS:
        print(f"  {'PASS' if r['pass'] else 'FAIL'}  {r['check']}")
    print(f"  estimated cost (priced checks): ${total_cost:.4f}")
    print(f"  elapsed: {time.time() - started:.1f}s")
    (OUT_DIR / "results.json").write_text(
        json.dumps({"results": RESULTS, "estimated_cost_usd": total_cost},
                   indent=2) + "\n", encoding="utf-8")
    return 0 if all(r["pass"] for r in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
