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

## Benchmark setup (Draft — grill in progress)
Grill target: this file (`PROJECT.md`) per repo convention.
- Settled: artifact boundary = 6 repros only. Each `bench/<id>/` gets a working repro page + `repro.json` + `serve.sh`; kaplay stays in `../kaplay`, never vendored.
- Settled: done = all 6 `repro.json` carry url/actions/duration_s/buggy_sha/fix_sha/noun_phrases with duration_s <= 5; each `serve.sh` runs; each page shows its bug at 1280x720 with no input in <= 5s; no kaplay sources in this repo.
- Settled: later stages deferred. Capture, SAM/Spark/Wonder, testgen/repair are proposals only; accepting this record never approves them. `go` authorizes only the 6-repro boundary above.
- Built: 6/6 repros land (ports 8901-8906). kaplay sources stay in `../kaplay` (single clone); `bench/k*/kaplay.js` are built bundles pinned at buggy_sha, plus test assets (9slice.png, Anton-Regular.ttf).
- Corrected: k950 fix_sha is `231b23f07e5af855ad46d1466228a553b6b26e3d` (the brief's `...b926e3d` has a typo'd extra `9` and resolves to nothing); all four kaplay "merges" are single-parent fix commits, so buggy_sha = fix_sha^.
- Verified: all six serve 200 with bug markers; d1/d2 fix-switch checked live; kaplay fix contrast checked at bundle level (impulse 0→1, maxActualBoundingBoxAscent 0→1, quad-guard return→continue, slice9 offset call appears). Pixel rendering unverified — sandbox has no WebGL.
- Render check: scripts/render_check.py + visiopatch/browser.py (ordered GL backends with WebGL gate, winner cached in render_out/.backend; SHA parent check; fresh browser per phase so the fix swap can't be masked by HTTP cache); render_out/ gitignored. All 36 render_out/<id>/{buggy,fix}_{1s,3s,5s}.png inspected — every bug visible at buggy_sha and gone at fix_sha.
