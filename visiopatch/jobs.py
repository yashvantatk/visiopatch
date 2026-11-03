"""Background job store for the VisioPatch MCP server — no model calls.

Long tools return a ``job_id`` immediately; the work runs in a daemon
thread and the record under ``runs/<run_id>/jobs/<job_id>.json`` tracks
it, so a server restart loses nothing (``job_status``/``job_result``
read the same files). All paths in records are repo-relative.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from visiopatch.bench import REPO_ROOT
from visiopatch.repair import rel_text

# Tools that can exceed Muse's MCP tool timeout always run as jobs.
LONG_TOOLS = frozenset(
    {"diagnose", "generate_visual_test", "repair_and_verify"})


def jobs_dir(run_id: str) -> Path:
    dest = REPO_ROOT / "runs" / run_id / "jobs"
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def tool_timeout_s() -> float | None:
    """Muse's MCP tool timeout, when known.

    Read from ``VISIOPATCH_TOOL_TIMEOUT`` (seconds); ``None`` means the
    setting was not found — the acceptance run records the real value
    in ``runs/demo/step7_meta.json``.
    """
    raw = os.environ.get("VISIOPATCH_TOOL_TIMEOUT")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def needs_background(tool: str) -> bool:
    return tool in LONG_TOOLS


def make_job_id(run_id: str) -> str:
    return f"{run_id}.{uuid.uuid4().hex[:12]}"


def parse_job_id(job_id: str) -> tuple[str, str]:
    """Split ``<run_id>.<hex>``; run ids never contain dots."""
    run_id, _, tail = job_id.rpartition(".")
    if not run_id or not tail:
        raise ValueError(f"malformed job_id: {job_id!r}")
    return run_id, job_id


def job_path(run_id: str, job_id: str) -> Path:
    return jobs_dir(run_id) / f"{job_id}.json"


def create_job(run_id: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    job_id = make_job_id(run_id)
    now = time.time()
    record = {
        "job_id": job_id,
        "run_id": run_id,
        "tool": tool,
        "args": args,
        "status": "running",
        "created_at": now,
        "updated_at": now,
        "tool_timeout_s": tool_timeout_s(),
        "pids": [],
        "result": None,
        "error": None,
    }
    job_path(run_id, job_id).write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def load_job(job_id: str) -> dict[str, Any]:
    run_id, _ = parse_job_id(job_id)
    path = jobs_dir(run_id) / f"{job_id}.json"
    if not path.is_file():
        raise KeyError(f"unknown job_id: {job_id!r}")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_record(record: dict[str, Any]) -> None:
    record["updated_at"] = time.time()
    path = jobs_dir(record["run_id"]) / f"{record['job_id']}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def finish_job(record: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    record["status"] = "done"
    record["result"] = result
    _write_record(record)
    return record


def fail_job(record: dict[str, Any], error: str) -> dict[str, Any]:
    record["status"] = "failed"
    record["error"] = rel_text(str(error))[-2000:]
    _write_record(record)
    return record


def add_pids(record: dict[str, Any], pids: list[int]) -> dict[str, Any]:
    """Log child PIDs (e.g. nested muse exec) in the job record."""
    seen = list(dict.fromkeys([*record.get("pids", []), *pids]))
    record["pids"] = seen
    _write_record(record)
    return record


def run_in_background(record: dict[str, Any],
                      fn: Callable[[], dict[str, Any]]) -> threading.Thread:
    """Run ``fn`` in a daemon thread, persisting done/failed state."""

    def _target() -> None:
        try:
            finish_job(record, fn())
        except Exception as exc:  # jobs must never die silently
            fail_job(record, f"{type(exc).__name__}: {exc}")

    thread = threading.Thread(target=_target, daemon=True,
                              name=f"vpjob-{record['job_id']}")
    thread.start()
    return thread
