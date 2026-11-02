"""Muse Code worktree fan-out repair + Gate 2 for VisioPatch bench cases.

Per case (only Gate 1 ``gate_pass`` cases): build one offline snapshot at
``buggy_sha`` under ``runs/<id>/repair/src`` (``git archive`` + fresh
``git init`` with a single ``baseline`` commit, so the candidate never sees
the upstream repo or its fix refs), write one ``prompts/repair_<id>_s<N>.txt``
per fix strategy, run one ``muse exec`` candidate per strategy in muse's own
worktree mode, then Gate 2 each candidate in its worktree:

1. The generated Gate 1 test passes.
2. The target repo's existing suite passes if it has one (kaplay: at minimum
   ``npm run check``; ``playwright test`` only with a 5-minute budget, else
   the reason is recorded; d1 app: ``none``). The name is recorded.
3. Visual confirm: rebuild, recapture with ``capture.py`` (15 fps, 1280x720),
   rerun SAM with the case noun list (hash-cached), save one screenshot.
   Spark reported-mode is informational only and never gates.

Guards: prompt leak validation (no fix_sha/buggy_sha, PR/issue text, diffs,
git log, or generated test source); snapshot isolation test (exactly 1 commit,
no remotes); per-candidate contamination scan of the JSONL log (web/fetch
tool calls or github.com references mark the candidate contaminated and
ineligible); allowlist diff guard on the snapshot (kaplay: only ``src/**``;
d1: only the app sources; package.json, lockfiles, configs, test dirs and
build output are always rejected; lines changed are counted on allowed files
only). The smallest passing diff (allowed lines changed) is picked; a failed,
contaminated, or guard-rejected candidate is never picked.

Harness-only scoring (never fed to any model): whether the picked diff
touches the same files as the human fix. The harness restores bench state to
buggy_sha afterwards and verifies it. No model or network calls in tests.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from visiopatch.bench import BENCH, KAPLAY, REPO_ROOT, case_git_dir, read_meta, sh

REPAIR_PROMPT_VERSION = 1
MAX_MODEL_STEPS = 40

PROMPTS_DIR = REPO_ROOT / "prompts"

# Resolved from `muse exec --help` plus live probes: `-w existing` with a
# caller-owned worktree nested in the snapshot (`git worktree add
# <snapshot>/<tag>`) keeps the candidate diff after the run ("caller-owned
# worktree retained"), while `-w create` session worktrees are cleaned up.
# `--subagent-worktree-isolation` is the compatibility flag the task names.
# Recorded verbatim in gate2.json per candidate.
MUSE_EXEC_FLAGS = [
    "exec", "--json", "--disable-approval", "--trust-workspace",
    "--max-model-steps", str(MAX_MODEL_STEPS),
    "--subagent-worktree-isolation", "-w", "existing",
]


def make_worktree(snapshot: Path, tag: str) -> Path:
    """Create a caller-owned candidate worktree nested in the snapshot."""
    dest = snapshot / f"work_{tag}"
    if dest.exists():
        sh("git", "worktree", "remove", "--force", str(dest), cwd=snapshot)
    sh("git", "worktree", "add", str(dest), "-b", f"cand-{tag}",
       cwd=snapshot)
    return dest

_BANNED_PROMPT_TEXT = (
    "fix_sha", "buggy_sha", "MODEL_API_KEY", "api.meta.ai",
    "pull request", "github.com",
)
_STATE_LABEL_RE = re.compile(r"\bfix(ed)?\b|\bbug(gy)?\b", re.IGNORECASE)
_HEX40_RE = re.compile(r"[0-9a-f]{40}", re.IGNORECASE)
_DIFF_MARKER_RE = re.compile(
    r"^diff --git|^@@ |^\+{3} |^--- |git log", re.IGNORECASE | re.MULTILINE)
_TEST_SOURCE_MARKERS = ("test_reported_symptom", "launch_chromium",
                        "VISIOPATCH_CASE_URL")

# Contamination rule v2 (inputs-only): flag upstream references or
# network tools only inside tool-call INPUTS — fetch/web tool uses and
# shell commands such as curl, wget, git clone/fetch, gh, or npm view.
# Text the model read from local files (tool outputs, e.g. package.json
# content echoing a homepage URL) never counts. Rule v1 scanned the whole
# log and flagged s3 for a local package.json read; the v1 flag is kept on
# that candidate as a note.
_FETCH_TOOL_RE = re.compile(r"tool\.[a-z_]*(web|fetch|url|curl|wget)[a-z_]*",
                            re.IGNORECASE)
_INPUT_UPSTREAM_RES = (
    re.compile(r"github\.com|raw\.githubusercontent\.com|gitlab\.com",
               re.IGNORECASE),
    re.compile(r"\bcurl\b|\bwget\b", re.IGNORECASE),
    re.compile(r"\bgit\s+(clone|fetch|ls-remote)\b", re.IGNORECASE),
    re.compile(r"\bgh\s+(pr|issue|repo|api|search)\b", re.IGNORECASE),
    re.compile(r"\bnpm\s+view\b", re.IGNORECASE),
)
# The candidate's invoked shell commands echo through its tool results as
# an escaped `"command": "..."` field; those strings are the scan input.
_COMMAND_ECHO_RE = re.compile(r'\\"command\\": \\"(.{0,400}?)\\"')

_D1_ALLOWED_SUFFIXES = {".html", ".css", ".js"}
_REJECT_NAMES = {
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "tsconfig.json", "tsconfig.build.json", "eslint.config.mjs", "dprint.json",
    "playwright.config.ts",
}


def rel_str(value: str) -> str:
    """Render a path repo-relative for records; other text untouched."""
    prefix = str(REPO_ROOT) + "/"
    if value == str(REPO_ROOT):
        return "."
    if value.startswith(prefix):
        return value[len(prefix):]
    return value


def rel_text(text: str) -> str:
    """Strip the repo-root prefix from recorded text (e.g. tool tails)."""
    return text.replace(str(REPO_ROOT) + "/", "")


def sanitize_record(doc: Any) -> Any:
    """Walk a JSON-able record, making every repo-root path relative."""
    if isinstance(doc, dict):
        return {k: sanitize_record(v) for k, v in doc.items()}
    if isinstance(doc, list):
        return [sanitize_record(v) for v in doc]
    if isinstance(doc, str):
        return rel_text(doc)
    return doc


def is_kaplay_case(case_id: str) -> bool:
    return not (BENCH / case_id / "app").exists() and not (
        BENCH / case_id / "app.bundle").exists()


def snapshot_dir(case_id: str) -> Path:
    return REPO_ROOT / "runs" / case_id / "repair" / "src"


def build_snapshot(case_id: str) -> Path:
    """Export buggy_sha into runs/<id>/repair/src as a single-commit repo."""
    case = BENCH / case_id
    meta = read_meta(case_id)
    dest = snapshot_dir(case_id)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    repo = case_git_dir(case)
    archive = subprocess.run(
        ["git", "archive", meta["buggy_sha"]], cwd=repo,
        capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(dest)],
                   input=archive, check=True)
    sh("git", "init", "-q", cwd=dest)
    sh("git", "add", "-A", cwd=dest)
    sh("git", "-c", "user.email=repair@local", "-c",
       "user.name=repair", "commit", "-qm", "baseline", cwd=dest)
    return dest


def verify_snapshot(dest: Path) -> dict[str, Any]:
    """The snapshot must hold exactly 1 commit and no remotes."""
    commits = sh("git", "rev-list", "--count", "HEAD", cwd=dest)
    remotes = subprocess.run(
        ["git", "remote"], cwd=dest, capture_output=True,
        text=True, check=True).stdout.strip()
    ok = commits.strip() == "1" and remotes == ""
    return {"commits": int(commits.strip()), "remotes": remotes,
            "ok": ok}


def build_repair_prompt(report: str, diagnosis: dict[str, Any],
                        strategy: dict[str, Any]) -> str:
    diag = {k: diagnosis.get(k) for k in
            ("root_cause", "file", "line_range", "suspect_function",
             "mechanism")}
    files = strategy.get("files", [])
    return (
        "You are fixing a visual bug in the source checkout in the current "
        "working directory. Work completely offline: do not use any web, "
        "fetch, or network tool, and do not look up anything outside this "
        "checkout, including upstream trackers or published history. "
        "Only edit files in this checkout.\n"
        f"A user reported: {report}\n"
        "A source-code diagnosis (a hint that may be wrong) is:\n"
        f"{json.dumps(diag, indent=None)}\n"
        "Apply exactly this fix strategy and nothing else:\n"
        f"strategy name: {strategy.get('name')}\n"
        f"strategy files: {json.dumps(files)}\n"
        f"strategy approach: {strategy.get('approach')}\n"
        "Rules:\n"
        "- Keep the change minimal and confined to the strategy files.\n"
        "- Do not add tests, do not touch package.json, lockfiles, configs, "
        "test directories, or build output.\n"
        "- Do not contact the network for any reason.\n"
        "- When done, report the list of files you changed.\n"
    )


def validate_repair_prompt(text: str) -> list[str]:
    """Leak checks mirroring testgen: hashes, SHAs, diffs, test source."""
    errs: list[str] = []
    low = text.lower()
    for term in _BANNED_PROMPT_TEXT:
        if term.lower() in low:
            errs.append(f"contains banned term: {term}")
    # No state-label word check here: repair prompts legitimately say
    # "fix strategy" / "visual bug". State labels (fix_sha/buggy_sha and
    # 40-char SHAs) stay banned above.
    if _HEX40_RE.search(low):
        errs.append("contains a 40-char hex string")
    if _DIFF_MARKER_RE.search(text):
        errs.append("contains a diff/git-log marker")
    for marker in _TEST_SOURCE_MARKERS:
        if marker in text:
            errs.append(f"contains generated test source: {marker}")
    return errs


def write_repair_prompts(case_id: str) -> list[Path]:
    """Write one prompt per fix strategy; all must pass leak validation."""
    meta = read_meta(case_id)
    diagnosis = json.loads(
        (REPO_ROOT / "runs" / case_id / "rootcause.json").read_text()
    )["diagnosis"]
    paths: list[Path] = []
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    for i, strategy in enumerate(diagnosis["fix_strategies"], start=1):
        text = build_repair_prompt(str(meta["report"]), diagnosis, strategy)
        errs = validate_repair_prompt(text)
        if errs:
            raise ValueError(f"{case_id} strategy {i}: " + "; ".join(errs))
        path = PROMPTS_DIR / f"repair_{case_id}_s{i}.txt"
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    return paths


def _scan_inputs_v1(text: str) -> dict[str, Any]:
    """Legacy whole-log scan (kept for the audit trail on s3)."""
    legacy = (
        re.compile(r"github\.com", re.IGNORECASE),
        re.compile(r"web_?fetch", re.IGNORECASE),
        re.compile(r'"tool"\s*:\s*"web', re.IGNORECASE),
        re.compile(r"fetch_url|fetch-url|url_fetch", re.IGNORECASE),
    )
    hits = sorted({f"pattern-{i}" for i, rx in enumerate(legacy)
                   if rx.search(text)})
    return {"contaminated": bool(hits), "patterns": hits,
            "substrings": sorted({s for s in ("github.com",)
                                  if s in text.lower()})}


def scan_contamination(log_path: Path) -> dict[str, Any]:
    """Inputs-only contamination scan (rule v2) of a muse exec JSONL log.

    Collects tool-call inputs — fetch/web tool task kinds plus invoked
    shell commands — and flags upstream references or network commands
    there only. Local file content flowing through tool outputs never
    counts. The legacy whole-log verdict is kept under ``v1``.
    """
    try:
        lines = log_path.read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"contaminated": False, "hits": [], "inputs_scanned": 0,
                "rule": "inputs-only-v2", "v1": {"contaminated": False}}
    inputs: list[str] = []
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        task_kind = (obj.get("payload", {}) or {}).get("event", {}).get(
            "task_kind", "")
        if isinstance(task_kind, str) and task_kind:
            inputs.append(f"tool:{task_kind}")
        inputs.extend(_COMMAND_ECHO_RE.findall(json.dumps(obj)))
    hits = sorted({f"input-{i}" for i, rx in enumerate(_INPUT_UPSTREAM_RES)
                   for text in inputs if rx.search(text)})
    fetch_tools = sorted({t for t in inputs if t.startswith("tool:")
                          and _FETCH_TOOL_RE.search(t)})
    hits.extend(f"fetch-tool:{t}" for t in fetch_tools)
    v1 = _scan_inputs_v1("\n".join(lines))
    return {"contaminated": bool(hits), "hits": hits,
            "inputs_scanned": len(inputs), "rule": "inputs-only-v2",
            "v1": v1}


def model_of_log(log_path: Path) -> str | None:
    """Best-effort extraction of the model muse exec used."""
    try:
        for line in log_path.read_text(
                encoding="utf-8", errors="replace").splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            for key in ("model", "model_id", "modelId"):
                if isinstance(obj, dict) and obj.get(key):
                    return str(obj[key])
            # Recurse one level into common envelopes.
            if isinstance(obj, dict):
                for value in obj.values():
                    if isinstance(value, dict):
                        for key in ("model", "model_id"):
                            if value.get(key):
                                return str(value[key])
    except OSError:
        return None
    return None


def allowed_change(path: str, case_id: str) -> bool:
    """Allowlist guard on the snapshot: sources only, never tooling."""
    if is_kaplay_case(case_id):
        if not path.startswith("src/"):
            return False
    else:
        if "/" in path:
            return False
        if not any(path.endswith(s) for s in _D1_ALLOWED_SUFFIXES):
            return False
    name = path.rsplit("/", 1)[-1]
    if name in _REJECT_NAMES:
        return False
    if "/test" in path or path.startswith("tests/"):
        return False
    if path.startswith("dist/"):
        return False
    return True


def candidate_worktree_from_log(log_path: Path) -> Path | None:
    """Recover the muse session worktree path recorded in a JSONL log."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    matches = re.findall(r"[^\s\"']+\.muse/worktrees/[^\s\"']+", text)
    for match in reversed(matches):
        if Path(match).exists():
            return Path(match)
    return None


def run_candidate(prompt_path: Path, snapshot: Path, workdir: Path,
                  log_path: Path, muse_bin: str = "muse") -> dict[str, Any]:
    """Run one muse exec candidate in its own snapshot worktree."""
    import time
    cmd = ([muse_bin] + MUSE_EXEC_FLAGS
           + ["--workspace", str(snapshot),
              "--worktree-existing", str(workdir),
              "--prompt-file", str(prompt_path)])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as fh:
        proc = subprocess.run(cmd, cwd=workdir, stdout=fh,
                              stderr=subprocess.STDOUT, text=True)
    elapsed_s = round(time.monotonic() - started, 1)
    contamination = scan_contamination(log_path)
    return {"exit": proc.returncode, "model": model_of_log(log_path),
            "elapsed_s": elapsed_s,
            "flags": [rel_str(f) for f in cmd[1:]],
            "contamination": contamination,
            "worktree": rel_str(str(workdir))}


def guard_diff(workdir: Path, case_id: str) -> dict[str, Any]:
    """Diff the snapshot workdir vs HEAD; reject non-allowlist changes.

    Uses ``git status --porcelain`` so untracked files are seen too;
    line counts come from ``--numstat`` for tracked files and from a
    line count for untracked ones.
    """
    porcelain = subprocess.run(
        ["git", "status", "--porcelain", "--"], cwd=workdir,
        capture_output=True, text=True, check=True).stdout.splitlines()
    names = [line[3:] for line in porcelain if len(line) > 3]
    names = sorted({p.strip('"') for p in names})
    # muse session bookkeeping and the deps symlink are not candidate
    # work; ignore them. node_modules never counts toward lines changed.
    ignored = sorted(p for p in names
                     if p.startswith(".muse/") or p == "node_modules"
                     or p.startswith("node_modules/"))
    names = [p for p in names if p not in ignored]
    numstat = subprocess.run(
        ["git", "diff", "--numstat", "HEAD", "--"], cwd=workdir,
        capture_output=True, text=True, check=True).stdout.splitlines()
    added_deleted: dict[str, int] = {}
    for line in numstat:
        parts = line.split("\t")
        if len(parts) == 3:
            try:
                added_deleted[parts[2]] = int(parts[0]) + int(parts[1])
            except ValueError:
                added_deleted[parts[2]] = 0
    for name in names:
        if name not in added_deleted and (workdir / name).is_file():
            try:
                added_deleted[name] = (workdir / name).read_bytes().count(
                    b"\n") + 1
            except OSError:
                added_deleted[name] = 0
    rejected = sorted(p for p in names if not allowed_change(p, case_id))
    allowed = sorted(p for p in names if allowed_change(p, case_id))
    lines = sum(added_deleted.get(p, 0) for p in allowed)
    return {"files": sorted(names), "allowed": allowed,
            "rejected": rejected, "ignored": ignored,
            "lines_changed": lines,
            "ok": bool(allowed) and not rejected}


# --- Gate 2 helpers (harness side; deterministic, no model calls) -----------

def install_snapshot_deps(snapshot: Path,
                            timeout_s: int = 1200) -> dict[str, Any]:
    """Install deps once per case snapshot from its own lockfile.

    Each case pins a different kaplay SHA, so deps install per snapshot,
    never shared from ../kaplay (which has no node_modules). `npm ci`
    when a lockfile is present (exact pins), else `npm install`
    (older SHAs such as k864's predate the lockfile). `node_modules`
    stays gitignored under runs/ and is symlinked, never copied, into each
    candidate worktree. Cases without a package.json (d1) need nothing.
    """
    if not (snapshot / "package.json").is_file():
        return {"skipped": True, "reason": "no package.json in snapshot"}
    if (snapshot / "package-lock.json").is_file():
        cmds = [(["npm", "ci", "--no-audit", "--no-fund"], "npm-ci")]
    else:
        # Older SHAs predate the lockfile; plain install can hit npm's
        # 'edgesOut' peer-dep bug (seen on k950 alpha.23), so fall back
        # to --legacy-peer-deps before giving up.
        cmds = [(["npm", "install", "--no-audit", "--no-fund"],
                 "npm-install"),
                (["npm", "install", "--no-audit", "--no-fund",
                  "--legacy-peer-deps"], "npm-install-legacy-peer-deps")]
    last: dict[str, Any] = {"exit": None, "method": None, "ok": False,
                            "tail": ""}
    for cmd, method in cmds:
        proc = subprocess.run(
            cmd, cwd=snapshot, capture_output=True, text=True,
            timeout=timeout_s)
        ok = proc.returncode == 0 and (snapshot / "node_modules").is_dir()
        last = {"exit": proc.returncode, "method": method, "ok": ok,
                "tail": (proc.stdout + proc.stderr)[-1000:]}
        if ok:
            break
    return last


def link_worktree_deps(workdir: Path, snapshot: Path) -> bool:
    """Symlink the snapshot node_modules into a candidate worktree.

    Candidates may run tsc themselves. The symlink is a guard exception
    (recorded as ignored) and never counts toward lines changed.
    """
    src = snapshot / "node_modules"
    dest = workdir / "node_modules"
    if dest.exists():
        return True
    if not src.is_dir():
        return False
    dest.symlink_to(src, target_is_directory=True)
    return True


def build_kaplay_bundle(workdir: Path, timeout_s: int = 600) -> dict[str, Any]:
    """Run the kaplay build in a snapshot workdir; return dist/kaplay.js."""
    proc = subprocess.run(
        ["npm", "run", "build"], cwd=workdir, capture_output=True, text=True,
        timeout=timeout_s)
    bundle = workdir / "dist" / "kaplay.js"
    return {"exit": proc.returncode, "bundle": str(bundle),
            "exists": bundle.is_file(),
            "tail": (proc.stdout + proc.stderr)[-1000:]}


def served_bundle_path(case_id: str) -> Path:
    return BENCH / case_id / "kaplay.js"


def stage_bundle(case_id: str, built: Path) -> tuple[bytes, str]:
    """Stage a candidate bundle as the served file; return (saved, sha)."""
    target = served_bundle_path(case_id)
    saved = target.read_bytes()
    shutil.copyfile(built, target)
    return saved, hashlib.sha256(target.read_bytes()).hexdigest()


def restore_bundle(case_id: str, saved: bytes) -> str:
    """Restore the pinned buggy bundle and verify its hash."""
    target = served_bundle_path(case_id)
    target.write_bytes(saved)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest != hashlib.sha256(saved).hexdigest():
        raise ValueError(f"{case_id}: kaplay.js restore hash mismatch")
    return digest


def run_gate1_test(case_id: str, url: str,
                   timeout_s: int = 180) -> tuple[int, str]:
    """Run the generated Gate 1 test against the currently served state."""
    import sys
    test_path = REPO_ROOT / "tests_generated" / f"test_{case_id}.py"
    env = dict(__import__("os").environ, VISIOPATCH_CASE_URL=url)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(test_path)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout_s,
        env=env)
    return proc.returncode, (proc.stdout + proc.stderr)[-2000:]


def run_tsc(workdir: Path, timeout_s: int = 300) -> tuple[int, str]:
    """Minimum existing suite for kaplay: npm run check (tsc)."""
    proc = subprocess.run(
        ["npm", "run", "check"], cwd=workdir, capture_output=True, text=True,
        timeout=timeout_s)
    return proc.returncode, (proc.stdout + proc.stderr)[-1500:]


def run_playwright_suite(workdir: Path,
                         timeout_s: int = 300,
                         out_path: Path | None = None,
                         ) -> tuple[int | None, str]:
    """kaplay playwright suite with a 5-minute budget; else record why not.

    The full output goes to ``out_path`` when given (failure sets are
    parsed from the full text, not the tail); the return value keeps only
    the tail for gate2.json.
    """
    try:
        proc = subprocess.run(
            ["npm", "exec", "playwright", "test"], cwd=workdir,
            capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return None, (f"not finished within {timeout_s}s; "
                      "recorded as not-run (budget)")
    full = proc.stdout + proc.stderr
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(full, encoding="utf-8", errors="replace")
    return proc.returncode, full[-1500:]


def playwright_failures(output: str) -> list[str]:
    """Extract the failing-test titles from kaplay playwright output.

    Failure-list lines start with whitespace then ``[chromium] ›``;
    progress lines start with ``[n/m]`` and must not match.
    """
    titles = []
    for line in output.splitlines():
        match = re.match(r"\s+\[chromium\] › (.+)", line)
        if match:
            titles.append(match.group(1).strip())
    return sorted(set(titles))


def suspect_file_overlap(case_id: str, picked_files: list[str]) -> dict:
    """Harness-only scoring: did the picked diff touch the human-fix files?"""
    meta = read_meta(case_id)
    repo = case_git_dir(BENCH / case_id)
    changed = sh("git", "-C", str(repo), "diff", "--name-only",
                 meta["buggy_sha"], meta["fix_sha"]).splitlines()
    hit = bool(set(picked_files) & set(changed))
    return {"hit": hit, "changed_files": sorted(changed)}

