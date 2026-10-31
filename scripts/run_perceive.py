#!/usr/bin/env python3
"""Capture both states and perceive every noun phrase for bench cases.

Usage: .venv/bin/python scripts/run_perceive.py [--only id1,id2]

For each case: capture buggy+fix MP4s, then run SAM 3.1 once per noun
phrase per state. Nouns come only from repro.json: if a primary noun
returns 0 tracks on the buggy clip, its ``noun_fallbacks`` are tried in
order, stopping at the first hit. The winning noun is used for the fix
clip and recorded in the tracks file. If every candidate misses, the
noun is flagged EMPTY (never retried with new nouns).

Prints frames, track count and estimated cost per clip, plus a total.
Stops before the projected cost goes over $2.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from visiopatch.bench import BENCH, case_ids, read_meta  # noqa: E402
from visiopatch.capture import FPS, capture  # noqa: E402
from visiopatch.perceive import (  # noqa: E402
    CACHE_DIR,
    cache_stem,
    estimate_cost_usd,
    perceive,
    validate_tracks_doc,
)
from visiopatch.capture import sha256_of  # noqa: E402

PERCEPTION = REPO_ROOT / "perception"
BUDGET_USD = 2.0


def is_cached(mp4: Path, noun: str) -> bool:
    doc_path = CACHE_DIR / f"{cache_stem(sha256_of(mp4), noun)}_doc.json"
    if not doc_path.exists():
        return False
    try:
        doc = json.loads(doc_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return doc.get("sha256") == sha256_of(mp4) and not validate_tracks_doc(doc)


def est_clip_cost(duration_s: float) -> float:
    return estimate_cost_usd(int(duration_s * FPS))


class BudgetExceeded(Exception):
    pass


def perceive_job(job: dict, dest_dir: Path, perceive_fn, pre_call) -> tuple[
        str | None, list[tuple[str, str, dict]]]:
    """Fallback chain for one primary noun.

    Tries ``job["candidates"]`` on the buggy clip in order, stopping at the
    first noun with tracks > 0, then perceives the fix clip once with that
    winner. ``perceive_fn(mp4, noun, dest, requested)`` does one call;
    ``pre_call(noun, est_cost)`` runs before each call and may raise
    ``BudgetExceeded``. Returns ``(winner, rows)`` where each row is
    ``(state, noun_used, summary)``. All-miss means no fix call.
    """
    rows: list[tuple[str, str, dict]] = []
    winner: str | None = None
    for cand in job["candidates"]:
        pre_call(cand, est_clip_cost(job["duration_s"]))
        summary = perceive_fn(job["mp4s"]["buggy"], cand,
                              dest_dir / f"buggy.{job['noun']}.json",
                              job["noun"])
        rows.append(("buggy", cand, summary))
        if summary["tracks"] > 0:
            winner = cand
            break
    if winner is not None:
        pre_call(winner, est_clip_cost(job["duration_s"]))
        summary = perceive_fn(job["mp4s"]["fix"], winner,
                              dest_dir / f"fix.{job['noun']}.json",
                              job["noun"])
        rows.append(("fix", winner, summary))
    return winner, rows


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Capture + perceive bench cases")
    parser.add_argument("--only", default="",
                        help="comma-separated case ids to run (default: all)")
    args = parser.parse_args()
    try:
        ids = case_ids(args.only)
    except ValueError as exc:
        print(f"{exc}", flush=True)
        return 1

    # Capture first (free), so cost projection uses real clips.
    jobs: list[dict] = []  # one per primary noun
    for case_id in ids:
        meta = read_meta(case_id)
        mp4s = {}
        for state in ("buggy", "fix"):
            mp4, sidecar = capture(case_id, state)
            print(f"[capture] {case_id}/{state}: {mp4.name} + {sidecar.name}",
                  flush=True)
            mp4s[state] = mp4
        fallbacks = meta.get("noun_fallbacks", {})
        for noun in meta["noun_phrases"]:
            cands = [noun] + list(fallbacks.get(noun, []))
            jobs.append({"case": case_id, "noun": noun,
                         "candidates": cands, "mp4s": mp4s,
                         "duration_s": float(meta["duration_s"])})

    def remaining_cost(skip_job: int, skip_cands: int) -> float:
        cost = 0.0
        for j, job in enumerate(jobs):
            cands = job["candidates"] if j > skip_job else job["candidates"][skip_cands:]
            # Upper bound: every remaining buggy try + one fix call.
            cost += (len(cands) + 1) * est_clip_cost(job["duration_s"])
        return cost

    total = 0.0
    # (case, state, requested noun, noun used, summary)
    results: list[tuple[str, str, str, str, dict]] = []
    for ji, job in enumerate(jobs):
        case_id, primary = job["case"], job["noun"]
        dest_dir = PERCEPTION / case_id

        # Guard with knowledge of which clip each call hits: buggy tries
        # come first, then at most one fix call with the winner.
        calls_done = 0

        def guard(noun: str, est: float) -> None:
            nonlocal calls_done
            # Which clip is this call for? Calls beyond the candidate list
            # length can only be the single fix call.
            is_fix_call = calls_done >= len(job["candidates"])
            mp4 = job["mp4s"]["fix"] if is_fix_call else job["mp4s"]["buggy"]
            if not is_cached(mp4, noun):
                if is_fix_call:
                    projected = total + remaining_cost(ji + 1, 0) + est
                else:
                    tried = calls_done
                    projected = total + remaining_cost(ji, tried)
                if projected > BUDGET_USD:
                    raise BudgetExceeded(
                        f"projected cost ${projected:.4f} exceeds "
                        f"${BUDGET_USD:.2f} budget")
            calls_done += 1

        try:
            _winner, rows = perceive_job(job, dest_dir, perceive, guard)
        except BudgetExceeded as exc:
            print(f"STOP: {exc}", flush=True)
            return 1
        for state, used, summary in rows:
            total += summary["cost_usd"]
            if total > BUDGET_USD:
                print(f"STOP: spent ${total:.4f} exceeds ${BUDGET_USD:.2f} budget",
                      flush=True)
                return 1
            results.append((case_id, state, primary, used, summary))
            print(f"[perceive] {case_id}/{state}.{primary} as {used!r}: "
                  f"frames={summary['frames']} tracks={summary['tracks']} "
                  f"est=${summary['cost_usd']:.4f}"
                  + (" (cached)" if summary["cached"] else ""), flush=True)

    print(f"\nTotal estimated cost: ${total:.4f}", flush=True)

    # Empties report: primary nouns with 0 buggy tracks across all candidates.
    by_noun: dict[tuple[str, str], dict[str, int]] = {}
    for case_id, state, primary, _used, summary in results:
        by_noun.setdefault((case_id, primary), {})[state] = summary["tracks"]
    empties = [k for k, v in sorted(by_noun.items())
               if v.get("buggy", 0) == 0]
    diffs = [(k, v) for k, v in sorted(by_noun.items())
             if v.get("buggy", 0) > 0 and v.get("fix", 0) != v.get("buggy", 0)]
    if diffs:
        print("\nDIFFERENCES (buggy vs fix track count):", flush=True)
        for (case_id, noun), v in diffs:
            print(f"  {case_id} {noun!r}: buggy={v.get('buggy', 0)} "
                  f"fix={v.get('fix', 0)}", flush=True)
    if empties:
        print("\nEMPTY (no tracks for any listed noun — flagged, not retried):",
              flush=True)
        for case_id, noun in empties:
            print(f"  {case_id} {noun!r}", flush=True)
    if not empties and not diffs:
        print("\nAll nouns tracked in both states.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
