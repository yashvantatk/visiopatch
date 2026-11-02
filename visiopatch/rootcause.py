"""Wonder 1.0 root-cause diagnosis for VisioPatch bench cases.

Two Wonder calls per case:

``pick_files`` (reasoning effort medium) sees the one-line user report,
the sanitized reported-mode Spark analysis for the current revision,
and the source file tree at the current revision. It returns up to 5
candidate files with a reason for each.

``diagnose`` (reasoning effort high) sees the same inputs plus the
contents of the picked files (line-numbered, trimmed to stay under
60k tokens) and returns a structured diagnosis.

Both calls use Wonder exactly the way ``scripts/smoke.py`` does (sync
openai SDK, ``base_url https://api.meta.ai/v1``, ``MODEL_API_KEY`` from
``.env``), cache by sha256 of the inputs plus a prompt version so the
same inputs are never paid for twice, and validate every JSON response
against a schema before trusting it.

No leakage: prompts receive only the report line, the sanitized
reported analysis, file paths, and file contents at the current
revision. Case ids, revision hashes, PR or issue text, diffs, git
history, and state labels never enter a prompt.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from openai import OpenAI

from visiopatch.analyze import extract_json
from visiopatch.bench import BENCH, KAPLAY, case_git_dir, ensure_app, read_meta
from visiopatch.perceive import CACHE_DIR

BASE_URL = "https://api.meta.ai/v1"
WONDER_MODEL = "muse-wonder-1.0"
TIMEOUT_S = 180.0

PICK_PROMPT_VERSION = 1
DIAGNOSE_PROMPT_VERSION = 2

PICK_EFFORT = "medium"
DIAGNOSE_EFFORT = "high"

# PROJECT.md decision 6: USD per 1M tokens (in/out) for Wonder.
WONDER_PRICES = (4.0, 20.0)

MAX_DIAGNOSE_TOKENS = 60_000
CHARS_PER_TOKEN = 4

SOURCE_SUFFIXES = {".ts", ".tsx", ".js", ".mjs", ".html", ".css"}

REPORTED_KEYS = ("anomaly", "frame", "time_s", "track_ids", "bbox",
                 "observed", "expected", "confidence")

WonderFn = Callable[[str, str], tuple[str, dict[str, Any] | None]]


class BudgetExceeded(Exception):
    """Raised by a budget callback before a call that would overspend."""


def make_client(timeout: float = TIMEOUT_S) -> OpenAI:
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
    in_rate, out_rate = WONDER_PRICES
    inp = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    out = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    return (inp * in_rate + out * out_rate) / 1_000_000


def _call_wonder(prompt: str, effort: str) -> tuple[str, dict[str, Any] | None]:
    """One live Wonder text call, shaped exactly like scripts/smoke.py."""
    from visiopatch.bench import REPO_ROOT
    load_dotenv(REPO_ROOT / ".env")
    client = make_client()
    resp = client.responses.create(
        model=WONDER_MODEL, reasoning={"effort": effort}, input=prompt)
    return (resp.output_text or "").strip(), usage_dict(resp)


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


# --- local inputs (never sent verbatim beyond the allowed fields) -----------

def sanitize_reported(doc: dict[str, Any]) -> dict[str, Any]:
    """Only the observational fields of a reported analysis are allowed."""
    return {k: doc.get(k) for k in REPORTED_KEYS}


def load_reported(case_id: str) -> dict[str, Any]:
    from visiopatch.bench import REPO_ROOT
    path = REPO_ROOT / "analysis" / case_id / "buggy.reported.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    return sanitize_reported(doc)


def _case_path(case_id: str) -> Path:
    case = BENCH / case_id
    if not (case / "repro.json").is_file():
        raise ValueError(f"unknown case: {case_id!r}")
    return case


def _repo_dir(case_id: str) -> Path:
    case = _case_path(case_id)
    meta = read_meta(case_id)
    if (case / "app.bundle").is_file():
        ensure_app(case, meta)
    return case_git_dir(case)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True).stdout


def source_tree(case_id: str) -> list[str]:
    """Source file paths at the current revision, sorted."""
    meta = read_meta(case_id)
    repo = _repo_dir(case_id)
    names = _git(repo, "ls-tree", "-r", "--name-only", meta["buggy_sha"])
    return sorted(
        p for p in names.splitlines()
        if Path(p).suffix in SOURCE_SUFFIXES and p.strip())


def file_contents(case_id: str, paths: list[str]) -> dict[str, str]:
    """File contents at the current revision, in the order given."""
    meta = read_meta(case_id)
    repo = _repo_dir(case_id)
    out: dict[str, str] = {}
    for p in paths:
        out[p] = _git(repo, "show", f"{meta['buggy_sha']}:{p}")
    return out


# --- trimming -----------------------------------------------------------------

def _numbered(path: str, text: str, first: int, last: int) -> str:
    lines = text.splitlines()
    body = "\n".join(f"{i + 1}: {lines[i]}" for i in range(first, last))
    return f"--- {path} (lines {first + 1}-{last} of {len(lines)}) ---\n{body}"


def trim_contents(contents: dict[str, str],
                  token_budget: int) -> dict[str, str]:
    """Line-numbered file blocks trimmed to a shared token budget.

    Files keep their picked order. A file that does not fit whole keeps
    its head and tail line ranges (original numbering) with the omitted
    range noted; later files get whatever budget remains.
    """
    out: dict[str, str] = {}
    remaining_chars = token_budget * CHARS_PER_TOKEN
    for path, text in contents.items():
        if remaining_chars <= 0:
            out[path] = f"--- {path} (omitted: token budget exhausted) ---"
            continue
        lines = text.splitlines()
        full = _numbered(path, text, 0, len(lines))
        if len(full) <= remaining_chars:
            out[path] = full
            remaining_chars -= len(full)
            continue
        # Keep head 70% / tail 30% of the remaining budget, on line bounds.
        avail = remaining_chars
        head_chars = int(avail * 0.7)
        head_n = 0
        used = 0
        while head_n < len(lines):
            used += len(lines[head_n]) + 8
            if used > head_chars:
                break
            head_n += 1
        tail_n = len(lines)
        used = 0
        tail_chars = avail - head_chars
        while tail_n > head_n:
            used += len(lines[tail_n - 1]) + 8
            if used > tail_chars:
                break
            tail_n -= 1
        if head_n == 0 and tail_n == len(lines):
            tail_n = head_n + 1  # always show at least one line
        parts = []
        if head_n > 0:
            parts.append(_numbered(path, text, 0, head_n))
        if tail_n > head_n:
            if parts:
                parts.append(f"... ({tail_n - head_n} lines omitted) ...")
            if tail_n < len(lines):
                parts.append(_numbered(path, text, tail_n, len(lines)))
        block = "\n".join(parts) if parts else _numbered(path, text, 0, 1)
        out[path] = block
        remaining_chars -= len(block)
    return out


# --- prompts (generic only; never tuned per case) -----------------------------

def build_pick_prompt(report: str, reported: dict[str, Any],
                      tree: list[str]) -> str:
    return (
        "You are locating the source files most likely responsible for "
        "a reported visual behaviour in a web page or canvas game.\n"
        f"A user reported: {report}\n"
        "An automated video analysis of the current page, prompted with "
        "that report, produced this observation (it is a hint and may be "
        "wrong):\n"
        f"{json.dumps(reported, indent=None)}\n"
        "The page is built from these source files:\n"
        + "\n".join(tree) + "\n"
        "Pick at most 5 files that most likely contain the logic behind "
        "the reported behaviour, in order of likelihood, each with a "
        "short reason. Reply with JSON only, no other text: "
        '{"files": [{"path": "source path", "why": "short reason"}]}'
    )


def build_diagnose_prompt(report: str, reported: dict[str, Any],
                          picked: list[dict[str, str]],
                          contents: dict[str, str]) -> str:
    files_block = "\n\n".join(contents.get(p["path"], "") for p in picked)
    return (
        "You are finding the root cause of a reported visual behaviour "
        "in a web page or canvas game, from its source code.\n"
        f"A user reported: {report}\n"
        "An automated video analysis of the current page, prompted with "
        "that report, produced this observation (it is a hint and may be "
        "wrong):\n"
        f"{json.dumps(reported, indent=None)}\n"
        "Candidate files, with line numbers:\n"
        f"{files_block}\n"
        "Identify the single root cause: the specific logic that makes "
        "the reported behaviour happen. Also name the function or method "
        "containing that logic, state what an automated page test should "
        "assert to tell correct behaviour from the reported behaviour "
        "(measured quantities and direction of change, not source text), "
        "and propose exactly 3 distinct strategies for correcting the "
        "logic, differing in where or how they intervene, each naming "
        "the candidate files it would touch. Reply with JSON only, no "
        "other text, with exactly these keys: "
        '{"root_cause": "one-sentence statement of the cause", '
        '"file": "one of the candidate file paths", '
        '"line_range": [first line, last line], '
        '"suspect_function": "function or method containing the logic", '
        '"mechanism": "how that logic produces the reported behaviour", '
        '"test_spec": "what a page test should measure and assert", '
        '"fix_strategies": [{"name": "short strategy name", '
        '"files": ["candidate file paths it would touch"], '
        '"approach": "how it corrects the logic"}], '
        '"confidence": a number from 0 to 1}'
    )


# --- schema validation --------------------------------------------------------

def validate_pick_doc(doc: Any, tree: list[str]) -> list[str]:
    errs: list[str] = []
    if not isinstance(doc, dict):
        return ["pick doc must be an object"]
    files = doc.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= 5:
        return ["'files' must be a list of 1 to 5 entries"]
    seen: set[str] = set()
    tree_set = set(tree)
    for i, entry in enumerate(files):
        if not isinstance(entry, dict):
            errs.append(f"files[{i}] must be an object")
            continue
        path, why = entry.get("path"), entry.get("why")
        if not isinstance(path, str) or not path.strip():
            errs.append(f"files[{i}].path must be a non-empty string")
        elif path not in tree_set:
            errs.append(f"files[{i}].path {path!r} is not in the source tree")
        elif path in seen:
            errs.append(f"files[{i}].path is a duplicate")
        else:
            seen.add(path)
        if not isinstance(why, str) or not why.strip():
            errs.append(f"files[{i}].why must be a non-empty string")
    return errs


def validate_diagnose_doc(doc: Any, picked_paths: list[str],
                          line_counts: dict[str, int]) -> list[str]:
    errs: list[str] = []
    if not isinstance(doc, dict):
        return ["diagnose doc must be an object"]
    for key in ("root_cause", "file", "line_range", "suspect_function",
                "mechanism", "test_spec", "fix_strategies", "confidence"):
        if key not in doc:
            errs.append(f"missing key: {key}")
    if errs:
        return errs
    for key in ("root_cause", "mechanism", "suspect_function", "test_spec"):
        if not isinstance(doc[key], str) or not doc[key].strip():
            errs.append(f"'{key}' must be a non-empty string")
    strategies = doc["fix_strategies"]
    if not isinstance(strategies, list) or len(strategies) != 3:
        errs.append("'fix_strategies' must be a list of exactly 3 strategies")
    else:
        names: set[str] = set()
        approaches: set[str] = set()
        for i, s in enumerate(strategies):
            if not isinstance(s, dict):
                errs.append(f"fix_strategies[{i}] must be an object")
                continue
            name, files, approach = s.get("name"), s.get("files"), s.get("approach")
            if not isinstance(name, str) or not name.strip():
                errs.append(f"fix_strategies[{i}].name must be a non-empty string")
            elif name.strip().lower() in names:
                errs.append(f"fix_strategies[{i}].name is a duplicate")
            else:
                names.add(name.strip().lower())
            if not isinstance(approach, str) or not approach.strip():
                errs.append(f"fix_strategies[{i}].approach must be a non-empty string")
            elif approach.strip().lower() in approaches:
                errs.append(f"fix_strategies[{i}].approach is a duplicate")
            else:
                approaches.add(approach.strip().lower())
            if (not isinstance(files, list) or not files
                    or not all(isinstance(f, str) for f in files)):
                errs.append(f"fix_strategies[{i}].files must be a non-empty list of paths")
            elif any(f not in picked_paths for f in files):
                errs.append(f"fix_strategies[{i}].files must be candidate file paths")
    if doc["file"] not in picked_paths:
        errs.append("'file' must be one of the candidate file paths")
    lr = doc["line_range"]
    if (not isinstance(lr, list) or len(lr) != 2
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in lr)):
        errs.append("'line_range' must be [first line, last line] integers")
    elif doc["file"] in line_counts:
        first, last = lr
        if not 1 <= first <= last <= line_counts[doc["file"]]:
            errs.append("'line_range' must lie inside the named file")
    conf = doc["confidence"]
    if (not isinstance(conf, (int, float)) or isinstance(conf, bool)
            or not math.isfinite(conf) or not 0.0 <= conf <= 1.0):
        errs.append("'confidence' must be a number from 0 to 1")
    return errs


# --- caching ------------------------------------------------------------------

def _cache_path(kind: str, version: int, payload: dict[str, Any]) -> Path:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return CACHE_DIR / f"{digest[:16]}_wonder-{kind}-v{version}-{digest[16:28]}.json"


def _read_cache(path: Path, validator: Callable[[Any], list[str]]) -> dict | None:
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return doc if not validator(doc) else None


def _wonder_json(prompt: str, effort: str, kind: str, version: int,
                 payload: dict[str, Any],
                 validator: Callable[[Any], list[str]],
                 wonder_fn: WonderFn,
                 budget_check: Callable[[float], None] | None,
                 est_usd: float) -> dict[str, Any]:
    path = _cache_path(kind, version, payload)
    cached = _read_cache(path, validator)
    if cached is not None:
        return {**cached, "cost_usd": 0.0, "cached": True, "usage": None}
    if budget_check is not None:
        budget_check(est_usd)
    text, usage = wonder_fn(prompt, effort)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{path.stem}_raw.txt").write_text(text + "\n", encoding="utf-8")
    try:
        doc = extract_json(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Wonder {kind} reply is not JSON: {exc}") from exc
    errs = validator(doc)
    if errs:
        raise ValueError(f"invalid {kind} doc: " + "; ".join(errs))
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return {**doc, "cost_usd": token_cost_usd(usage), "cached": False,
            "usage": usage}


# --- public API ---------------------------------------------------------------

def pick_files(case_id: str, wonder_fn: WonderFn = _call_wonder,
               budget_check: Callable[[float], None] | None = None,
               ) -> dict[str, Any]:
    """First Wonder call: up to 5 candidate files for one case."""
    meta = read_meta(case_id)
    report = str(meta["report"])
    reported = load_reported(case_id)
    tree = source_tree(case_id)
    prompt = build_pick_prompt(report, reported, tree)
    payload = {"kind": "pick", "report": report, "reported": reported,
               "tree": tree}
    result = _wonder_json(
        prompt, PICK_EFFORT, "pick", PICK_PROMPT_VERSION, payload,
        lambda d: validate_pick_doc(d, tree), wonder_fn, budget_check, 0.25)
    result.update({"model": WONDER_MODEL,
                   "prompt_version": PICK_PROMPT_VERSION,
                   "effort": PICK_EFFORT})
    return result


def diagnose(case_id: str, picked: list[dict[str, str]],
             wonder_fn: WonderFn = _call_wonder,
             budget_check: Callable[[float], None] | None = None,
             ) -> dict[str, Any]:
    """Second Wonder call: structured root cause for one case."""
    meta = read_meta(case_id)
    report = str(meta["report"])
    reported = load_reported(case_id)
    picked_paths = [p["path"] for p in picked]
    raw_contents = file_contents(case_id, picked_paths)
    line_counts = {p: len(t.splitlines()) for p, t in raw_contents.items()}
    overhead = estimate_tokens(build_diagnose_prompt(report, reported, picked, {}))
    contents = trim_contents(
        raw_contents, max(1_000, MAX_DIAGNOSE_TOKENS - overhead))
    prompt = build_diagnose_prompt(report, reported, picked, contents)
    if estimate_tokens(prompt) > MAX_DIAGNOSE_TOKENS:
        contents = trim_contents(raw_contents, MAX_DIAGNOSE_TOKENS // 2)
        prompt = build_diagnose_prompt(report, reported, picked, contents)
    payload = {"kind": "diagnose", "report": report, "reported": reported,
               "picked": picked, "contents": contents}
    result = _wonder_json(
        prompt, DIAGNOSE_EFFORT, "diagnose", DIAGNOSE_PROMPT_VERSION, payload,
        lambda d: validate_diagnose_doc(d, picked_paths, line_counts),
        wonder_fn, budget_check, 0.75)
    result.update({"model": WONDER_MODEL,
                   "prompt_version": DIAGNOSE_PROMPT_VERSION,
                   "effort": DIAGNOSE_EFFORT})
    return result


def root_cause(case_id: str, wonder_fn: WonderFn = _call_wonder,
               budget_check: Callable[[float], None] | None = None,
               ) -> dict[str, Any]:
    """Both Wonder calls for one case, with combined cost."""
    picked = pick_files(case_id, wonder_fn, budget_check)
    diagnosis = diagnose(case_id, picked["files"], wonder_fn, budget_check)
    return {
        "case_id": case_id,
        "pick": picked,
        "diagnosis": diagnosis,
        "cost_usd": picked["cost_usd"] + diagnosis["cost_usd"],
    }
