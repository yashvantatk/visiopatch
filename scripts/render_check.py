#!/usr/bin/env python3
"""Render check for VisioPatch bench repros — no model or network calls.

For each bench/<id>/repro.json: starts serve.sh, waits for the port,
screenshots the page at 1s/3s/5s at buggy_sha, switches the served content
to fix_sha (DOM cases: git checkout in app/; kaplay cases: swap in the
pinned kaplay.fix.js bundle), screenshots again, restores buggy content,
kills the server. Also verifies every buggy_sha is the full parent commit
of its fix_sha (kaplay SHAs against ../kaplay, DOM SHAs against app/).

Usage: .venv/bin/python scripts/render_check.py [--only id1,id2]
Outputs: render_out/<id>/{buggy,fix}_{1s,3s,5s}.png
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from visiopatch.bench import (  # noqa: E402
    BENCH,
    ServedCase,
    check_shas,
    ensure_app,
    restore_buggy,
    switch_to_fix,
)
from visiopatch.browser import VIEWPORT, assert_webgl, launch_chromium  # noqa: E402

OUT = REPO_ROOT / "render_out"

TIMES_MS = (("1s", 1000), ("3s", 2000), ("5s", 2000))


def shoot(page, out_dir: Path, tag: str) -> list[Path]:
    paths = []
    for name, wait_ms in TIMES_MS:
        page.wait_for_timeout(wait_ms)
        path = out_dir / f"{tag}_{name}.png"
        page.screenshot(path=str(path))
        paths.append(path)
    return paths


def run_case(launch, case: Path) -> list[Path]:
    meta = json.loads((case / "repro.json").read_text())
    out_dir = OUT / case.name
    out_dir.mkdir(parents=True, exist_ok=True)

    # A fresh browser per phase: the fix content is swapped in under the same
    # URLs, and a reload must never serve the buggy bytes from HTTP cache.
    # Playwright uses a fresh profile per launch, so a new browser cannot
    # reuse anything cached during the buggy phase.
    def fresh_page():
        b = launch()
        ctx = b.new_context(viewport=VIEWPORT)
        page = ctx.new_page()
        page.goto(meta["url"], wait_until="load", timeout=30000)
        assert_webgl(page)
        return b, page

    with ServedCase(case):
        browser, page = fresh_page()
        try:
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)[:200]))
            paths = shoot(page, out_dir, "buggy")
            if errors:
                raise RuntimeError(f"{case.name}: page errors: {errors[:2]}")
        finally:
            browser.close()
        saved = switch_to_fix(case, meta)
        try:
            browser, page = fresh_page()
            try:
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)[:200]))
                paths += shoot(page, out_dir, "fix")
                if errors:
                    raise RuntimeError(f"{case.name}: page errors at fix: {errors[:2]}")
            finally:
                browser.close()
        finally:
            restore_buggy(case, meta, saved)
    for p in paths:
        print(f"  {p.relative_to(REPO_ROOT)}", flush=True)
    return paths


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Screenshot bench repros at buggy and fix SHAs")
    parser.add_argument("--only", default="",
                        help="comma-separated case ids to run (default: all)")
    only = {s for s in parser.parse_args().only.split(",") if s}
    cases = sorted(p for p in BENCH.iterdir() if (p / "repro.json").is_file())
    if not cases:
        print("no bench cases found", flush=True)
        return 1
    if only:
        unknown = only - {c.name for c in cases}
        if unknown:
            print(f"unknown case(s): {sorted(unknown)}", flush=True)
            return 1
        cases = [c for c in cases if c.name in only]
    metas = {c: json.loads((c / "repro.json").read_text()) for c in cases}
    for case in cases:
        if (case / "app.bundle").is_file():
            ensure_app(case, metas[case])
    for case in cases:
        check_shas(case, metas[case])
    all_paths: list[Path] = []
    with sync_playwright() as pw:
        launch = lambda: launch_chromium(pw)  # noqa: E731
        for case in cases:
            print(f"--- {case.name} ---", flush=True)
            all_paths += run_case(launch, case)
    print(f"\nwrote {len(all_paths)} screenshots under render_out/", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
