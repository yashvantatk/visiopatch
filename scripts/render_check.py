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

import hashlib
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from visiopatch.browser import VIEWPORT, assert_webgl, launch_chromium
BENCH = REPO_ROOT / "bench"
OUT = REPO_ROOT / "render_out"
KAPLAY = REPO_ROOT.parent / "kaplay"

TIMES_MS = (("1s", 1000), ("3s", 2000), ("5s", 2000))


def sh(*cmd: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        list(cmd), cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def wait_for_port(port: int, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"port {port} never opened")


def case_git_dir(case: Path) -> Path:
    if (case / "app" / ".git").is_dir():
        return case / "app"
    return KAPLAY


def ensure_app(case: Path, meta: dict) -> None:
    """Restore a gitignored app/ from the committed bundle, then check out
    buggy_sha, so buggy_sha and fix_sha resolve on fresh clones."""
    app, bundle = case / "app", case / "app.bundle"
    if app.joinpath(".git").is_dir():
        return
    if not bundle.is_file():
        raise ValueError(f"{case.name}: app/ missing and no app.bundle to restore from")
    if app.exists():
        shutil.rmtree(app)
    sh("git", "clone", "-q", str(bundle), str(app))
    sh("git", "-C", str(app), "checkout", "-q", meta["buggy_sha"])


def check_shas(case: Path, meta: dict) -> None:
    for key in ("buggy_sha", "fix_sha"):
        sha = meta[key]
        if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError(f"{case.name}: {key} is not a full commit SHA: {sha!r}")
    if not (KAPLAY / ".git").is_dir() and not (case / "app" / ".git").is_dir():
        raise ValueError(f"{case.name}: no git repo available for SHA check")
    if not shutil.which("git"):
        raise ValueError("git not found on PATH")
    parent = sh("git", "-C", str(case_git_dir(case)), "rev-parse", meta["fix_sha"] + "^")
    if parent != meta["buggy_sha"]:
        raise ValueError(
            f"{case.name}: buggy_sha {meta['buggy_sha']} != rev-parse {meta['fix_sha']}^ ({parent})"
        )
    print(f"[ok] {case.name}: buggy_sha == rev-parse fix_sha^", flush=True)


def switch_to_fix(case: Path, meta: dict) -> bytes | None:
    """Serve fix_sha content through the same path serve.sh serves. Returns restore token."""
    if (case / "app" / ".git").is_dir():
        sh("git", "-C", str(case / "app"), "checkout", "-q", meta["fix_sha"])
        return None
    saved = (case / "kaplay.js").read_bytes()
    fix = case / "kaplay.fix.js"
    if not fix.is_file():
        raise ValueError(f"{case.name}: missing pinned fix bundle {fix}")
    shutil.copyfile(fix, case / "kaplay.js")
    return saved


def restore_buggy(case: Path, meta: dict, saved: bytes | None) -> None:
    if (case / "app" / ".git").is_dir():
        sh("git", "-C", str(case / "app"), "checkout", "-q", meta["buggy_sha"])
        head = sh("git", "-C", str(case / "app"), "rev-parse", "HEAD")
        if head != meta["buggy_sha"]:
            raise ValueError(f"{case.name}: restore failed, HEAD={head}")
        return
    assert saved is not None
    (case / "kaplay.js").write_bytes(saved)
    digest = hashlib.sha256((case / "kaplay.js").read_bytes()).hexdigest()
    if digest != hashlib.sha256(saved).hexdigest():
        raise ValueError(f"{case.name}: kaplay.js restore hash mismatch")


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
    port = int(meta["url"].rsplit(":", 1)[1].rstrip("/"))
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

    proc = subprocess.Popen(["bash", str(case / "serve.sh")])
    try:
        wait_for_port(port)
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
    finally:
        proc.terminate()
        proc.wait(timeout=10)
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
