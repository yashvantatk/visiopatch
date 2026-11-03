"""Shared bench harness for VisioPatch render/capture tools.

Moved here from ``scripts/render_check.py`` so render and capture share one
serve / switch / restore implementation. No model or network calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BENCH = REPO_ROOT / "bench"
KAPLAY = REPO_ROOT.parent / "kaplay"


def spawn_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for harness-spawned subprocesses (pytest, muse exec,
    browsers): the parent env minus ``PYTHONPATH``. A terminal-inherited
    ``PYTHONPATH`` (e.g. a ROS plugin path) breaks pytest collection
    inside served and candidate runs."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    if extra:
        env.update(extra)
    return env


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


def read_meta(case_id: str) -> dict:
    case = BENCH / case_id
    return json.loads((case / "repro.json").read_text())


def case_ids(only: str = "") -> list[str]:
    cases = sorted(p.name for p in BENCH.iterdir() if (p / "repro.json").is_file())
    if not only:
        return cases
    wanted = {s for s in only.split(",") if s}
    unknown = wanted - set(cases)
    if unknown:
        raise ValueError(f"unknown case(s): {sorted(unknown)}")
    return [c for c in cases if c in wanted]


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


class ServedCase:
    """serve.sh running for one case; terminates on close."""

    def __init__(self, case: Path):
        self.case = case
        self.meta = json.loads((case / "repro.json").read_text())
        self.port = int(self.meta["url"].rsplit(":", 1)[1].rstrip("/"))
        self.proc: subprocess.Popen | None = None

    def start(self) -> "ServedCase":
        if (self.case / "app.bundle").is_file():
            ensure_app(self.case, self.meta)
        self.proc = subprocess.Popen(["bash", str(self.case / "serve.sh")])
        wait_for_port(self.port)
        return self

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            self.proc.wait(timeout=10)
            self.proc = None

    def __enter__(self) -> "ServedCase":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
