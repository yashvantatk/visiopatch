"""Shared headless-Chromium launch config for VisioPatch render/capture tools.

Tries each GL backend in order, keeps the first one that yields a WebGL
context, and caches the winner in render_out/.backend so later runs try it
first. Raises only when every backend fails. No model or network calls.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Browser, Playwright

REPO_ROOT = Path(__file__).resolve().parent.parent

# Ordered GL backends; first one with a live WebGL context wins.
BACKENDS = [
    ["--use-angle=gl-egl"],
    ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
    ["--use-gl=egl"],
]
# Last resort: backend selection plus the software-GL permission flags as one
# set, for sandboxes where a bare backend flag does not reliably enable
# software WebGL. Tried only after every BACKENDS entry has failed.
LAST_RESORT = [
    "--use-angle=gl-egl",
    "--enable-unsafe-swiftshader",
    "--ignore-gpu-blocklist",
]

CANDIDATES = BACKENDS + [LAST_RESORT]

VIEWPORT = {"width": 1280, "height": 720}

CACHE_FILE = REPO_ROOT / "render_out" / ".backend"


def _ordered_backends() -> list[list[str]]:
    try:
        cached = json.loads(CACHE_FILE.read_text()).get("backend")
    except (OSError, ValueError):
        cached = None
    ordered = [cached] if cached in CANDIDATES else []
    return ordered + [b for b in CANDIDATES if b != cached]


def _save_backend(backend: list[str]) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps({"backend": backend}) + "\n")
    except OSError:
        pass


def assert_webgl(page) -> None:
    """Fail loudly when the page has no WebGL context (kaplay cannot run)."""
    has_gl = page.evaluate(
        "() => !!document.createElement('canvas').getContext('webgl')"
    )
    if not has_gl:
        raise RuntimeError("WebGL context unavailable in headless Chromium")


def launch_chromium(pw: Playwright, **overrides) -> Browser:
    """Launch headless Chromium with the first working backend.

    An explicit ``args=`` override is honored as a single attempt and is
    never cached. Returns the open browser; the caller owns closing it.
    """
    headless = overrides.pop("headless", True)
    pinned = overrides.pop("args", None)
    # Environment binary override (sandbox use only): selects which Chromium
    # binary to launch. Backend selection below is unaffected.
    exe = os.environ.get("VISIOPATCH_CHROMIUM_EXE")
    if exe:
        overrides["executable_path"] = exe
    if "env" not in overrides:
        from visiopatch.bench import spawn_env
        overrides["env"] = spawn_env()
    candidates = [list(pinned)] if pinned is not None else _ordered_backends()
    failures: list[str] = []
    for backend in candidates:
        try:
            browser = pw.chromium.launch(headless=headless, args=list(backend),
                                         **overrides)
        except Exception as exc:
            failures.append(f"{backend}: launch failed: {exc}")
            continue
        try:
            page = browser.new_page()
            try:
                page.goto("about:blank")
                assert_webgl(page)
            finally:
                page.close()
        except Exception as exc:
            failures.append(f"{backend}: no WebGL context ({exc})")
            browser.close()
            continue
        print(f"[browser] using backend {backend}", flush=True)
        if pinned is None:
            _save_backend(backend)
        return browser
    raise RuntimeError("No working Chromium WebGL backend. Tried "
                       + "; ".join(failures))
