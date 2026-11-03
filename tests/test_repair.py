"""Offline tests for visiopatch.repair — no model or network calls."""

import json
import subprocess
from pathlib import Path

from visiopatch import repair
from visiopatch.repair import (
    allowed_change,
    build_repair_prompt,
    model_of_log,
    scan_contamination,
    validate_repair_prompt,
    verify_snapshot,
)

REPO = Path(__file__).resolve().parent.parent

DIAGNOSIS = {
    "root_cause": "velocity is not transferred to the other body",
    "file": "src/ecs/components/physics/body.ts",
    "line_range": [298, 315],
    "suspect_function": "body() - the onPhysicsResolve callback",
    "mechanism": "projection uses only this.vel",
}
STRATEGY = {
    "name": "Momentum impulse in physics-resolve handler",
    "files": ["src/ecs/components/physics/body.ts"],
    "approach": "Compute relative velocity along the normal and apply "
                "opposite impulses to both bodies.",
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        check=True).stdout.strip()


def _mini_repo(tmp_path: Path) -> Path:
    dest = tmp_path / "snap"
    dest.mkdir()
    (dest / "src").mkdir()
    (dest / "src" / "a.ts").write_text("export const x = 1;\n")
    _git(dest, "init", "-q")
    _git(dest, "add", "-A")
    _git(dest, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "baseline")
    return dest


def test_prompt_has_one_strategy_and_no_leak_markers():
    text = build_repair_prompt("squares move oddly", DIAGNOSIS, STRATEGY)
    assert "Momentum impulse" in text
    assert "body.ts" in text
    assert validate_repair_prompt(text) == []


def test_prompt_validation_rejects_leaks():
    bad = build_repair_prompt("r", DIAGNOSIS, STRATEGY)
    assert validate_repair_prompt(bad + "\nfix_sha a9680214") != []
    assert validate_repair_prompt(bad + "\n" + "a" * 40) != []
    assert validate_repair_prompt(
        bad + "\ndef test_reported_symptom(): pass") != []
    assert validate_repair_prompt(bad + "\ndiff --git a/b") != []


def test_snapshot_verify_single_commit_no_remotes(tmp_path):
    dest = _mini_repo(tmp_path)
    verdict = verify_snapshot(dest)
    assert verdict == {"commits": 1, "remotes": "", "ok": True}


def test_snapshot_verify_fails_with_remote(tmp_path):
    dest = _mini_repo(tmp_path)
    _git(dest, "remote", "add", "origin", "https://example.invalid/x.git")
    verdict = verify_snapshot(dest)
    assert verdict["ok"] is False


def test_allowlist_kaplay_src_only():
    assert allowed_change("src/ecs/components/physics/body.ts", "k1174")
    assert not allowed_change("package.json", "k1174")
    assert not allowed_change("package-lock.json", "k1174")
    assert not allowed_change("tests/unit/x.spec.ts", "k1174")
    assert not allowed_change("dist/kaplay.js", "k1174")
    assert not allowed_change("playwright.config.ts", "k1174")


def test_allowlist_d1_app_sources_only(tmp_path):
    assert allowed_change("index.html", "d1-modal-zindex")
    assert not allowed_change("package.json", "d1-modal-zindex")
    assert not allowed_change("sub/index.html", "d1-modal-zindex")


def test_guard_diff_counts_allowed_only(tmp_path):
    dest = _mini_repo(tmp_path)
    (dest / "src" / "a.ts").write_text("export const x = 2;\n")
    verdict = repair.guard_diff(dest, "k1174")
    assert verdict["ok"] is True
    assert verdict["rejected"] == []
    assert verdict["lines_changed"] > 0


def test_guard_diff_rejects_tooling(tmp_path):
    dest = _mini_repo(tmp_path)
    (dest / "package.json").write_text("{}\n")
    verdict = repair.guard_diff(dest, "k1174")
    assert verdict["rejected"] == ["package.json"]
    assert verdict["ok"] is False


def test_guard_diff_ignores_node_modules_symlink(tmp_path):
    dest = _mini_repo(tmp_path)
    (dest / "src" / "a.ts").write_text("export const x = 2;\n")
    (dest / "node_modules").symlink_to(tmp_path, target_is_directory=True)
    verdict = repair.guard_diff(dest, "k1174")
    assert any(p.startswith("node_modules") for p in verdict["ignored"])
    assert verdict["rejected"] == []
    assert verdict["ok"] is True


def _log_line(payload_type: str, event: dict) -> str:
    return json.dumps({"payload_type": payload_type,
                       "payload": {"event": event}}) + "\n"


def test_contamination_v2_flags_curl_command_input(tmp_path):
    inner = json.dumps({"chunk_id": "exec-1-1",
                        "command": "curl https://example.invalid/x"})
    log = tmp_path / "dirty.jsonl"
    log.write_text(_log_line("task.lifecycle.output", {"chunk": inner}))
    verdict = scan_contamination(log)
    assert verdict["contaminated"] is True
    assert verdict["rule"] == "inputs-only-v2"


def test_contamination_v2_flags_fetch_tool_kind(tmp_path):
    log = tmp_path / "fetch.jsonl"
    log.write_text(_log_line("task.lifecycle.proposed",
                             {"task_kind": "tool.web_fetch"}))
    assert scan_contamination(log)["contaminated"] is True


def test_contamination_v2_ignores_local_file_content(tmp_path):
    # Rule v1 flagged this (github.com inside a locally read package.json);
    # v2 scans tool-call inputs only, so the same content is clean.
    log = tmp_path / "clean.jsonl"
    log.write_text(_log_line("tool.result", {"text":
        'Read text file `package.json`: {"bugs": '
        '{"url": "https://github.com/kaplayjs/kaplay/issues"}}'}))
    verdict = scan_contamination(log)
    assert verdict["contaminated"] is False
    assert verdict["v1"]["contaminated"] is True


def test_contamination_v2_ignores_local_git_log(tmp_path):
    inner = json.dumps({"chunk_id": "exec-1-1",
                        "command": "git log --oneline -5"})
    log = tmp_path / "git.jsonl"
    log.write_text(_log_line("task.lifecycle.output", {"chunk": inner}))
    assert scan_contamination(log)["contaminated"] is False


def test_model_of_log_missing_is_none(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("{not json}\n")
    assert model_of_log(empty) is None


def test_install_snapshot_deps_skips_without_package_json(tmp_path):
    from visiopatch.repair import install_snapshot_deps
    dest = tmp_path / "snap"
    dest.mkdir()
    verdict = install_snapshot_deps(dest)
    assert verdict == {"skipped": True,
                       "reason": "no package.json in snapshot"}


def test_playwright_failures_only_failure_list():
    from visiopatch.repair import playwright_failures
    out = ("[69/73] [chromium] › tests/unit/a.spec.ts:1:1 › passes\n"
           "  1 failed\n"
           "    [chromium] › tests/integration/b.spec.ts:7:1 › blows up \n"
           "  72 passed\n")
    assert playwright_failures(out) == ["tests/integration/b.spec.ts:7:1 › blows up"]


def test_rel_str_makes_repo_paths_relative():
    from visiopatch.bench import REPO_ROOT
    from visiopatch.repair import rel_str
    assert rel_str(str(REPO_ROOT / "runs" / "x" / "gate2.json")) == "runs/x/gate2.json"
    assert rel_str("/usr/lib/python3.10/os.py") == "/usr/lib/python3.10/os.py"
    assert rel_str("already/relative") == "already/relative"


def test_sanitize_record_strips_repo_root_everywhere():
    from visiopatch.bench import REPO_ROOT
    from visiopatch.repair import sanitize_record
    doc = {"worktree": str(REPO_ROOT / "runs" / "x"),
           "tail": f"from {REPO_ROOT}/runs/x/src/a.ts",
           "n": 3, "items": [str(REPO_ROOT / "p.txt")]}
    out = sanitize_record(doc)
    assert out == {"worktree": "runs/x",
                   "tail": "from runs/x/src/a.ts",
                   "n": 3, "items": ["p.txt"]}
    assert str(REPO_ROOT) not in json.dumps(out)


def test_committed_run_jsons_hold_no_absolute_repo_paths():
    from visiopatch.bench import REPO_ROOT
    for path in sorted((REPO_ROOT / "runs").glob("*/gate1.json")) + \
            sorted((REPO_ROOT / "runs").glob("*/gate2.json")) + \
            sorted((REPO_ROOT / "runs").glob("*/rootcause.json")):
        text = path.read_text(encoding="utf-8")
        assert str(REPO_ROOT) + "/" not in text, path


def test_spawn_env_drops_pythonpath(monkeypatch):
    from visiopatch.bench import spawn_env
    monkeypatch.setenv("PYTHONPATH", "/ros/plugin:/x")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = spawn_env()
    assert "PYTHONPATH" not in env
    assert env["PATH"] == "/usr/bin"
    assert spawn_env({"A": "b"})["A"] == "b"


def test_run_candidate_records_real_pid_and_clean_env(monkeypatch, tmp_path):
    """Popen (not run) exposes .pid; the child env has no PYTHONPATH."""
    import subprocess as sp_mod
    seen: dict = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            seen["cmd"] = cmd
            seen["env"] = kwargs.get("env")
            self.pid = 4242
            self.returncode = 0

        def wait(self):
            return 0

    snap = _mini_repo(tmp_path)
    monkeypatch.setattr(sp_mod, "Popen", FakePopen)
    monkeypatch.setenv("PYTHONPATH", "/ros/plugin")
    prompt = tmp_path / "p.txt"
    prompt.write_text("prompt\n", encoding="utf-8")
    log = tmp_path / "cand.jsonl"
    out = repair.run_candidate(prompt, snap, snap, log, muse_bin="muse")
    assert out["pid"] == 4242
    assert isinstance(out["pid"], int)
    assert "PYTHONPATH" not in seen["env"]
    assert seen["cmd"][0] == "muse"


def test_make_worktree_retry_after_crash(tmp_path):
    """A crashed run leaves work_s1 + cand-s1 behind; the next fan-out
    must still succeed (this exact retry failed in attempt 5)."""
    snap = _mini_repo(tmp_path)
    wt1 = repair.make_worktree(snap, "s1")
    assert wt1.is_dir()
    # crash leftover with the worktree entry gone but the branch kept,
    # plus the plain double-create path:
    subprocess.run(["git", "worktree", "remove", "--force", str(wt1)],
                   cwd=snap, check=True)
    wt2 = repair.make_worktree(snap, "s1")
    assert wt2.is_dir()
    wt3 = repair.make_worktree(snap, "s1")
    assert wt3.is_dir()
    repair.prune_worktrees(snap, ["s1"])
    branches = subprocess.run(
        ["git", "branch", "--list", "cand-s1"], cwd=snap,
        capture_output=True, text=True, check=True).stdout
    assert branches.strip() == ""
    listed = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=snap,
        capture_output=True, text=True, check=True).stdout
    assert "work_s1" not in listed
