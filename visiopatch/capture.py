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
from typing import Any

from playwright.sync_api import sync_playwright

from visiopatch.bench import BENCH, REPO_ROOT, ServedCase, restore_buggy, switch_to_fix
from visiopatch.browser import VIEWPORT, assert_webgl, launch_chromium

CAPTURES = REPO_ROOT / "captures"

FPS = 15
WIDTH, HEIGHT = VIEWPORT["width"], VIEWPORT["height"]
TAIL_S = 0.5

# Generic repro-step vocabulary for capture: page interactions that make
# a reported behaviour actually appear on screen (scrolling a popup
# under a sticky header, opening a panel, hovering). Page-generic —
# selectors only, never case-specific code.
STEP_KINDS = ("scroll_to", "scroll_by", "click", "wait_ms")
MAX_STEP_WAIT_MS = 10_000


def validate_actions(actions: Any) -> list[dict[str, Any]]:
    """Normalize repro steps; ``None``/``[]`` means record at load state.

    Raises ValueError on any malformed step (unknown action, empty
    selector, bad numbers) so a typo fails fast instead of silently
    recording the wrong state.
    """
    if actions is None:
        return []
    if not isinstance(actions, list):
        raise ValueError("steps must be a list of single-key objects")
    out: list[dict[str, Any]] = []
    for i, step in enumerate(actions):
        if not isinstance(step, dict) or len(step) != 1:
            raise ValueError(f"steps[{i}] must be a single-key object")
        (kind, arg), = step.items()
        if kind == "scroll_to" or kind == "click":
            if not isinstance(arg, str) or not arg.strip():
                raise ValueError(
                    f"steps[{i}] {kind!r} needs a non-empty CSS selector")
            out.append({kind: arg.strip()})
        elif kind == "scroll_by":
            if (not isinstance(arg, (list, tuple)) or len(arg) != 2
                    or any(isinstance(v, bool) or not isinstance(v, (int, float))
                           for v in arg)):
                raise ValueError(
                    f"steps[{i}] 'scroll_by' needs [dx, dy] numbers")
            out.append({kind: [float(arg[0]), float(arg[1])]})
        elif kind == "wait_ms":
            if (isinstance(arg, bool) or not isinstance(arg, (int, float))
                    or not 0 <= arg <= MAX_STEP_WAIT_MS):
                raise ValueError(
                    f"steps[{i}] 'wait_ms' needs 0..{MAX_STEP_WAIT_MS}")
            out.append({kind: int(arg)})
        else:
            raise ValueError(
                f"steps[{i}] unknown action {kind!r} "
                f"(one of: {', '.join(STEP_KINDS)})")
    return out


def run_actions(page: Any, actions: Any) -> None:
    """Replay validated repro steps on a Playwright page (or a fake with
    ``evaluate``/``click``/``wait_for_timeout`` in tests)."""
    import json as _json
    for step in validate_actions(actions):
        if "scroll_to" in step:
            sel = _json.dumps(step["scroll_to"])
            page.evaluate(
                f"() => {{ const el = document.querySelector({sel}); "
                f"if (el) el.scrollIntoView({{block: 'center'}}); }}")
        elif "scroll_by" in step:
            dx, dy = step["scroll_by"]
            page.evaluate(f"() => window.scrollBy({dx}, {dy})")
        elif "click" in step:
            page.click(step["click"])
        elif "wait_ms" in step:
            page.wait_for_timeout(step["wait_ms"])


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


def _record(url: str, record_s: float,
            actions: Any = None) -> tuple[Path, float]:
    """Record the page; returns (webm path, load_offset_s).

    The video context is created *before* ``goto`` so frame 0 predates the
    load event. ``load_offset_s`` is seconds from recording start to the
    ``goto(..., wait_until="load")`` return. Generic repro ``actions``
    (scroll/click/wait) run after load so the clip shows the reported
    state, not just the load state; the trailing wait fills whatever
    recording time remains.
    """
    steps = validate_actions(actions)
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
            run_actions(page, steps)
            elapsed = time.monotonic() - t0
            page.wait_for_timeout(max(0, int((record_s - elapsed) * 1000)))
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


def capture(case_id: str, state: str,
            actions: Any = None) -> tuple[Path, Path]:
    """Capture one state of one case. Returns (mp4 path, meta path).

    Repro ``actions`` default to the case's ``repro.json`` steps (all
    current benches record at load state with ``[]``); an explicit list
    overrides them.
    """
    if state not in ("buggy", "fix"):
        raise ValueError(f"state must be 'buggy' or 'fix', got {state!r}")
    case = BENCH / case_id
    meta = json.loads((case / "repro.json").read_text())
    steps = (validate_actions(actions) if actions is not None
             else validate_actions(meta.get("actions")))
    duration_s = float(meta["duration_s"])
    record_s = duration_s + TAIL_S

    saved: bytes | None = None
    if state == "fix":
        # Swap in fix content before serving so serve.sh serves the fix.
        saved = switch_to_fix(case, meta)
    try:
        with ServedCase(case):
            webm, load_offset_s = _record(meta["url"], record_s, steps)
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
