# VisioPatch — Decision Record

Status: Final

## Goal
Live smoke test `scripts/smoke.py` (not pytest): SAM 3.1, Spark 1.3, Wonder, `muse exec` against real APIs; each check independent.

## Decisions
1. Decision record lives here (`PROJECT.md`, repo root). Decisions kept to 1–3 lines each.
2. Exit 1 if any check FAILs, 0 only if all PASS. All four checks always run regardless.
3. `muse exec`: fresh temp dir, `--json --disable-approval --max-model-steps 10`. PASS = exit 0 AND a script re-run of pytest inside that dir also exits 0.
4. Spark file part: `{"type":"input_file","file_id":fid}` in user-message content beside `input_text`. If rejected, print API error verbatim and FAIL; no fallback shape.
5. Timeouts (timeout = FAIL): SAM 300s, Spark 180s, Wonder 180s, `muse exec` 300s.
6. Final cost estimate from token usage: Spark $1.25 in / $4.25 out, Wonder $4 in / $20 out per Mtok; SAM $0.20 per 1k frames.
7. `muse exec` also runs `--no-session-log` (sandbox home is read-only for session logs). ROS pytest-plugin pollution is fixed by unsetting `PYTHONPATH` in the venv, not in this script.

## Assumed (accepted)
- Uploads cached by file sha256 in `smoke_out/cache.json`; SAM and Spark share one `file_id` for the same mp4.
- SAM frame count = distinct frame indices in parsed records; track count = distinct `object_id`s. One noun phrase: "ping pong ball".
- Raw outputs saved per check under `smoke_out/`; token usage printed when returned, else `n/a` (never a failure).
- Sync `openai` SDK, `base_url https://api.meta.ai/v1`, `MODEL_API_KEY` via `python-dotenv`; cookbook `sam_api.py` streaming shim ported, not imported.

## Open
None.
