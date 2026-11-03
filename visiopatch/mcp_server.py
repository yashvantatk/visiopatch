"""VisioPatch as a Muse Code MCP server (stdio) — thin wrappers only.

Each tool wraps an existing module and reuses every cache (SAM uploads
by file hash in ``cache/``, rootcause/testgen prompt caches, Gate 1/2
records). Tools return short JSON plus artifact paths, never raw logs.

Model choice (all env-overridable, nothing hardcoded):

- ``VISIOPATCH_ANALYZE_MODEL`` (default ``muse-spark-1.3``): nouns + anomaly.
- ``VISIOPATCH_DIAGNOSE_MODEL`` (default ``muse-wonder-1.0``): diagnose + testgen.
- ``VISIOPATCH_REPAIR_MODEL`` (default ``muse-spark-1.3``) plus
  ``VISIOPATCH_REPAIR_EFFORT`` (default ``medium``): muse exec candidates.
- ``VISIOPATCH_NO_CACHE=1`` forces a fully live run; otherwise results
  are keyed by content (snapshot tree hash + report text + video hash)
  so the same buggy tree reuses Step 5/6 artifacts. Every tool result
  says ``"cache": "hit"`` or ``"cache": "live"``.

Jobs: ``diagnose``, ``generate_visual_test`` and ``repair_and_verify``
always start a job (see ``visiopatch/jobs.py``) and return ``job_id``
immediately. Logs go to stderr only — stdout is the MCP channel.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from visiopatch.bench import BENCH, REPO_ROOT, read_meta, sh
from visiopatch import jobs as JOB
from visiopatch.repair import rel_str, sanitize_record

logger = logging.getLogger("visiopatch.mcp")

ANALYZE_MODEL = os.environ.get("VISIOPATCH_ANALYZE_MODEL", "muse-spark-1.3")
DIAGNOSE_MODEL = os.environ.get("VISIOPATCH_DIAGNOSE_MODEL", "muse-wonder-1.0")
REPAIR_MODEL = os.environ.get("VISIOPATCH_REPAIR_MODEL", "muse-spark-1.3")
REPAIR_EFFORT = os.environ.get("VISIOPATCH_REPAIR_EFFORT", "medium")
NO_CACHE = os.environ.get("VISIOPATCH_NO_CACHE", "") == "1"


def spark_fn_for(model: str = ANALYZE_MODEL):
    """A ``SparkFn`` calling ``model`` instead of the module default."""
    from visiopatch.analyze import (
        get_file_id, make_client as spark_client, usage_dict,
    )

    def _call(mp4: Path, prompt: str):
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
        client = spark_client()
        file_id = get_file_id(client, mp4)
        resp = client.responses.create(
            model=model,
            input=[{"role": "user", "content": [
                {"type": "input_file", "file_id": file_id},
                {"type": "input_text", "text": prompt},
            ]}],
        )
        return (resp.output_text or "").strip(), usage_dict(resp)

    return _call


def wonder_fn_for(model: str = DIAGNOSE_MODEL):
    """A ``WonderFn`` calling ``model`` instead of the module default."""
    from visiopatch.rootcause import make_client as wonder_client
    from visiopatch.rootcause import usage_dict as wonder_usage

    def _call(prompt: str, effort: str):
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
        client = wonder_client()
        resp = client.responses.create(
            model=model, reasoning={"effort": effort}, input=prompt)
        return (resp.output_text or "").strip(), wonder_usage(resp)

    return _call


# --- targets -----------------------------------------------------------------

def parse_target(target: str) -> dict[str, Any]:
    """A bench case id, or a JSON ``{"repo_path": ..., "page"?}`` object.

    The page is a repo-relative path (default ``index.html``); absolute
    paths and ``..`` escapes are rejected. A caller-served ``"url"`` key
    is accepted but ignored: the repo is always served in-process (the
    outer agent cannot background a server in the sandbox).
    """
    import posixpath
    text = target.strip()
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"target is not a case id or JSON: {exc}") from exc
        if not isinstance(obj, dict) or not obj.get("repo_path"):
            raise ValueError("target JSON needs {\"repo_path\": ..., "
                             "\"page\"?}")
        page = obj.get("page", "index.html")
        if not isinstance(page, str) or not page:
            raise ValueError("page must be a non-empty relative path")
        norm = posixpath.normpath(page.replace("\\", "/"))
        if (not norm or norm.startswith("/") or norm == "."
                or norm == ".." or norm.startswith("../")):
            raise ValueError(f"page must stay inside repo_path: {page!r}")
        from visiopatch.capture import validate_actions
        try:
            steps = validate_actions(obj.get("steps"))
        except ValueError as exc:
            raise ValueError(f"steps: {exc}") from exc
        return {"kind": "repo", "repo_path": str(obj["repo_path"]),
                "page": norm, "url": obj.get("url"), "steps": steps}
    if not (BENCH / text / "repro.json").is_file():
        raise ValueError(f"unknown bench case: {text!r}")
    return {"kind": "bench", "case_id": text}


def run_id_for(resolved: dict[str, Any], report: str = "") -> str:
    """Stable run id: the case id, or ``repo-<tree12>`` for arbitrary repos."""
    if resolved["kind"] == "bench":
        return resolved["case_id"]
    tree = snapshot_tree_hash(Path(resolved["repo_path"]))
    return f"repo-{tree[:12]}"


def snapshot_tree_hash(repo_path: Path) -> str:
    """Content hash of a repo: git tree, or a hash of the working tree."""
    if (repo_path / ".git").is_dir():
        try:
            head = sh("git", "rev-parse", "HEAD", cwd=repo_path)
            tree = sh("git", "rev-parse", f"{head}^{{tree}}", cwd=repo_path)
            dirty = subprocess.run(
                ["git", "status", "--porcelain"], cwd=repo_path,
                capture_output=True, text=True, check=True).stdout.strip()
            if not dirty:
                return tree
        except subprocess.CalledProcessError:
            pass
    digest = hashlib.sha256()
    for path in sorted(repo_path.rglob("*")):
        if ".git" in path.parts or not path.is_file():
            continue
        digest.update(str(path.relative_to(repo_path)).encode())
        digest.update(b"\0")
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
    return digest.hexdigest()


def build_snapshot(repo_path: Path, dest: Path) -> Path:
    """Snapshot an arbitrary repo: git archive HEAD, or the working tree
    if dirty — into a single-commit, zero-remote repo, exactly like the
    Step 6 bench snapshots. Candidates never see the original refs."""
    repo_path = repo_path.resolve()
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    if (repo_path / ".git").is_dir():
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo_path,
            capture_output=True, text=True, check=True).stdout.strip()
        if not dirty:
            head = sh("git", "rev-parse", "HEAD", cwd=repo_path)
            archive = subprocess.run(
                ["git", "archive", head], cwd=repo_path,
                capture_output=True, check=True).stdout
            subprocess.run(["tar", "-x", "-C", str(dest)],
                           input=archive, check=True)
        else:
            _copy_tree(repo_path, dest)
    else:
        _copy_tree(repo_path, dest)
    sh("git", "init", "-q", cwd=dest)
    sh("git", "add", "-A", cwd=dest)
    sh("git", "-c", "user.email=repair@local", "-c",
       "user.name=repair", "commit", "-qm", "baseline", cwd=dest)
    verdict = {"commits": sh("git", "rev-list", "--count", "HEAD", cwd=dest),
               "remotes": subprocess.run(
                   ["git", "remote"], cwd=dest, capture_output=True,
                   text=True, check=True).stdout.strip()}
    if verdict["commits"].strip() != "1" or verdict["remotes"] != "":
        raise RuntimeError(f"snapshot isolation failed: {verdict}")
    return dest


def _copy_tree(src: Path, dest: Path) -> None:
    for path in src.iterdir():
        if path.name == ".git":
            continue
        target = dest / path.name
        if path.is_dir() and not path.is_symlink():
            shutil.copytree(path, target)
        elif path.is_file():
            shutil.copy2(path, target)


# --- result cache (content-keyed) --------------------------------------------

def content_key(tool: str, parts: dict[str, Any]) -> str:
    blob = json.dumps({"tool": tool, **parts}, sort_keys=True,
                      default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def mcp_cache_dir(run_id: str) -> Path:
    dest = REPO_ROOT / "runs" / run_id / "mcp_cache"
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def cache_get(run_id: str, key: str) -> dict[str, Any] | None:
    if NO_CACHE:
        return None
    path = mcp_cache_dir(run_id) / f"{key}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def cache_put(run_id: str, key: str, result: dict[str, Any]) -> None:
    if NO_CACHE:
        return
    (mcp_cache_dir(run_id) / f"{key}.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8")


def with_cache(run_id: str, key: str, live_fn) -> dict[str, Any]:
    hit = cache_get(run_id, key)
    if hit is not None:
        return {**hit, "cache": "hit"}
    result = live_fn()
    cache_put(run_id, key, result)
    return {**result, "cache": "live"}


# --- tool implementations (plain functions; MCP wraps them) ------------------

def do_capture_bug(target: str, report: str) -> dict[str, Any]:
    """Capture the buggy clip. Bench id → captures/; repo → serve + record."""
    resolved = parse_target(target)
    run_id = run_id_for(resolved, report)
    if resolved["kind"] == "bench":
        from visiopatch.capture import capture
        key = content_key("capture_bug", {"target": target, "report": report})
        def _live():
            mp4, sidecar = capture(resolved["case_id"], "buggy")
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
            return {"mp4": rel_str(str(mp4)),
                    "meta": rel_str(str(sidecar)),
                    "frames": meta.get("frames"),
                    "sha256": meta.get("sha256")}
        return {"run_id": run_id, **with_cache(run_id, key, _live)}
    return {"run_id": run_id,
            **_capture_repo(resolved, run_id, report)}


def _capture_repo(resolved: dict[str, Any], run_id: str,
                  report: str) -> dict[str, Any]:
    """Serve an arbitrary repo in-process and record the buggy clip.

    Always ``ServedDir(repo_path)`` on a free port, torn down on close:
    nothing depends on a caller-backgrounded server. Only ``repo_path``
    plus the page path are needed.
    """
    repo_path = Path(resolved["repo_path"]).resolve()
    if not repo_path.is_dir():
        raise ValueError(f"repo_path is not a directory: {repo_path}")
    tree = snapshot_tree_hash(repo_path)
    page = resolved.get("page", "index.html")
    steps = resolved.get("steps", [])
    key = content_key("capture_bug", {"tree": tree, "report": report,
                                      "page": page, "steps": steps})
    def _live():
        result: dict[str, Any] = {}
        if resolved.get("url"):
            result["note"] = ("caller url ignored: repo served in-process "
                              "on a free port")
        with ServedDir(repo_path) as served:
            result.update(_record_live(served.url + page, run_id, tree,
                                       steps))
        result["served"] = "in-process"
        if steps:
            result["steps"] = steps
        return result
    return with_cache(run_id, key, _live)


def _record_live(url: str, run_id: str, tree: str,
                 steps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    from visiopatch.capture import (
        _record, build_meta, probe_frames, sha256_of, transcode,
    )
    from visiopatch.capture import FPS, TAIL_S
    duration_s = 5.0
    record_s = duration_s + TAIL_S
    webm, load_offset_s = _record(url, record_s, steps)
    dest = REPO_ROOT / "runs" / run_id / "capture"
    dest.mkdir(parents=True, exist_ok=True)
    mp4 = dest / "buggy.mp4"
    transcode(webm, mp4, record_s)
    digest = sha256_of(mp4)
    frames = probe_frames(mp4, record_s)
    sidecar = dest / "buggy.meta.json"
    sidecar.write_text(json.dumps(build_meta(
        run_id, "buggy", url, duration_s, record_s,
        round(load_offset_s, 3), frames, digest), indent=2) + "\n")
    return {"mp4": rel_str(str(mp4)), "meta": rel_str(str(sidecar)),
            "frames": frames, "sha256": digest}


def _noun_candidates(nouns: list[str] | None, resolved: dict[str, Any],
                     mp4: Path) -> tuple[list[str], dict[str, list[str]]]:
    """Primary nouns + repro-listed fallbacks (bench), else given/Spark."""
    if nouns:
        return list(nouns), {}
    if resolved["kind"] == "bench":
        meta = read_meta(resolved["case_id"])
        return (list(meta.get("noun_phrases", [])),
                dict(meta.get("noun_fallbacks", {})))
    return [], {}


def do_track_objects(video: str, nouns: list[str] | None = None,
                     target: str | None = None) -> dict[str, Any]:
    """SAM track summary. No nouns → Spark proposes them, then repro-listed
    fallbacks are tried in order. A zero-frame result is reported honestly
    (d1 is a known SAM blind spot) — never retried with new nouns."""
    from visiopatch.analyze import propose_nouns, summarize_tracks
    from visiopatch.capture import sha256_of
    from visiopatch.perceive import perceive
    mp4 = (REPO_ROOT / video) if not Path(video).is_absolute() else Path(video)
    if not mp4.is_file():
        raise ValueError(f"video not found: {video!r}")
    resolved = parse_target(target) if target else {"kind": "repo"}
    run_id = run_id_for(resolved) if target else "adhoc"
    digest = sha256_of(mp4)
    primaries, fallbacks = _noun_candidates(nouns, resolved, mp4)
    key = content_key("track_objects", {"video_sha": digest,
                                        "nouns": primaries or nouns})
    def _live():
        import re
        dest_dir = REPO_ROOT / "runs" / run_id / "tracks"
        dest_dir.mkdir(parents=True, exist_ok=True)
        tried: list[str] = []
        if not primaries:
            from visiopatch.analyze import propose_nouns
            primaries.extend(
                propose_nouns(mp4, spark_fn=spark_fn_for())["nouns"])
            proposed = True
        else:
            proposed = False
        rows: list[dict[str, Any]] = []
        docs: list[dict[str, Any]] = []
        for primary in primaries:
            cands = [(primary, "repro" if not proposed else "spark")]
            if not proposed:
                cands += [(f, "fallback")
                          for f in fallbacks.get(primary, [])]
            winner: dict[str, Any] | None = None
            stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", primary).strip("._")
            for cand, source in cands:
                if cand.lower() in tried:
                    continue
                tried.append(cand.lower())
                dest = dest_dir / f"{stem or 'noun'}.json"
                summary = perceive(mp4, cand, dest, primary)
                if summary["frames"] > 0:
                    winner = {"noun": primary, "noun_used": cand,
                              "noun_source": source,
                              "frames": summary["frames"],
                              "tracks": summary["tracks"],
                              "cached": summary["cached"]}
                    try:
                        docs.append(json.loads(dest.read_text(
                            encoding="utf-8")))
                    except (json.JSONDecodeError, OSError):
                        pass
                    break
            rows.append(winner or {
                "noun": primary, "noun_used": None,
                "noun_source": "empty", "frames": 0, "tracks": 0,
                "tried": sorted(tried),
                "note": "EMPTY: no tracks for repro, fallback or Spark "
                        "nouns — reported honestly, never retried with "
                        "new nouns"})
        return {"video": rel_str(str(mp4)), "video_sha256": digest,
                "tracks_summary": summarize_tracks(docs), "rows": rows,
                "spark_proposed": proposed}
    return {"run_id": run_id, **with_cache(run_id, key, _live)}


def do_find_anomaly(video: str, report: str,
                    target: str | None = None) -> dict[str, Any]:
    """Spark reported-mode anomaly result for a clip."""
    from visiopatch.analyze import (
        _anomaly_cache_path, _normalize, _read_cache, _track_bbox_at,
        build_anomaly_prompt, extract_json, summarize_tracks,
        validate_anomaly_doc,
    )
    from visiopatch.capture import FPS, sha256_of
    mp4 = (REPO_ROOT / video) if not Path(video).is_absolute() else Path(video)
    if not mp4.is_file():
        raise ValueError(f"video not found: {video!r}")
    resolved = parse_target(target) if target else {"kind": "repo"}
    run_id = run_id_for(resolved) if target else "adhoc"
    digest = sha256_of(mp4)
    sidecar = mp4.with_suffix(".meta.json")
    try:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        load_offset_s = float(meta.get("load_offset_s", 0.0))
        total_frames = int(meta.get("frames", 0)) or None
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        load_offset_s, total_frames, meta = 0.0, None, {}
    tracks_file = (REPO_ROOT / "runs" / run_id / "tracks")
    docs: list[dict[str, Any]] = []
    if tracks_file.is_dir():
        from visiopatch.perceive import validate_tracks_doc
        for path in sorted(tracks_file.glob("*.json")):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not validate_tracks_doc(doc) and doc.get("track_count", 0) > 0:
                docs.append(doc)
    summary = summarize_tracks(docs)
    if total_frames is None:
        total_frames = max(
            [e["frame"] for d in docs for e in d.get("frames", [])]
            + [int(FPS * 5)])
    key = content_key("find_anomaly", {"video_sha": digest,
                                       "report": report,
                                       "tracks": summary})
    def _live():
        from visiopatch.perceive import CACHE_DIR
        path = _anomaly_cache_path(digest, summary, report)
        cached = _read_cache(path, validate_anomaly_doc)
        if cached is not None and not NO_CACHE:
            return {**cached, "localization": (
                "sam" if cached.get("track_ids") else "spark")}
        spark = spark_fn_for()
        text, usage = spark(mp4, build_anomaly_prompt(summary, report))
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / f"{path.stem}_raw.txt").write_text(
            text + "\n", encoding="utf-8")
        try:
            raw = extract_json(text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(
                f"Spark anomaly reply is not JSON: {exc}") from exc
        result = _normalize(raw, load_offset_s, total_frames)
        errs = validate_anomaly_doc(result)
        if errs:
            raise ValueError("invalid anomaly doc: " + "; ".join(errs))
        if result["anomaly"] and result["track_ids"]:
            sam_bbox = _track_bbox_at(docs, result["track_ids"],
                                      result["frame"])
            if sam_bbox is not None:
                result["bbox"] = sam_bbox
        path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return {**result, "localization": (
            "sam" if result.get("track_ids") and result.get("bbox")
            else "spark")}
    out = {"run_id": run_id, **with_cache(run_id, key, _live)}
    out["video"] = rel_str(str(mp4))
    return out


def _bench_tree_matches(case_id: str) -> bool:
    """Is the bench checkout at the recorded buggy revision?"""
    from visiopatch.bench import case_git_dir
    case = BENCH / case_id
    meta = read_meta(case_id)
    try:
        head = sh("git", "-C", str(case_git_dir(case)),
                  "rev-parse", "HEAD")
    except subprocess.CalledProcessError:
        return False
    return head == meta["buggy_sha"]


def _reported_from_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
    from visiopatch.rootcause import REPORTED_KEYS
    if not isinstance(analysis, dict):
        return {}
    return {k: analysis.get(k) for k in REPORTED_KEYS}


def do_diagnose_impl(target: str, report: str,
                     analysis: dict[str, Any]) -> dict[str, Any]:
    """Wonder rootcause.json: suspect file/function, 3 fix strategies,
    test_spec. Bench reuses the Step 5 artifact when the checkout is at
    the buggy revision; otherwise runs live (prompt caches still apply).
    Always a background job via ``diagnose``."""
    from visiopatch.rootcause import (
        DIAGNOSE_EFFORT, DIAGNOSE_PROMPT_VERSION, PICK_EFFORT,
        PICK_PROMPT_VERSION, _wonder_json, build_diagnose_prompt,
        build_pick_prompt, diagnose as wonder_diagnose, estimate_tokens,
        file_contents, pick_files, source_tree, trim_contents,
        validate_diagnose_doc, validate_pick_doc,
    )
    from visiopatch.rootcause import MAX_DIAGNOSE_TOKENS, SOURCE_SUFFIXES
    resolved = parse_target(target)
    run_id = run_id_for(resolved, report)
    reported = _reported_from_analysis(analysis)
    wonder = wonder_fn_for()
    if resolved["kind"] == "bench":
        case_id = resolved["case_id"]
        rc_path = REPO_ROOT / "runs" / case_id / "rootcause.json"
        if (rc_path.is_file() and not NO_CACHE
                and _bench_tree_matches(case_id)):
            doc = json.loads(rc_path.read_text(encoding="utf-8"))
            short = _diagnose_short(case_id, doc.get("diagnosis", {}),
                                    rc_path, report, reported)
            short["case_id"] = case_id
            return {"run_id": run_id, **short, "cache": "hit",
                    "note": "Step 5 rootcause.json reused (checkout at "
                            "buggy revision)"}
        key = content_key("diagnose", {"case": case_id, "report": report,
                                       "analysis": reported,
                                       "model": DIAGNOSE_MODEL})
        def _live():
            picked = pick_files(case_id, wonder_fn=wonder)
            diagnosis = wonder_diagnose(case_id, picked["files"],
                                        wonder_fn=wonder)
            doc = {"case_id": case_id, "pick": picked,
                   "diagnosis": diagnosis,
                   "cost_usd": picked["cost_usd"] + diagnosis["cost_usd"]}
            rc_path.parent.mkdir(parents=True, exist_ok=True)
            rc_path.write_text(json.dumps(sanitize_record(doc), indent=2)
                               + "\n", encoding="utf-8")
            short = _diagnose_short(case_id, diagnosis, rc_path, report,
                                    reported)
            short["case_id"] = case_id
            return short
        return {"run_id": run_id, **with_cache(run_id, key, _live)}
    repo_path = Path(resolved["repo_path"]).resolve()
    tree = snapshot_tree_hash(repo_path)
    run_id = f"repo-{tree[:12]}"
    key = content_key("diagnose", {"tree": tree, "report": report,
                                   "analysis": reported,
                                   "model": DIAGNOSE_MODEL})
    def _live_repo():
        snap = REPO_ROOT / "runs" / run_id / "src"
        if not (snap / ".git").is_dir():
            build_snapshot(repo_path, snap)
        snap_tree = _snapshot_tree(snap)
        pick_prompt = build_pick_prompt(report, reported, snap_tree)
        pick_payload = {"kind": "pick", "report": report,
                        "reported": reported, "tree": snap_tree,
                        "snapshot": tree}
        picked = _wonder_json(
            pick_prompt, PICK_EFFORT, "pick", PICK_PROMPT_VERSION,
            pick_payload,
            lambda d: validate_pick_doc(d, snap_tree), wonder, None, 0.25)
        picked_paths = [p["path"] for p in picked["files"]]
        raw_contents = _snapshot_contents(snap, picked_paths)
        line_counts = {p: len(t.splitlines())
                       for p, t in raw_contents.items()}
        overhead = estimate_tokens(build_diagnose_prompt(
            report, reported, picked["files"], {}))
        contents = trim_contents(
            raw_contents, max(1000, MAX_DIAGNOSE_TOKENS - overhead))
        diag_prompt = build_diagnose_prompt(report, reported,
                                            picked["files"], contents)
        diag_payload = {"kind": "diagnose", "report": report,
                        "reported": reported, "picked": picked["files"],
                        "contents": contents, "snapshot": tree}
        diagnosis = _wonder_json(
            diag_prompt, DIAGNOSE_EFFORT, "diagnose",
            DIAGNOSE_PROMPT_VERSION, diag_payload,
            lambda d: validate_diagnose_doc(d, picked_paths, line_counts),
            wonder, None, 0.75)
        doc = {"run_id": run_id, "pick": picked, "diagnosis": diagnosis,
               "snapshot_tree": tree,
               "cost_usd": picked["cost_usd"] + diagnosis["cost_usd"]}
        rc_path = REPO_ROOT / "runs" / run_id / "rootcause.json"
        rc_path.parent.mkdir(parents=True, exist_ok=True)
        rc_path.write_text(json.dumps(sanitize_record(doc), indent=2)
                           + "\n", encoding="utf-8")
        return _diagnose_short(run_id, diagnosis, rc_path, report, reported)
    return {"run_id": run_id, **with_cache(run_id, key, _live_repo)}


def _diagnose_short(run_id: str, diagnosis: dict[str, Any], rc_path: Path,
                    report: str, reported: dict[str, Any]) -> dict[str, Any]:
    return {"run_id": run_id,
            "suspect_file": diagnosis.get("file"),
            "suspect_function": diagnosis.get("suspect_function"),
            "fix_strategies": diagnosis.get("fix_strategies"),
            "test_spec": diagnosis.get("test_spec"),
            "root_cause": diagnosis.get("root_cause"),
            "rootcause": rel_str(str(rc_path)),
            "report": report, "reported": reported}


def _full_diagnosis(diagnosis: dict[str, Any]) -> dict[str, Any]:
    """Load the full diagnosis doc via the rootcause artifact when the
    tool result only carries the short form testgen needs full shape."""
    rc = diagnosis.get("rootcause")
    if rc:
        try:
            doc = json.loads((REPO_ROOT / rc).read_text(encoding="utf-8"))
            full = doc.get("diagnosis")
            if isinstance(full, dict) and full.get("fix_strategies"):
                return full
        except (json.JSONDecodeError, OSError):
            pass
    return diagnosis


def _snapshot_tree(snap: Path) -> list[str]:
    from visiopatch.rootcause import SOURCE_SUFFIXES
    try:
        files = sh("git", "-C", str(snap), "ls-files").splitlines()
    except subprocess.CalledProcessError:
        files = []
    return sorted(f for f in files
                  if Path(f).suffix in SOURCE_SUFFIXES)[:400]


def _snapshot_contents(snap: Path, paths: list[str]) -> dict[str, str]:
    contents: dict[str, str] = {}
    for path in paths:
        full = snap / path
        if full.is_file():
            try:
                contents[path] = full.read_text(encoding="utf-8",
                                                errors="replace")
            except OSError:
                continue
    return contents


class ServedDir:
    """Serve a directory over HTTP; terminates on close."""

    def __init__(self, directory: Path, port: int = 0):
        self.directory = directory
        self.port = port or _free_port()
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> "ServedDir":
        from visiopatch.bench import wait_for_port
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(self.port),
             "--directory", str(self.directory)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_for_port(self.port)
        return self

    def __exit__(self, *exc) -> None:
        if self.proc is not None:
            self.proc.terminate()
            self.proc.wait(timeout=15)
            self.proc = None

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}/"


def _free_port() -> int:
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run_pytest_file(test_path: Path, url: str,
                     timeout_s: int = 180) -> tuple[int, str]:
    env = dict(os.environ, VISIOPATCH_CASE_URL=url)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(test_path)],
            cwd=REPO_ROOT, capture_output=True, text=True,
            timeout=timeout_s, env=env)
        return proc.returncode, (proc.stdout + proc.stderr)
    except subprocess.TimeoutExpired:
        return 124, f"pytest timed out after {timeout_s}s"


def _strategy_for(resolved: dict[str, Any]) -> str:
    if resolved["kind"] == "bench":
        from visiopatch.testgen import assertion_strategy
        return assertion_strategy(resolved["case_id"])
    return ("Assert with DOM geometry: use getBoundingClientRect via "
            "page.evaluate on the relevant elements and compare "
            "positions, sizes, or overlap numerically.")


def do_generate_impl(diagnosis: dict[str, Any]) -> dict[str, Any]:
    """Wonder-written Playwright test + Gate 1 result on the current build.

    ``diagnosis`` is the ``diagnose`` tool result (carries run report,
    reported analysis and, for bench, the case id). Gate 1 is two-phase:
    Phase A (page boots, target selector exists) must pass on the current
    build while Phase B (the visual assertion) must fail with
    AssertionError — TimeoutError never passes. Retries up to 3 attempts
    with current-build output only. If Gate 1 fails, the result says so
    honestly — callers must stop.
    """
    from visiopatch.testgen import (
        TESTGEN_PROMPT_VERSION, gate1_verdict, generate_repo_test,
        generate_test, validate_generated_code,
    )
    run_id = str(diagnosis.get("run_id", ""))
    if not run_id:
        raise ValueError("diagnosis needs a run_id (use diagnose output)")
    resolved = (parse_target(diagnosis["case_id"])
                if diagnosis.get("case_id") else {"kind": "repo"})
    if resolved["kind"] == "repo" and run_id.startswith("repo-"):
        pass
    report = str(diagnosis.get("report", ""))
    reported = dict(diagnosis.get("reported", {}))
    full = _full_diagnosis(diagnosis)
    diag = full if full.get("fix_strategies") else diagnosis
    from visiopatch.capture import validate_actions
    try:
        diag_steps = validate_actions(diagnosis.get("steps"))
    except ValueError as exc:
        raise ValueError(f"diagnosis steps: {exc}") from exc
    duration_s = 5.0
    key = content_key("generate_visual_test",
                      {"run": run_id, "diag": diag, "model": DIAGNOSE_MODEL,
                       "prompt_version": TESTGEN_PROMPT_VERSION,
                       "steps": diag_steps})
    def _live():
        wonder = wonder_fn_for()
        test_path = _test_path_for(resolved, run_id)
        raw_dir = JOB.jobs_dir(run_id)

        def _attempts(url: str) -> dict[str, Any]:
            attempts: list[dict[str, Any]] = []
            feedback: str | None = None
            gate_pass = False
            validation_failures = 0

            def _result(note: str, passed: bool) -> dict[str, Any]:
                return {"test_file": rel_str(str(test_path)),
                        "gate": "two-phase (A must pass, B must fail with "
                                "AssertionError)",
                        "gate_pass": passed, "attempts": attempts,
                        "url": url, "note": note}
            for attempt in range(1, 4):
                rec: dict[str, Any] = {"attempt": attempt}
                try:
                    if resolved["kind"] == "bench":
                        gen = generate_test(
                            resolved["case_id"], diag, wonder, None,
                            feedback)
                    else:
                        # Code-shaped call, exactly like Step 5 bench
                        # testgen: the prompt asks for Python code and the
                        # reply is parsed as code, never as JSON.
                        gen = generate_repo_test(
                            report, reported,
                            {**diag, "file": diag.get("suspect_file")}, url,
                            duration_s, _strategy_for(resolved), feedback,
                            wonder_fn=wonder, raw_dir=raw_dir,
                            steps=diag_steps)
                except ValueError as exc:
                    # The gen functions raise on an invalid reply before
                    # returning: count it as a validation failure (one
                    # retry with the reason fed back, then STOP).
                    reason = f"{type(exc).__name__}: {exc}"
                    rec.update({"stage": "validation", "gate_pass": False,
                                "error": reason})
                    attempts.append(rec)
                    validation_failures += 1
                    if validation_failures >= 2:
                        return _result(
                            "STOP: generated test failed validation twice "
                            f"({reason}) — do not repair.", False)
                    feedback = reason
                    continue
                code = gen["code"]
                test_path.write_text(code, encoding="utf-8")
                errs = validate_generated_code(code, duration_s)
                if errs:
                    reason = "; ".join(errs)
                    rec.update({"stage": "validation", "gate_pass": False,
                                "error": reason})
                    attempts.append(rec)
                    validation_failures += 1
                    if validation_failures >= 2:
                        return _result(
                            "STOP: generated test failed validation twice "
                            f"({reason}) — do not repair.", False)
                    feedback = reason
                    continue
                exit_code, out = _run_pytest_file(test_path, url)
                verdict = gate1_verdict(exit_code, out)
                rec.update({"stage": "gate", "exit": exit_code,
                            "output_tail": out[-2000:],
                            "verdict": verdict["reason"]})
                rec["gate_pass"] = verdict["gate_pass"]
                attempts.append(rec)
                if rec["gate_pass"]:
                    gate_pass = True
                    break
                feedback = verdict["reason"] + "\n" + out[-4000:]
            return _result(
                ("Gate 1 fails on the current build as required."
                 if gate_pass else
                 "Gate 1 did not fail as required on the "
                 "unmodified build — STOP, do not repair."), gate_pass)

        if resolved["kind"] == "bench":
            from visiopatch.bench import ServedCase
            case = BENCH / resolved["case_id"]
            with ServedCase(case):
                return _attempts(str(read_meta(resolved["case_id"])["url"]))
        snap = REPO_ROOT / "runs" / run_id / "src"
        if not snap.is_dir():
            raise ValueError(f"{run_id}: no snapshot; run diagnose first")
        with ServedDir(snap) as served:
            return _attempts(served.url)
    out = {"run_id": run_id, **with_cache(run_id, key, _live)}
    if resolved["kind"] == "bench":
        out["case_id"] = resolved["case_id"]
    return out


def _test_path_for(resolved: dict[str, Any], run_id: str) -> Path:
    if resolved["kind"] == "bench":
        dest = REPO_ROOT / "tests_generated" / f"test_{run_id}.py"
    else:
        dest = REPO_ROOT / "runs" / run_id / "test_visual.py"
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


def _child_extra_args() -> list[str]:
    """Model/effort for nested muse exec, plus optional MCP isolation.

    ``VISIOPATCH_CHILD_ARGS`` (shlex-split) appends site-specific flags.
    ``-c mcpServers={}`` isolation is verified at acceptance time; the
    repair prompt already bans network tools and VisioPatch MCP use.
    """
    import shlex
    extra = ["--model", REPAIR_MODEL, "--reasoning-effort", REPAIR_EFFORT]
    site = os.environ.get("VISIOPATCH_CHILD_ARGS", "").strip()
    if site:
        extra += shlex.split(site)
    return extra


def do_repair_impl(target: str, job_record: dict | None = None) -> dict:
    """The Step 6 pipeline: 3 candidates, Gate 2 table, picked patch.

    Chains diagnose + visual test when their artifacts are missing, then
    fans out. Bench targets delegate to the Step 6 runner; arbitrary
    repos use the generic static flow on an isolated snapshot. Never
    applies the patch to the user's repo — returns ``fix.patch`` plus an
    apply command. ``job_record`` receives nested muse child PIDs.
    """
    from visiopatch import repair as R
    resolved = parse_target(target)
    if resolved["kind"] == "bench":
        return _repair_bench(resolved["case_id"], job_record)
    return _repair_repo(resolved, job_record)


def _repair_bench(case_id: str, job_record: dict | None) -> dict[str, Any]:
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import run_repair
    doc = run_repair.run_case(case_id, extra_args=_child_extra_args())
    if job_record is not None:
        JOB.add_pids(job_record, [c.get("pid") for c in doc["candidates"]
                                  if isinstance(c.get("pid"), int)])
    table = [{"strategy": c.get("strategy"), "tag": c.get("tag"),
              "eligible": bool(c.get("eligible")),
              "reason": c.get("reason"),
              "gate1_exit": ((c.get("gate2") or {}).get("gate1_exit")),
              "lines_changed": (c.get("guard") or {}).get("lines_changed")}
             for c in doc["candidates"]]
    picked = doc.get("picked") or {}
    patch = REPO_ROOT / "runs" / case_id / "fix.patch"
    return {"run_id": case_id, "case_id": case_id, "cache": "live",
            "gate2_table": table, "picked": picked,
            "fix_patch": rel_str(str(patch)) if patch.is_file() else None,
            "apply_command": (f"git apply {patch}" if patch.is_file()
                              else None),
            "gate2": rel_str(f"runs/{case_id}/gate2.json"),
            "note": ("Picked patch is Gate-2-verified. It is NOT applied; "
                     "run apply_command in the target repo yourself.")}


def _repair_repo(resolved: dict[str, Any],
                 job_record: dict | None) -> dict[str, Any]:
    from visiopatch import repair as R
    repo_path = Path(resolved["repo_path"]).resolve()
    tree = snapshot_tree_hash(repo_path)
    run_id = f"repo-{tree[:12]}"
    key = content_key("repair_and_verify",
                      {"tree": tree, "repair_model": REPAIR_MODEL,
                       "repair_effort": REPAIR_EFFORT,
                       "no_cache": NO_CACHE})
    if not NO_CACHE:
        hit = cache_get(run_id, key)
        if hit is not None:
            return {**hit, "cache": "hit"}
    repair_dir = REPO_ROOT / "runs" / run_id / "repair"
    repair_dir.mkdir(parents=True, exist_ok=True)
    snap = REPO_ROOT / "runs" / run_id / "src"
    if not (snap / ".git").is_dir():
        build_snapshot(repo_path, snap)
    report = str(resolved.get("report") or _run_report(run_id))
    # Chain: diagnose + visual test artifacts must exist first.
    diag = do_diagnose_impl(
        json.dumps({"repo_path": str(repo_path)}), report,
        _run_analysis(run_id))
    gen = do_generate_impl({**diag, "run_id": run_id})
    if not gen.get("gate_pass"):
        result = {"run_id": run_id, "gate2_table": [],
                  "picked": None, "fix_patch": None,
                  "apply_command": None,
                  "note": "Gate 1 did not fail on the unmodified snapshot "
                          "— STOP, no repair attempted."}
        cache_put(run_id, key, result)
        return {**result, "cache": "live"}
    test_path = REPO_ROOT / gen["test_file"]
    diagnosis = _full_diagnosis(diag)
    strategies = (diagnosis.get("fix_strategies") or [])[:3]
    if len(strategies) < 3:
        raise ValueError("diagnosis has fewer than 3 fix_strategies")
    deps = R.install_snapshot_deps(snap)
    prompts_dir = repair_dir / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    candidates: list[dict[str, Any]] = []
    pids: list[int] = []
    for i, strategy in enumerate(strategies, 1):
        tag = f"s{i}"
        text = R.build_repair_prompt(report, diagnosis, strategy)
        text += ("\n- Do not use any network tool, and do not call any "
                 "VisioPatch MCP tools; work with local files only.\n")
        errs = R.validate_repair_prompt(text)
        if errs:
            raise ValueError(f"strategy {i}: " + "; ".join(errs))
        prompt_path = prompts_dir / f"repair_{tag}.txt"
        prompt_path.write_text(text, encoding="utf-8")
        workdir = R.make_worktree(snap, tag)
        R.link_worktree_deps(workdir, snap)
        log = repair_dir / f"cand_{tag}.jsonl"
        run = R.run_candidate(prompt_path, snap, workdir, log,
                              extra_args=_child_extra_args())
        if isinstance(run.get("pid"), int):
            pids.append(run["pid"])
        rec = {"strategy": strategy.get("name"), "tag": tag,
               "prompt": rel_str(str(prompt_path)),
               "log": rel_str(str(log)), **run}
        if run["exit"] != 0:
            rec.update({"eligible": False,
                        "reason": "muse exec failed or worktree lost"})
            candidates.append(rec)
            continue
        guard = R.guard_diff(workdir, _pseudo_case(run_id, snap))
        rec["guard"] = guard
        if rec.get("contamination", {}).get("contaminated"):
            rec.update({"eligible": False, "reason": "contaminated log"})
        elif not guard["ok"]:
            rec.update({"eligible": False, "reason": (
                f"guard rejected: {guard['rejected']}")})
        else:
            gate = _gate2_repo(run_id, tag, workdir, guard["allowed"],
                               test_path)
            rec["gate2"] = gate
            rec["eligible"] = bool(gate.get("gate_ok"))
            if not gate.get("gate_ok"):
                rec["reason"] = "gate 2 failed"
        candidates.append(rec)
    if job_record is not None and pids:
        JOB.add_pids(job_record, pids)
    eligible = [c for c in candidates if c.get("eligible")]
    picked = min(eligible, key=lambda c: c["guard"]["lines_changed"],
                 default=None)
    patch_path = REPO_ROOT / "runs" / run_id / "fix.patch"
    table = [{"strategy": c.get("strategy"), "tag": c.get("tag"),
              "eligible": bool(c.get("eligible")),
              "reason": c.get("reason"),
              "gate1_exit": (c.get("gate2") or {}).get("gate1_exit"),
              "lines_changed": (c.get("guard") or {}).get("lines_changed")}
             for c in candidates]
    out: dict[str, Any] = {"run_id": run_id, "gate2_table": table,
                           "picked": None, "fix_patch": None,
                           "apply_command": None, "deps": deps}
    if picked is not None:
        picked_wt = Path(picked["worktree"])
        workdir = (picked_wt if picked_wt.is_absolute()
                   else REPO_ROOT / picked_wt)
        diff = subprocess.run(
            ["git", "diff", "HEAD", "--"], cwd=workdir, capture_output=True,
            text=True, check=True).stdout
        patch_path.write_text(diff, encoding="utf-8")
        out.update({
            "picked": {"strategy": picked["strategy"], "tag": picked["tag"],
                       "files": picked["guard"]["allowed"],
                       "lines_changed": picked["guard"]["lines_changed"],
                       "model": picked.get("model")},
            "fix_patch": rel_str(str(patch_path)),
            "apply_command": f"git -C {repo_path} apply {patch_path}",
            "note": ("Picked patch is Gate-2-verified. It is NOT applied; "
                     "run apply_command in the target repo yourself.")})
    else:
        out["note"] = "No candidate passed Gate 2."
    doc = sanitize_record({**out, "candidates": candidates,
                           "snapshot_tree": tree})
    (REPO_ROOT / "runs" / run_id / "gate2.json").write_text(
        json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    result = {**out, "gate2": rel_str(f"runs/{run_id}/gate2.json")}
    cache_put(run_id, key, result)
    return {**result, "cache": "live"}


def _pseudo_case(run_id: str, snap: Path) -> str:
    """Allowlist guard needs a case id; static snapshots use d1 rules."""
    return "d1-modal-zindex"


def _run_report(run_id: str) -> str:
    for name in ("rootcause.json",):
        path = REPO_ROOT / "runs" / run_id / name
        if path.is_file():
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
                rep = doc.get("report") or doc.get("diagnosis", {}).get(
                    "report")
                if rep:
                    return str(rep)
            except (json.JSONDecodeError, OSError):
                pass
    return ""


def _run_analysis(run_id: str) -> dict[str, Any]:
    for name in ("anomaly.json", "tracks.json"):
        path = REPO_ROOT / "runs" / run_id / name
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
    return {}


def _gate2_repo(run_id: str, tag: str, workdir: Path, allowed: list[str],
                test_path: Path) -> dict[str, Any]:
    """Gate 2 for a static snapshot candidate: serve the worktree itself,
    run the generated test, recapture + screenshot + SAM (frames recorded,
    not gated — same v2 rule as Step 6)."""
    from visiopatch.capture import build_meta, probe_frames, sha256_of
    from visiopatch.capture import TAIL_S, _record
    from visiopatch.perceive import perceive
    gate: dict[str, Any] = {"kind": "repo-static", "suite": "none"}
    with ServedDir(workdir) as served:
        exit_code, out = _run_pytest_file(test_path, served.url)
    gate.update({"gate1_exit": exit_code, "gate1_tail": out[-2000:]})
    if exit_code != 0:
        gate["gate_ok"] = False
        return gate
    dest = REPO_ROOT / "runs" / run_id / "repair"
    with ServedDir(workdir) as served:
        webm, load_offset_s = _record(served.url, 5.0 + TAIL_S)
        from visiopatch.capture import transcode
        mp4 = dest / f"cap_{tag}.mp4"
        transcode(webm, mp4, 5.0 + TAIL_S)
        digest = sha256_of(mp4)
        frames = probe_frames(mp4, 5.0 + TAIL_S)
        (dest / f"cap_{tag}.meta.json").write_text(json.dumps(build_meta(
            run_id, "candidate", served.url, 5.0, 5.0 + TAIL_S,
            round(load_offset_s, 3), frames, digest), indent=2) + "\n")
        gate.update({"capture_ok": True, "frames": frames,
                     "mp4": rel_str(str(mp4))})
        try:
            from playwright.sync_api import sync_playwright
            from visiopatch.browser import launch_chromium
            with sync_playwright() as pw:
                browser = launch_chromium(pw)
                try:
                    page = browser.new_page(
                        viewport={"width": 1280, "height": 720})
                    page.goto(served.url, wait_until="load", timeout=30000)
                    page.wait_for_timeout(5000)
                    shot = dest / f"shot_{tag}.png"
                    page.screenshot(path=str(shot))
                    gate["shot"] = rel_str(str(shot))
                finally:
                    browser.close()
        except Exception as exc:
            gate["shot_error"] = f"{type(exc).__name__}: {exc}"
    nouns = _run_nouns(run_id)
    sam_rows = []
    for noun in nouns:
        try:
            summary = perceive(mp4, noun, dest / f"sam_{tag}_{noun}.json")
            sam_rows.append({"noun": noun, "frames": summary["frames"]})
        except Exception as exc:
            sam_rows.append({"noun": noun,
                             "error": f"{type(exc).__name__}: {exc}"})
    gate["sam"] = sam_rows
    gate["visual_pass"] = bool(gate.get("capture_ok") and gate.get("shot"))
    gate["gate_ok"] = bool(gate["visual_pass"])
    return gate


def _run_nouns(run_id: str) -> list[str]:
    tracks = REPO_ROOT / "runs" / run_id / "tracks"
    nouns: list[str] = []
    if tracks.is_dir():
        for path in sorted(tracks.glob("*.json")):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
                noun = doc.get("noun") or doc.get("requested_noun")
            except (json.JSONDecodeError, OSError):
                noun = None
            nouns.append(str(noun) if noun else path.stem.replace("_", " "))
    return nouns[:4]


# --- MCP tool entry points ----------------------------------------------------

def _start_job(tool: str, run_id: str, args: dict[str, Any],
               fn) -> dict[str, Any]:
    record = JOB.create_job(run_id, tool, args)
    JOB.run_in_background(record, fn)
    return {"job_id": record["job_id"], "run_id": run_id,
            "status": "running", "tool_timeout_s": record["tool_timeout_s"],
            "poll": "job_status(job_id), then job_result(job_id)"}


def tool_capture_bug(target: str, report: str) -> dict[str, Any]:
    return do_capture_bug(target, report)


def tool_track_objects(video: str, nouns: list[str] | None = None,
                       target: str | None = None) -> dict[str, Any]:
    return do_track_objects(video, nouns, target)


def tool_find_anomaly(video: str, report: str,
                      target: str | None = None) -> dict[str, Any]:
    return do_find_anomaly(video, report, target)


def tool_diagnose(target: str, report: str,
                  analysis: dict[str, Any]) -> dict[str, Any]:
    resolved = parse_target(target)
    run_id = run_id_for(resolved, report)
    return _start_job("diagnose", run_id,
                      {"target": target, "report": report},
                      lambda: do_diagnose_impl(target, report, analysis))


def tool_generate_visual_test(diagnosis: dict[str, Any]) -> dict[str, Any]:
    run_id = str(diagnosis.get("run_id", "adhoc"))
    return _start_job("generate_visual_test", run_id,
                      {"run_id": run_id,
                       "suspect_file": diagnosis.get("suspect_file")},
                      lambda: do_generate_impl(diagnosis))


def tool_repair_and_verify(target: str) -> dict[str, Any]:
    resolved = parse_target(target)
    run_id = run_id_for(resolved)
    record = JOB.create_job(run_id, "repair_and_verify",
                            {"target": target})
    JOB.run_in_background(
        record, lambda: do_repair_impl(target, job_record=record))
    return {"job_id": record["job_id"], "run_id": run_id,
            "status": "running", "tool_timeout_s": record["tool_timeout_s"],
            "poll": "job_status(job_id), then job_result(job_id)"}


def _clamp_wait(wait_seconds: float) -> float:
    """Clamp an optional poll wait to [0, 60] s; garbage means no wait."""
    try:
        wait = float(wait_seconds)
    except (TypeError, ValueError):
        return 0.0
    if wait != wait:  # NaN
        return 0.0
    return max(0.0, min(60.0, wait))


def _settle(job_id: str, wait_seconds: float) -> dict[str, Any] | None:
    """Poll the job record until it leaves running or the wait expires.

    Returns the latest record, or None when the id is unknown (or the
    file is unreadable). Only unexpected errors propagate — and the
    callers turn even those into ``status: failed`` JSON.
    """
    import time
    deadline = time.monotonic() + _clamp_wait(wait_seconds)
    try:
        record = JOB.load_job(job_id)
    except (KeyError, ValueError):
        return None
    while record["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.2)
        try:
            record = JOB.load_job(job_id)
        except (KeyError, ValueError):
            return None
    return record


def tool_job_status(job_id: str, wait_seconds: float = 0.0) -> dict[str, Any]:
    """Always returns JSON. Unknown ids report ``status: failed`` (never
    ``unknown``); ``wait_seconds`` (max 60) blocks until the job settles
    so the caller polls less."""
    try:
        record = _settle(job_id, wait_seconds)
    except Exception as exc:  # never leak a traceback-shaped error
        return {"job_id": job_id, "status": "failed",
                "error": f"{type(exc).__name__}: {exc}"}
    if record is None:
        return {"job_id": job_id, "status": "failed",
                "error": f"unknown job_id: {job_id!r}"}
    return {"job_id": job_id, "status": record["status"],
            "tool": record["tool"], "run_id": record["run_id"],
            "updated_at": record["updated_at"], "pids": record["pids"],
            "error": record["error"]}


def tool_job_result(job_id: str, wait_seconds: float = 0.0) -> dict[str, Any]:
    """Always returns JSON. Unknown ids report ``status: failed`` with an
    error; ``wait_seconds`` (max 60) blocks until the job settles."""
    try:
        record = _settle(job_id, wait_seconds)
    except Exception as exc:  # never leak a traceback-shaped error
        return {"job_id": job_id, "status": "failed",
                "error": f"{type(exc).__name__}: {exc}"}
    if record is None:
        return {"job_id": job_id, "status": "failed",
                "error": f"unknown job_id: {job_id!r}"}
    if record["status"] == "running":
        return {"job_id": job_id, "status": "running",
                "poll": "job_status(job_id, wait_seconds), then "
                        "job_result(job_id, wait_seconds)"}
    if record["status"] == "failed":
        return {"job_id": job_id, "status": "failed",
                "error": record["error"]}
    return {"job_id": job_id, "status": "done",
            "result": record["result"]}


def build_server():
    """Construct the stdio MCP server (mcp 2.x MCPServer API)."""
    from mcp.server.mcpserver import MCPServer
    server = MCPServer("visiopatch",
                       description="Visual bug capture, tracking, diagnosis "
                                   "and verified repair.")

    @server.tool(description="Capture the buggy clip. target is a bench "
                             "case id or a JSON {repo_path, page, steps} "
                             "object (page defaults to index.html). The "
                             "repo is served in-process; just pass "
                             "repo_path. steps (scroll_to, scroll_by, "
                             "click, wait_ms) reproduces bugs that only "
                             "appear after interaction. "
                             "Returns the mp4 path + meta.")
    def capture_bug(target: str, report: str) -> str:
        return json.dumps(tool_capture_bug(target, report))

    @server.tool(description="SAM track summary for a video. Optional nouns "
                             "list; without nouns Spark proposes them. "
                             "Zero-frame results are reported honestly.")
    def track_objects(video: str, nouns: list[str] | None = None,
                      target: str | None = None) -> str:
        return json.dumps(tool_track_objects(video, nouns, target))

    @server.tool(description="Spark reported-mode anomaly result for a "
                             "video + user report.")
    def find_anomaly(video: str, report: str,
                     target: str | None = None) -> str:
        return json.dumps(tool_find_anomaly(video, report, target))

    @server.tool(description="Wonder root cause (suspect file/function, 3 "
                             "fix strategies, test spec). Starts a job; "
                             "poll job_status/job_result.")
    def diagnose(target: str, report: str,
                 analysis: dict[str, Any]) -> str:
        return json.dumps(tool_diagnose(target, report, analysis))

    @server.tool(description="Write the Playwright visual test and run "
                             "two-phase Gate 1 on the current build "
                             "(Phase A boot/selector must pass, Phase B "
                             "visual assert must fail with AssertionError). "
                             "Pass repro steps through as "
                             "diagnosis.steps. Starts a job.")
    def generate_visual_test(diagnosis: dict[str, Any]) -> str:
        return json.dumps(tool_generate_visual_test(diagnosis))

    @server.tool(description="Step 6 pipeline: 3 candidates, Gate 2 table, "
                             "picked fix.patch + apply command. Does NOT "
                             "apply the patch. Starts a job.")
    def repair_and_verify(target: str) -> str:
        return json.dumps(tool_repair_and_verify(target))

    @server.tool(description="Poll a job started by diagnose, "
                             "generate_visual_test or repair_and_verify. "
                             "Always returns JSON with status failed + "
                             "error on bad ids. wait_seconds (max 60) "
                             "blocks until the job settles.")
    def job_status(job_id: str, wait_seconds: float = 0.0) -> str:
        return json.dumps(tool_job_status(job_id, wait_seconds))

    @server.tool(description="Fetch a finished job's result. Always "
                             "returns JSON with status failed + error on "
                             "bad ids. wait_seconds (max 60) blocks until "
                             "the job settles.")
    def job_result(job_id: str, wait_seconds: float = 0.0) -> str:
        return json.dumps(tool_job_result(job_id, wait_seconds))

    return server


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    server = build_server()
    server.run()


if __name__ == "__main__":
    main()
