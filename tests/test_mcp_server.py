"""Offline tests for visiopatch.mcp_server — no model or network calls.

Every model boundary is mocked: spark/wonder callables, muse exec
candidates, pytest runs. Nothing here touches the network.
"""

import json
import subprocess
from pathlib import Path

import pytest

from visiopatch import mcp_server as MCP
from visiopatch.bench import REPO_ROOT


# --- targets ---------------------------------------------------------------

def test_parse_target_bench_and_repo():
    assert MCP.parse_target("d1-modal-zindex") == {
        "kind": "bench", "case_id": "d1-modal-zindex"}
    resolved = MCP.parse_target('{"repo_path": "/tmp/x"}')
    assert resolved == {"kind": "repo", "repo_path": "/tmp/x",
                        "page": "index.html", "url": None}
    with pytest.raises(ValueError):
        MCP.parse_target("no-such-case")
    with pytest.raises(ValueError):
        MCP.parse_target('{"url": "http://x/"}')
    with pytest.raises(ValueError):
        MCP.parse_target("{not json")


def test_run_id_for():
    assert MCP.run_id_for({"kind": "bench",
                           "case_id": "d1-modal-zindex"}) == "d1-modal-zindex"


def test_content_key_stable():
    assert (MCP.content_key("t", {"a": 1})
            == MCP.content_key("t", {"a": 1}))
    assert (MCP.content_key("t", {"a": 1})
            != MCP.content_key("t", {"a": 2}))


# --- snapshots ---------------------------------------------------------------

def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "init"],
                   cwd=path, check=True)
    (path / "index.html").write_text("<html>hi</html>\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "page"], cwd=path, check=True)
    return path


def test_snapshot_tree_hash_clean_matches_git(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True,
                          check=True).stdout.strip()
    tree = subprocess.run(["git", "rev-parse", f"{head}^{{tree}}"],
                          cwd=repo, capture_output=True, text=True,
                          check=True).stdout.strip()
    assert MCP.snapshot_tree_hash(repo) == tree


def test_snapshot_tree_hash_dirty_differs(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    clean = MCP.snapshot_tree_hash(repo)
    (repo / "index.html").write_text("<html>dirty</html>\n",
                                     encoding="utf-8")
    assert MCP.snapshot_tree_hash(repo) != clean


def test_build_snapshot_isolation(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    dest = MCP.build_snapshot(repo, tmp_path / "snap")
    commits = subprocess.run(["git", "rev-list", "--count", "HEAD"],
                             cwd=dest, capture_output=True, text=True,
                             check=True).stdout.strip()
    remotes = subprocess.run(["git", "remote"], cwd=dest, capture_output=True,
                             text=True, check=True).stdout.strip()
    assert commits == "1" and remotes == ""
    assert (dest / "index.html").is_file()


def test_build_snapshot_dirty_tree_included(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / "uncommitted.html").write_text("<html>new</html>\n",
                                           encoding="utf-8")
    dest = MCP.build_snapshot(repo, tmp_path / "snap")
    assert (dest / "uncommitted.html").is_file()


def test_snapshot_tree_and_contents(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    snap = MCP.build_snapshot(repo, tmp_path / "snap")
    assert MCP._snapshot_tree(snap) == ["index.html"]
    assert MCP._snapshot_contents(snap, ["index.html"]) == {
        "index.html": "<html>hi</html>\n"}


# --- cache -------------------------------------------------------------------

def test_with_cache_live_then_hit(monkeypatch, tmp_path):
    import uuid
    monkeypatch.setattr(MCP, "NO_CACHE", False)
    run_id = f"testprobe-{uuid.uuid4().hex[:8]}"
    calls = []
    key = MCP.content_key("probe", {"v": 1})
    first = MCP.with_cache(run_id, key,
                           lambda: (calls.append(1), {"n": 1})[1])
    assert first == {"n": 1, "cache": "live"}
    second = MCP.with_cache(run_id, key,
                            lambda: (calls.append(1), {"n": 2})[1])
    assert second == {"n": 1, "cache": "hit"}
    assert calls == [1]


# --- diagnose reuse (no model) -----------------------------------------------

def test_diagnose_bench_reuses_step5_artifact(monkeypatch):
    monkeypatch.setattr(MCP, "NO_CACHE", False)
    monkeypatch.setattr(MCP, "_bench_tree_matches", lambda case: True)
    out = MCP.do_diagnose_impl(
        "d1-modal-zindex", "the popup's top part is hidden when it opens",
        {"anomaly": True})
    assert out["cache"] == "hit"
    assert out["case_id"] == "d1-modal-zindex"
    assert isinstance(out["fix_strategies"], list)
    assert len(out["fix_strategies"]) == 3
    assert out["test_spec"]
    assert out["rootcause"].endswith("rootcause.json")


def test_full_diagnosis_loads_artifact():
    short = {"run_id": "d1-modal-zindex",
             "rootcause": "runs/d1-modal-zindex/rootcause.json"}
    full = MCP._full_diagnosis(short)
    assert full.get("fix_strategies") and len(full["fix_strategies"]) == 3


# --- track + anomaly with mocked models --------------------------------------

def test_track_objects_mocked_perceive(monkeypatch, tmp_path):
    from visiopatch import perceive as PERCEIVE
    monkeypatch.setattr(MCP, "NO_CACHE", True)
    monkeypatch.setattr(PERCEIVE, "perceive",
                        lambda mp4, noun, dest, req=None: {
                            "frames": 12, "tracks": 1,
                            "cost_usd": 0.0, "cached": True,
                            "noun": noun})
    mp4 = tmp_path / "clip.mp4"
    mp4.write_bytes(b"\x00" * 64)
    out = MCP.do_track_objects(str(mp4), nouns=["widget"],
                               target="d1-modal-zindex")
    assert out["cache"] == "live"
    row = out["rows"][0]
    assert row["noun_used"] == "widget" and row["frames"] == 12


def test_track_objects_empty_reported_honestly(monkeypatch, tmp_path):
    from visiopatch import perceive as PERCEIVE
    monkeypatch.setattr(MCP, "NO_CACHE", True)
    monkeypatch.setattr(PERCEIVE, "perceive",
                        lambda mp4, noun, dest, req=None: {
                            "frames": 0, "tracks": 0,
                            "cost_usd": 0.0, "cached": False,
                            "noun": noun})
    mp4 = tmp_path / "clip.mp4"
    mp4.write_bytes(b"\x00" * 64)
    out = MCP.do_track_objects(str(mp4), nouns=["widget"],
                               target="d1-modal-zindex")
    assert out["rows"][0]["noun_source"] == "empty"
    assert "EMPTY" in out["rows"][0]["note"]


def test_find_anomaly_mocked_spark(monkeypatch, tmp_path):
    monkeypatch.setattr(MCP, "NO_CACHE", True)
    replies = {"n": 0}

    def _fake_spark(mp4, prompt):
        replies["n"] += 1
        return (json.dumps({"anomaly": True, "frame": 10,
                            "time_s": 0.5, "track_ids": [],
                            "bbox": [1, 2, 3, 4],
                            "observed": "o", "expected": "e",
                            "confidence": 0.9}), None)

    monkeypatch.setattr(MCP, "spark_fn_for", lambda model=None: _fake_spark)
    mp4 = tmp_path / "clip.mp4"
    mp4.write_bytes(b"\x00" * 64)
    out = MCP.do_find_anomaly(str(mp4), "something looks off")
    assert out["anomaly"] is True
    assert out["localization"] == "spark"
    assert replies["n"] == 1


# --- job wrappers ------------------------------------------------------------

def test_diagnose_tool_returns_job_id_immediately(monkeypatch):
    monkeypatch.setattr(
        MCP, "do_diagnose_impl",
        lambda target, report, analysis: {"suspect_file": "x.html"})
    out = MCP.tool_diagnose("d1-modal-zindex", "rep", {})
    assert out["status"] == "running" and "job_id" in out
    job_id = out["job_id"]
    done = MCP.JOB.load_job(job_id)
    assert done["status"] in ("running", "done")
    import time
    deadline = time.time() + 10
    while MCP.tool_job_status(job_id)["status"] == "running":
        assert time.time() < deadline
        time.sleep(0.05)
    result = MCP.tool_job_result(job_id)
    assert result["status"] == "done"
    assert result["result"] == {"suspect_file": "x.html"}


def test_child_extra_args_model_and_effort(monkeypatch):
    monkeypatch.delenv("VISIOPATCH_CHILD_ARGS", raising=False)
    args = MCP._child_extra_args()
    assert args[:4] == ["--model", MCP.REPAIR_MODEL,
                        "--reasoning-effort", MCP.REPAIR_EFFORT]


# --- capture_bug serves repo_path in-process ---------------------------------

def test_parse_target_repo_page_defaults_and_rejects_traversal():
    resolved = MCP.parse_target('{"repo_path": "/tmp/x"}')
    assert resolved["page"] == "index.html"
    resolved = MCP.parse_target(
        '{"repo_path": "/tmp/x", "page": "app/index.html"}')
    assert resolved["page"] == "app/index.html"
    for bad in ("../secret", "/abs/path", "a/../../b", ""):
        with pytest.raises(ValueError):
            MCP.parse_target(
                '{"repo_path": "/tmp/x", "page": %s}' % json.dumps(bad))


def test_capture_repo_serves_itself_in_process(monkeypatch, tmp_path):
    """No caller URL needed: capture_bug serves repo_path on a free port
    inside the MCP process (the outer agent cannot background a server
    in the sandbox) and tears it down after. Fails until _capture_repo
    stops depending on a caller-served URL."""
    from visiopatch import capture as CAP
    for var in ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(MCP, "NO_CACHE", True)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "index.html").write_text("<html>buggy page</html>\n",
                                     encoding="utf-8")
    seen: dict = {}

    def _fake_record(url: str, record_s: float):
        import urllib.request
        seen["url"] = url
        body = urllib.request.urlopen(url, timeout=10).read()
        assert b"buggy page" in body
        webm = tmp_path / "clip.webm"
        webm.write_bytes(b"\x00" * 16)
        return webm, 0.1

    monkeypatch.setattr(CAP, "_record", _fake_record)
    monkeypatch.setattr(CAP, "transcode",
                        lambda webm, mp4, rs: mp4.write_bytes(b"\x00" * 8))
    monkeypatch.setattr(CAP, "probe_frames", lambda mp4, rs: 82)
    monkeypatch.setattr(CAP, "sha256_of", lambda path: "digest")
    out = MCP.do_capture_bug(json.dumps({"repo_path": str(repo)}),
                             "the popup covers the button")
    assert out["frames"] == 82
    assert seen["url"].startswith("http://localhost:")
    assert seen["url"].endswith("/index.html")
    # Torn down: the port is dead after the tool returns.
    import urllib.request, urllib.error
    with pytest.raises(Exception):
        urllib.request.urlopen(seen["url"], timeout=5).read()


def test_capture_repo_ignores_caller_url(monkeypatch, tmp_path):
    """Even when the caller passes a (dead) localhost URL, the repo is
    served in-process — the Step 7 "errored 3 of 4" path."""
    from visiopatch import capture as CAP
    monkeypatch.setattr(MCP, "NO_CACHE", True)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "index.html").write_text("<html>hi</html>\n", encoding="utf-8")
    seen: dict = {}

    def _fake_record(url: str, record_s: float):
        seen["url"] = url
        webm = tmp_path / "clip.webm"
        webm.write_bytes(b"\x00" * 16)
        return webm, 0.1

    monkeypatch.setattr(CAP, "_record", _fake_record)
    monkeypatch.setattr(CAP, "transcode",
                        lambda webm, mp4, rs: mp4.write_bytes(b"\x00" * 8))
    monkeypatch.setattr(CAP, "probe_frames", lambda mp4, rs: 1)
    monkeypatch.setattr(CAP, "sha256_of", lambda path: "d")
    MCP.do_capture_bug(json.dumps({"repo_path": str(repo),
                                   "url": "http://localhost:9/"}), "rep")
    assert ":9/" not in seen["url"]


# --- model key loading is cwd-independent -------------------------------------

def test_wonder_fn_loads_dotenv_by_absolute_path(monkeypatch, tmp_path):
    """Step 7 suspect: key loading with Muse's cwd and env. The closures
    load REPO_ROOT/.env by absolute path, so a different cwd and a bare
    env still resolve the key the same way (offline: client is faked)."""
    import dotenv
    from visiopatch import rootcause as RC
    from visiopatch.bench import REPO_ROOT
    seen: dict = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.setattr(
        dotenv, "load_dotenv",
        lambda path, *a, **k: seen.setdefault("path", str(path)))
    monkeypatch.setattr(RC, "make_client", lambda *a, **k: _NoClient())
    fn = MCP.wonder_fn_for()
    with pytest.raises(Exception):
        fn("prompt", "high")
    assert seen["path"] == str(REPO_ROOT / ".env")
    assert Path(seen["path"]).is_absolute()


class _NoClient:
    def __getattr__(self, name):
        raise AssertionError("client must not be used in this test")


# --- generate stops after a second validation failure -----------------------

def test_generate_stops_after_second_validation_failure(monkeypatch):
    """Validation gets exactly one retry with the reason fed back, then
    the tool stops (acceptance attempt 2: don't burn model calls)."""
    import shutil
    from visiopatch import testgen as TG
    monkeypatch.setattr(MCP, "NO_CACHE", True)
    run_id = "repo-testval"
    snap = REPO_ROOT / "runs" / run_id / "src"
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "index.html").write_text("<html></html>\n", encoding="utf-8")
    try:
        calls: list = []

        def _fake_gen(*args, **kwargs):
            feedback = kwargs.get("feedback",
                                  args[6] if len(args) > 6 else None)
            calls.append(feedback)
            return {"code": "assert True\n"}

        monkeypatch.setattr(TG, "generate_repo_test", _fake_gen)
        monkeypatch.setattr(MCP, "wonder_fn_for", lambda *a, **k: None)
        out = MCP.do_generate_impl(
            {"run_id": run_id, "report": "r", "reported": {}})
    finally:
        shutil.rmtree(REPO_ROOT / "runs" / run_id, ignore_errors=True)
    assert out["gate_pass"] is False
    assert len(out["attempts"]) == 2
    assert all(a["stage"] == "validation" for a in out["attempts"])
    assert "STOP" in out["note"]
    assert calls[0] is None and calls[1]


# --- job_status / job_result: always JSON, with wait --------------------------

def test_job_status_unknown_is_failed_json():
    out = MCP.tool_job_status("d1-modal-zindex.000000000000")
    assert out["status"] == "failed"
    assert out["error"]


def test_job_result_unknown_is_failed_json():
    out = MCP.tool_job_result("d1-modal-zindex.000000000000")
    assert out == {"job_id": "d1-modal-zindex.000000000000",
                   "status": "failed", "error": out["error"]}
    assert out["error"]


def test_job_status_malformed_id_is_failed_json():
    out = MCP.tool_job_status("nohex")
    assert out["status"] == "failed" and out["error"]
    out = MCP.tool_job_result("nohex")
    assert out["status"] == "failed" and out["error"]


def test_job_wait_seconds_settles_without_polling():
    import time
    rec = MCP.JOB.create_job("d1-modal-zindex", "diagnose", {})
    def _slow():
        time.sleep(0.6)
        return {"answer": 1}
    MCP.JOB.run_in_background(rec, _slow)
    out = MCP.tool_job_result(rec["job_id"], wait_seconds=5)
    assert out["status"] == "done"
    assert out["result"] == {"answer": 1}
    status = MCP.tool_job_status(rec["job_id"], wait_seconds=5)
    assert status["status"] == "done"


def test_job_wait_seconds_clamped():
    assert MCP._clamp_wait(999) == 60
    assert MCP._clamp_wait(-3) == 0
    assert MCP._clamp_wait(2.5) == 2.5


# --- repo repair fan-out with mocked candidate -------------------------------

def _fresh_run_id(repo: Path) -> str:
    """Remove any stale run dir so worktree branches never collide."""
    import shutil
    run_id = f"repo-{MCP.snapshot_tree_hash(repo)[:12]}"
    shutil.rmtree(REPO_ROOT / "runs" / run_id, ignore_errors=True)
    return run_id


def test_repair_repo_gate1_stop(monkeypatch, tmp_path):
    monkeypatch.setattr(MCP, "NO_CACHE", True)
    repo = _git_repo(tmp_path / "repo")
    run_id = _fresh_run_id(repo)
    monkeypatch.setattr(
        MCP, "do_diagnose_impl",
        lambda target, report, analysis: {"run_id": run_id,
                                          "report": "r", "reported": {}})
    monkeypatch.setattr(
        MCP, "do_generate_impl",
        lambda diagnosis: {"run_id": run_id, "gate_pass": False})
    out = MCP.do_repair_impl(json.dumps({"repo_path": str(repo)}))
    assert out["picked"] is None and out["fix_patch"] is None
    assert "STOP" in out["note"]


def test_repair_repo_picks_mocked_candidate(monkeypatch, tmp_path):
    from visiopatch import repair as R
    monkeypatch.setattr(MCP, "NO_CACHE", True)
    repo = _git_repo(tmp_path / "repo")
    run_id = _fresh_run_id(repo)

    def _fake_diag(target, report, analysis):
        rc = REPO_ROOT / "runs" / run_id / "rootcause.json"
        rc.parent.mkdir(parents=True, exist_ok=True)
        rc.write_text(json.dumps({"diagnosis": {
            "file": "index.html", "root_cause": "c",
            "fix_strategies": [
                {"name": f"s{i}", "files": ["index.html"],
                 "approach": "edit it"} for i in (1, 2, 3)],
            "test_spec": "t"}}), encoding="utf-8")
        return {"run_id": run_id, "report": "r", "reported": {},
                "rootcause": f"runs/{run_id}/rootcause.json"}

    def _fake_gen(diagnosis):
        test_path = REPO_ROOT / "runs" / run_id / "test_visual.py"
        test_path.write_text("x = 1\n", encoding="utf-8")
        return {"run_id": run_id, "gate_pass": True,
                "test_file": f"runs/{run_id}/test_visual.py"}

    def _fake_candidate(prompt_path, snapshot, workdir, log,
                        muse_bin="muse", extra_args=None):
        assert extra_args and extra_args[0] == "--model"
        (workdir / "index.html").write_text(
            "<html>fixed</html>\n", encoding="utf-8")
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("{}\n", encoding="utf-8")
        return {"exit": 0, "model": "mock", "elapsed_s": 0.1,
                "pid": 4242, "flags": [],
                "worktree": R.rel_str(str(workdir)),
                "contamination": {"contaminated": False}}

    scm_seen: list[str] = []
    monkeypatch.setattr(MCP, "do_diagnose_impl", _fake_diag)
    monkeypatch.setattr(MCP, "do_generate_impl", _fake_gen)
    monkeypatch.setattr(R, "run_candidate", _fake_candidate)
    monkeypatch.setattr(
        MCP, "_gate2_repo",
        lambda run_id, tag, workdir, allowed, test_path: (
            scm_seen.append(tag), {"gate_ok": True, "gate1_exit": 0}
        )[1])
    out = MCP.do_repair_impl(json.dumps({"repo_path": str(repo)}))
    assert [c["tag"] for c in out["gate2_table"]] == ["s1", "s2", "s3"]
    assert out["picked"]["lines_changed"] > 0
    patch = REPO_ROOT / out["fix_patch"]
    assert patch.is_file() and "fixed" in patch.read_text()
    assert out["apply_command"].startswith(f"git -C {repo} apply ")
    assert out["cache"] == "live"
