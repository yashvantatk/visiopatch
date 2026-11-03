#!/usr/bin/env python3
"""Step 6: Muse Code worktree fan-out repair + Gate 2 verification.

Usage: .venv/bin/python scripts/run_repair.py --only k1174

Per case: build an offline snapshot at buggy_sha, write one repair prompt
per fix strategy, run one `muse exec` candidate per strategy in muse's own
worktree mode, then Gate 2 each candidate (generated Gate 1 test, existing
suite or 'none', visual rebuild + recapture + SAM + screenshot). The smallest
passing diff is picked; failures, contaminated runs, and guard rejections are
never picked. Bench state is restored to buggy_sha and verified afterwards.

Writes runs/<id>/gate2.json and runs/<id>/fix.patch. Raw muse JSONL logs
stay in gitignored runs/<id>/repair/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from visiopatch.bench import (  # noqa: E402
    BENCH, REPO_ROOT as RR, ServedCase, case_ids, read_meta, restore_buggy,
    sh, switch_to_fix,
)
from visiopatch import repair as R  # noqa: E402

PLAYWRIGHT_BUDGET_S = 300
SUBSCRIPTION_NOTE = "muse exec runs on the subscription; no API cost billed"


def _repair_dir(case_id: str) -> Path:
    dest = REPO_ROOT / "runs" / case_id / "repair"
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def _visual_confirm(case_id: str, tag: str, workdir: Path | None = None,
                    ) -> dict:
    """Recapture the served state, rerun SAM per noun, save one screenshot."""
    from visiopatch.browser import launch_chromium
    from visiopatch.capture import (
        _record, build_meta, probe_frames, sha256_of, transcode,
    )
    from visiopatch.perceive import perceive
    from playwright.sync_api import sync_playwright

    case = BENCH / case_id
    meta = read_meta(case_id)
    url = str(meta["url"])
    duration_s = float(meta["duration_s"])
    record_s = duration_s + 0.5
    dest = _repair_dir(case_id)
    out: dict = {"capture_ok": False, "sam": [], "shot": None}
    with ServedCase(case):
        webm, load_offset_s = _record(url, record_s)
        mp4 = dest / f"cap_{tag}.mp4"
        transcode(webm, mp4, record_s)
        digest = sha256_of(mp4)
        frames = probe_frames(mp4, record_s)
        (dest / f"cap_{tag}.meta.json").write_text(json.dumps(build_meta(
            case_id, "candidate", url, duration_s, record_s,
            round(load_offset_s, 3), frames, digest), indent=2) + "\n")
        out.update({"capture_ok": True, "frames": frames,
                    "mp4": str(mp4.relative_to(REPO_ROOT))})
        with sync_playwright() as pw:
            browser = launch_chromium(pw)
            try:
                page = browser.new_page(
                    viewport={"width": 1280, "height": 720})
                page.goto(url, wait_until="load", timeout=30000)
                page.wait_for_timeout(int(duration_s * 1000))
                shot = dest / f"shot_{tag}.png"
                page.screenshot(path=str(shot))
                out["shot"] = str(shot.relative_to(REPO_ROOT))
            finally:
                browser.close()
    # Same noun chain as Step 4 run_perceive: primary then repro-listed
    # fallbacks, first hit wins. Predicate v2: capture + screenshot prove
    # the rebuild serves; SAM frames are vocabulary-dependent (PROJECT.md
    # decisions 8-9: d1/k864/k950 never had a Step-4 hit), so the SAM leg
    # passes on completion with hit/miss recorded, not on frames > 0.
    fallbacks = meta.get("noun_fallbacks", {})
    for noun in meta.get("noun_phrases", []):
        for cand in [noun] + list(fallbacks.get(noun, [])):
            summary = perceive(mp4, cand, dest / f"sam_{tag}_{cand}.json")
            summary["requested_noun"] = noun
            out["sam"].append(summary)
            if summary.get("frames", 0) > 0:
                break
    out["visual_pass"] = bool(out["capture_ok"] and out["shot"]
                               and out["sam"])
    out["visual_rule"] = ("v2: capture+screenshot+SAM-completed; "
                          "frames recorded, not gated")
    return out


def _run_gate2_kaplay(case_id: str, tag: str, worktree: Path,
                      baseline_pw_failures: list[str] | None = None,
                      ) -> dict:
    """Gate 2 for a kaplay candidate: build, stage, serve, test, restore.

    The kaplay playwright suite fails 18 tests at the unmodified baseline
    in this environment, so the suite gate compares failure sets: a
    candidate passes when it introduces no NEW failures.
    """
    case = BENCH / case_id
    meta = read_meta(case_id)
    rec: dict = {"kind": "kaplay"}
    pinned = R.served_bundle_path(case_id).read_bytes()
    pinned_sha = hashlib.sha256(pinned).hexdigest()
    rec["pinned_sha"] = pinned_sha
    saved: bytes | None = None
    try:
        build = R.build_kaplay_bundle(worktree)
        rec["build"] = {k: v for k, v in build.items() if k != "bundle"}
        if build["exit"] != 0 or not build["exists"]:
            rec.update({"eligible": False, "reason": "build failed"})
            return rec
        saved, staged_sha = R.stage_bundle(case_id, Path(build["bundle"]))
        rec["staged_sha"] = staged_sha
        from visiopatch.bench import ServedCase as _ServedCase
        with _ServedCase(case):
            gate_exit, gate_tail = R.run_gate1_test(case_id, str(meta["url"]))
        rec.update({"gate1_exit": gate_exit, "gate1_tail": gate_tail})
        rec["visual"] = _visual_confirm(case_id, tag)
        tsc_exit, tsc_tail = R.run_tsc(worktree)
        rec.update({"suite": "npm run check (tsc)",
                    "tsc_exit": tsc_exit, "tsc_tail": tsc_tail})
        pw_log = _repair_dir(case_id) / f"pw_{tag}.log"
        pw_exit, pw_tail = R.run_playwright_suite(worktree, out_path=pw_log)
        pw_failures = R.playwright_failures(
            pw_log.read_text(encoding="utf-8", errors="replace"))
        new_failures = sorted(set(pw_failures)
                              - set(baseline_pw_failures or []))
        rec.update({"playwright_exit": pw_exit, "playwright_tail": pw_tail,
                    "playwright_failures": pw_failures,
                    "playwright_new_failures": new_failures})
    finally:
        if saved is not None:
            rec["restored_sha"] = R.restore_bundle(case_id, saved)
            rec["restore_ok"] = rec["restored_sha"] == pinned_sha
    return rec


def _run_gate2_d1(case_id: str, tag: str, worktree: Path,
                   allowed: list[str]) -> dict:
    """Gate 2 for the d1 app candidate: swap sources, serve, test, restore."""
    case = BENCH / case_id
    meta = read_meta(case_id)
    app = case / "app"
    saved = {p: (app / p).read_bytes() for p in allowed}
    rec: dict = {"kind": "d1", "suite": "none"}
    try:
        for p in allowed:
            shutil.copyfile(worktree / p, app / p)
        from visiopatch.bench import ServedCase as _ServedCase2
        with _ServedCase2(case):
            gate_exit, gate_tail = R.run_gate1_test(case_id, str(meta["url"]))
        rec.update({"gate1_exit": gate_exit, "gate1_tail": gate_tail})
        rec["visual"] = _visual_confirm(case_id, tag)
    finally:
        for p, data in saved.items():
            (app / p).write_bytes(data)
        head = sh("git", "-C", str(app), "rev-parse", "HEAD")
        rec["restore_ok"] = head == meta["buggy_sha"]
    return rec


def _baseline_check(case_id: str, snapshot: Path) -> dict:
    """Build the UNMODIFIED snapshot, serve it, run the Gate 1 test.

    It must FAIL (behaving like the pinned buggy bundle). If it passes,
    the build path is wrong and no candidate result counts.
    """
    from visiopatch.bench import ServedCase
    meta = read_meta(case_id)
    rec: dict = {"kind": "baseline"}
    if R.is_kaplay_case(case_id):
        build = R.build_kaplay_bundle(snapshot)
        rec["build"] = {k: v for k, v in build.items() if k != "bundle"}
        if build["exit"] != 0 or not build["exists"]:
            rec.update({"pass": False, "reason": "baseline build failed"})
            return rec
        pinned = R.served_bundle_path(case_id).read_bytes()
        saved, _ = R.stage_bundle(case_id, Path(build["bundle"]))
        try:
            with ServedCase(BENCH / case_id):
                exit_code, tail = R.run_gate1_test(
                    case_id, str(meta["url"]))
        finally:
            R.restore_bundle(case_id, saved)
            restored = R.served_bundle_path(case_id).read_bytes()
            rec["restore_ok"] = restored == pinned
        rec.update({"gate1_exit": exit_code, "gate1_tail": tail[-500:]})
        rec["pass"] = exit_code != 0
        if exit_code == 0:
            rec["reason"] = ("baseline Gate 1 test PASSED on the "
                             "unmodified build: build path is wrong")
    else:
        rec.update({"pass": True, "reason": "d1 serves sources directly"})
    return rec


def _gate2_candidate(case_id: str, rec: dict, worktree: Path,
                     guard: dict,
                     baseline_pw: list[str] | None = None) -> dict:
    """Run Gate 2 for one candidate record in place."""
    rec["guard"] = guard
    if rec.get("contamination", {}).get("contaminated"):
        rec.update({"eligible": False, "reason": "contaminated log"})
        return rec
    if not guard["ok"]:
        rec.update({"eligible": False,
                    "reason": f"guard rejected: {guard['rejected']}"})
        return rec
    tag = rec["tag"]
    if R.is_kaplay_case(case_id):
        gate = _run_gate2_kaplay(case_id, tag, worktree,
                                 baseline_pw_failures=baseline_pw)
        suite_pass = (gate.get("tsc_exit") == 0
                      and not gate.get("playwright_new_failures"))
        gate_ok = (gate.get("gate1_exit") == 0 and suite_pass
                   and gate.get("visual", {}).get("visual_pass", False))
    else:
        gate = _run_gate2_d1(case_id, tag, worktree, guard["allowed"])
        gate_ok = (gate.get("gate1_exit") == 0
                   and gate.get("visual", {}).get("visual_pass", False))
    rec["gate2"] = gate
    rec["eligible"] = bool(gate_ok)
    if not gate_ok:
        rec["reason"] = "gate 2 failed"
    return rec


def _record_from_log(case_id: str, tag: str, strategy: dict,
                      prompt: Path, log: Path, workdir: Path) -> dict:
    """Rebuild a candidate record from an existing run (gate2-only mode)."""
    run = {"exit": 0, "model": R.model_of_log(log), "elapsed_s": None,
           "flags": R.MUSE_EXEC_FLAGS, "worktree": R.rel_str(str(workdir)),
           "contamination": R.scan_contamination(log)}
    return {"strategy": strategy["name"], "tag": tag,
            "prompt": str(prompt.relative_to(REPO_ROOT)),
            "log": str(log.relative_to(REPO_ROOT)),
            "rerun": "gate2-only (no new model run)", **run}


def run_case(case_id: str, gate2_only: bool = False,
             extra_args: list[str] | None = None) -> dict:
    print(f"[repair] snapshot {case_id}", flush=True)
    if gate2_only:
        snapshot = R.snapshot_dir(case_id)
        if not (snapshot / ".git").is_dir():
            raise RuntimeError(f"{case_id}: no snapshot for gate2-only")
    else:
        snapshot = R.build_snapshot(case_id)
    snap_verdict = R.verify_snapshot(snapshot)
    print(f"[repair] snapshot commits={snap_verdict['commits']} "
          f"remotes={snap_verdict['remotes']!r} ok={snap_verdict['ok']}",
          flush=True)
    if not snap_verdict["ok"]:
        raise RuntimeError(f"{case_id}: snapshot isolation failed")

    deps = R.install_snapshot_deps(snapshot)
    print(f"[repair] deps: {deps}", flush=True)

    prompts = R.write_repair_prompts(case_id)
    diagnosis = json.loads(
        (REPO_ROOT / "runs" / case_id / "rootcause.json").read_text()
    )["diagnosis"]
    strategies = diagnosis["fix_strategies"]

    baseline = _baseline_check(case_id, snapshot)
    print(f"[repair] baseline_build_gate1 pass={baseline.get('pass')} "
          f"gate1_exit={baseline.get('gate1_exit')}", flush=True)
    if not baseline.get("pass"):
        raise RuntimeError(f"{case_id}: baseline check failed: "
                           f"{baseline.get('reason')}")
    baseline_pw: list[str] | None = None
    if R.is_kaplay_case(case_id):
        pw_exit, pw_tail = R.run_playwright_suite(
            snapshot, out_path=_repair_dir(case_id) / "pw_baseline.log")
        baseline_pw = R.playwright_failures(
            (_repair_dir(case_id) / "pw_baseline.log").read_text(
                encoding="utf-8", errors="replace"))
        baseline["playwright_exit"] = pw_exit
        baseline["playwright_failures"] = baseline_pw
        print(f"[repair] baseline playwright: exit={pw_exit} "
              f"failures={len(baseline_pw)}", flush=True)

    repair_dir = _repair_dir(case_id)
    candidates: list[dict] = []
    for i, (prompt, strategy) in enumerate(zip(prompts, strategies), 1):
        tag = f"s{i}"
        log = repair_dir / f"cand_{tag}.jsonl"
        if gate2_only:
            workdir = snapshot / f"work_{tag}"
            if not workdir.is_dir() or not log.is_file():
                raise RuntimeError(
                    f"{case_id} {tag}: missing worktree/log for gate2-only")
            R.link_worktree_deps(workdir, snapshot)
            rec = _record_from_log(case_id, tag, strategy, prompt, log,
                                   workdir)
            print(f"[repair] {case_id} {tag} gate2-only rescan: "
                  f"contaminated={rec['contamination']['contaminated']} "
                  f"v1={rec['contamination']['v1']}", flush=True)
        else:
            print(f"[repair] {case_id} candidate {tag}: "
                  f"{strategy['name']}", flush=True)
            workdir = R.make_worktree(snapshot, tag)
            R.link_worktree_deps(workdir, snapshot)
            run = R.run_candidate(prompt, snapshot, workdir, log,
                                  extra_args=extra_args)
            print(f"[repair] {tag} exit={run['exit']} model={run['model']} "
                  f"contaminated={run['contamination']['contaminated']} "
                  f"worktree={run['worktree']!r}", flush=True)
            rec = {"strategy": strategy["name"], "tag": tag,
                   "prompt": str(prompt.relative_to(REPO_ROOT)),
                   "log": str(log.relative_to(REPO_ROOT)), **run}
            worktree = workdir
            if run["exit"] != 0 or worktree is None:
                rec.update({"eligible": False,
                            "reason": "muse exec failed or worktree lost"})
                candidates.append(rec)
                continue
        guard = R.guard_diff(workdir, case_id)
        candidates.append(
            _gate2_candidate(case_id, rec, workdir, guard, baseline_pw))

    eligible = [c for c in candidates if c.get("eligible")]
    picked = min(eligible, key=lambda c: c["guard"]["lines_changed"],
                 default=None)
    doc: dict = {
        "case_id": case_id,
        "snapshot": snap_verdict,
        "deps": deps,
        "baseline_build_gate1": baseline,
        "contamination_rule": "inputs-only-v2 (tool-call inputs; "
                              "local file content never counts)",
        "muse_flags": R.MUSE_EXEC_FLAGS,
        "max_model_steps": R.MAX_MODEL_STEPS,
        "cost_note": SUBSCRIPTION_NOTE,
        "candidates": candidates,
        "picked": None,
    }
    if picked is not None:
        picked_wt = Path(picked["worktree"])
        worktree = picked_wt if picked_wt.is_absolute() else REPO_ROOT / picked_wt
        diff = subprocess.run(
            ["git", "diff", "HEAD", "--"], cwd=worktree, capture_output=True,
            text=True, check=True).stdout
        (REPO_ROOT / "runs" / case_id / "fix.patch").write_text(
            diff, encoding="utf-8")
        overlap = R.suspect_file_overlap(case_id, picked["guard"]["allowed"])
        doc["picked"] = {
            "strategy": picked["strategy"], "tag": picked["tag"],
            "files": picked["guard"]["allowed"],
            "lines_changed": picked["guard"]["lines_changed"],
            "model": picked.get("model"),
        }
        doc["overlap"] = overlap
    doc = R.sanitize_record(doc)
    (REPO_ROOT / "runs" / case_id / "gate2.json").write_text(
        json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return doc


def main() -> int:
    parser = argparse.ArgumentParser(description="Step 6 repair + Gate 2")
    parser.add_argument("--only", default="",
                        help="comma-separated case ids (default: all)")
    parser.add_argument("--gate2-only", action="store_true",
                        help="rerun Gate 2 on existing worktrees/logs; "
                             "no new model runs")
    args = parser.parse_args()
    ids = case_ids(args.only)
    results = {}
    for case_id in ids:
        gate1 = json.loads(
            (REPO_ROOT / "runs" / case_id / "gate1.json").read_text())
        if not gate1.get("gate_pass"):
            print(f"[repair] {case_id}: Gate 1 failed, no repair",
                  flush=True)
            continue
        results[case_id] = run_case(case_id, gate2_only=args.gate2_only)
    for case_id, doc in results.items():
        n_ok = sum(1 for c in doc["candidates"] if c.get("eligible"))
        picked = (doc["picked"] or {}).get("strategy", "none")
        print(f"{case_id}: {n_ok}/{len(doc['candidates'])} eligible "
              f"picked={picked}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
