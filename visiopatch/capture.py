"""Video capture for VisioPatch bench repros — no model calls.

``capture(case_id, state)`` records the repro page with Playwright's
``record_video`` for ``duration_s + 0.5`` s, then transcodes the result to
H.264 MP4 (15 fps, 1280x720, yuv420p) plus a ``.meta.json`` sidecar with
``load_offset_s`` so later stages can map frames to page time.

Recording starts before the page loads: the video context is created first
and ``page.goto`` runs while recording, so frame 0 predates the load event.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from visiopatch.bench import BENCH, REPO_ROOT, ServedCase, restore_buggy, switch_to_fix
from visiopatch.browser import VIEWPORT, assert_webgl, launch_chromium

CAPTURES = REPO_ROOT / "captures"

FPS = 15
WIDTH, HEIGHT = VIEWPORT["width"], VIEWPORT["height"]
TAIL_S = 0.5


def build_meta(case_id: str, state: str, url: str, duration_s: float,
               record_s: float, load_offset_s: float, frames: int,
               sha256: str) -> dict:
    """Pure meta sidecar builder (kept separate for offline testing)."""
    return {
        "case_id": case_id,
        "state": state,
        "url": url,
        "duration_s": duration_s,
        "record_s": record_s,
        "load_offset_s": load_offset_s,
        "fps": FPS,
        "width": WIDTH,
        "height": HEIGHT,
        "frames": frames,
        "sha256": sha256,
    }


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def transcode(webm: Path, mp4: Path, record_s: float) -> None:
    mp4.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(webm),
         "-t", str(record_s),
         "-r", str(FPS), "-s", f"{WIDTH}x{HEIGHT}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(mp4)],
        check=True,
    )


def probe_frames(mp4: Path, record_s: float) -> int:
    """Actual encoded frame count; falls back to the expected count."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-count_frames",
             "-select_streams", "v:0",
             "-show_entries", "stream=nb_read_frames",
             "-of", "default=nokey=1:noprint_wrappers=1", str(mp4)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return int(out)
    except (subprocess.CalledProcessError, ValueError):
        return round(record_s * FPS)


def _record(url: str, record_s: float) -> tuple[Path, float]:
    """Record the page; returns (webm path, load_offset_s).

    The video context is created *before* ``goto`` so frame 0 predates the
    load event. ``load_offset_s`` is seconds from recording start to the
    ``goto(..., wait_until="load")`` return.
    """
    tmp = Path(tempfile.mkdtemp(prefix="visiopatch-cap-"))
    with sync_playwright() as pw:
        browser = launch_chromium(pw)
        try:
            ctx = browser.new_context(
                viewport=VIEWPORT,
                record_video_dir=str(tmp),
                record_video_size=VIEWPORT,
            )
            page = ctx.new_page()
            t0 = time.monotonic()  # ~recording start (video begins at page creation)
            page.goto(url, wait_until="load", timeout=30000)
            load_offset_s = time.monotonic() - t0
            assert_webgl(page)
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)[:200]))
            page.wait_for_timeout(max(0, int((record_s - load_offset_s) * 1000)))
            if errors:
                raise RuntimeError(f"page errors during capture: {errors[:2]}")
            ctx.close()  # flush the recording to disk
        finally:
            browser.close()
    webms = sorted(tmp.glob("*.webm"))
    if not webms:
        raise RuntimeError(f"no recording produced in {tmp}")
    final = tmp / "capture.webm"
    webms[0].rename(final)
    return final, load_offset_s


def capture(case_id: str, state: str) -> tuple[Path, Path]:
    """Capture one state of one case. Returns (mp4 path, meta path)."""
    if state not in ("buggy", "fix"):
        raise ValueError(f"state must be 'buggy' or 'fix', got {state!r}")
    case = BENCH / case_id
    meta = json.loads((case / "repro.json").read_text())
    if meta.get("actions"):
        raise ValueError(f"{case_id}: non-empty actions not supported yet: {meta['actions']}")
    duration_s = float(meta["duration_s"])
    record_s = duration_s + TAIL_S

    saved: bytes | None = None
    if state == "fix":
        # Swap in fix content before serving so serve.sh serves the fix.
        saved = switch_to_fix(case, meta)
    try:
        with ServedCase(case):
            webm, load_offset_s = _record(meta["url"], record_s)
    finally:
        if state == "fix":
            restore_buggy(case, meta, saved)

    out_dir = CAPTURES / case_id
    mp4 = out_dir / f"{state}.mp4"
    transcode(webm, mp4, record_s)
    digest = sha256_of(mp4)
    frames = probe_frames(mp4, record_s)
    sidecar = out_dir / f"{state}.meta.json"
    sidecar.write_text(json.dumps(build_meta(
        case_id, state, meta["url"], duration_s, record_s,
        round(load_offset_s, 3), frames, digest), indent=2) + "\n")
    print(f"[capture] {mp4.relative_to(REPO_ROOT)} "
          f"frames={frames} load_offset_s={load_offset_s:.2f}", flush=True)
    return mp4, sidecar
