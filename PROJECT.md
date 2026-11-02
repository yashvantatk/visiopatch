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
8. SAM returns frames=0 at no cost when a noun phrase matches nothing (empty output text → 0 frames → $0.00); a miss is a vocabulary problem, not a pipeline bug. k1174's `box` became `square` because SAM segments both squares but associates nothing with "box".
9. Noun fallback rule: per-noun candidate lists in `repro.json` `noun_fallbacks`; the runner tries primary then fallbacks in order on the buggy clip, stops at the first hit, uses the winner for the fix clip, records the noun used, and only ever tries listed nouns. All-miss is flagged EMPTY, never retried with new nouns.

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

## Spark analysis (Step 4) — blind + reported results
- Built: bench pages stripped of verdict text (raw values only; d1/d2 fixture bundles rebuilt, functional diffs unchanged, new SHAs in repro.json, one-line user "report" in each repro.json, cause-word-free); visiopatch/analyze.py (Spark nouns + anomaly, sha256+prompt-version cache, strict JSON validation); Spark noun stage added after noun_fallbacks in run_perceive (noun_source repro/fallback/spark; all 4 EMPTY primaries still EMPTY after Spark nouns); scripts/run_analyze.py writes analysis/<id>/<state>.json (blind) and .reported.json (reported), $3 budget. All 24 files validate; total Spark spend for Step 4 ≈ $0.71.
- Scores (buggy hit = observed names the real bug; reported hits also checked for frame/bbox landing on it):

  | mode | buggy hit rate | fix false-positive rate |
  |---|---|---|
  | blind v1 | 2/6 (k1174, d1; d2 flagged for the wrong reason) | 2/6 (k1174, d2) |
  | blind v2 | 1/6 (d1 only) | 1/6 (d2) |
  | reported | 2/6 (k1174: frame 45 on the stationary blue square; k950: frame 36 bbox on the top text block) | 1/6 (k864 fix) |

- Read: blind v2 trades false positives for misses (accepts rebounds and steady motion as normal, so k1174's momentum bug goes unseen); the report line is what recovers k1174 and k950, and it also suppresses both v1 fix false positives. d1/d2/k658/k864 reports still miss in reported mode — the reported halves of those clips describe stability, not the fault.
- Confidence carries no signal and must not gate anything downstream: blind v2 0.85–0.95, reported 0.85–0.95 across hits, misses and false positives alike.
- Prompt versions used: blind 2 of 2 max (final), reported 1 of 1. Bench pages and report lines must stay verdict-free (tests/test_bench_pages.py).

## Wonder root cause + Gate 1 testgen (Step 5) — results
- Built: visiopatch/rootcause.py (Wonder pick_files at medium effort, diagnose at high effort; sha256-of-inputs + prompt-version cache in cache/, strict JSON validation; diagnose prompt v2 adds suspect_function, test_spec, and exactly 3 fix_strategies for the later repair fan-out); visiopatch/testgen.py (Wonder at high effort builds one deterministic pytest-playwright test per case on the diagnosis test_spec, via visiopatch/browser.py at 1280x720, py_compile + AST validation; testgen prompt v5); scripts/run_gate1.py is the only state switcher (fails-on-buggy + passes-on-fix, retry with buggy-run pytest output only, max 3 attempts per case). runs/<id>/rootcause.json and runs/<id>/gate1.json are committed; *_raw.txt and cache/ stay gitignored.
- Scoring: root-cause verdict is my judgement of the diagnosis against bench/candidates.md. suspect_file_hit is harness-only scoring (git diff --name-only buggy_sha..fix_sha, app/ repo for d1/d2; whether any picked or diagnosed file was really changed) and never enters a prompt.

| case | root cause | suspect_file_hit | Gate 1 | attempts | cost |
|---|---|---|---|---|---|
| k1174 | match (no momentum/impulse transfer in body.ts) | yes | pass | 1 | $0.1716 |
| k950 | match (font atlas crops ascenders above the em box) | yes | pass | 3 | $0.6754 |
| k864 | match (one degenerate slice aborts the whole 9-slice draw) | yes | pass | 3 | $0.4549 |
| k658 | match (per-tile anchor instead of whole-panel anchor) | yes | fail | 3 | $0.7164 |
| d1-modal-zindex | match (transform on main traps modal z-index) | yes | pass | 1 | $0.2036 |
| d2-carousel-drift | match (rounded cumulative step drifts 1/3px per advance) | yes | fail | 3 | $0.3837 |

- Read: root cause is 6/6 with suspect_file_hit 6/6, so file picking and diagnosis are not the bottleneck. Gate 1 is 4/6: d2's generated test also fails on the fix build, and k658's never separates the builds (one attempt also wasted on a missing PIL import the AST validator does not ban). Test assertion quality, not diagnosis, is what Gate 2 repair should distrust. Confidence (0.85–0.95) again carries no signal and gates nothing.
- Spend: final recorded runs total $2.61; with the k1174 prompt-iteration runs (testgen v1–v4) the whole step cost ≈ $4.06 of the $10 cap.

## Wonder repair + Gate 2 (Step 6) — done, 4/4 picks overlap the human fix
- Harness: `visiopatch/repair.py` (offline snapshot at buggy_sha, leak-checked prompts, allowlist guard, inputs-only contamination scan, Gate 2 helpers) + `scripts/run_repair.py` (`--only`, `--gate2-only`); `runs/<id>/gate2.json` + `runs/<id>/fix.patch` committed, raw muse JSONL logs and `repair/` workdirs gitignored. `tests/test_repair.py` (16 tests) covers prompts, guard, scanner, and parse helpers offline.
- Snapshot isolation: `git archive buggy_sha` into `runs/<id>/repair/src`, fresh `git init` with one `baseline` commit and no remotes; candidates run in caller-owned nested worktrees via `muse exec -w existing` with `--max-model-steps 40` (a `-w create` worktree is deleted on success, losing the diff). Exactly 1 commit + 0 remotes asserted per case. Model on all 12 candidates: `muse-spark-1.3`, subscription, $0 API.
- Contamination rule change (v1 → v2) and reason: v1 scanned the whole JSONL and flagged k1174-s3 for a `github.com` URL inside a locally read `package.json` (tool output, not a lookup). v2 scans tool-call inputs only (fetch/web tool uses; shell commands like curl, wget, git clone/fetch, gh, npm view); local file content never counts. The v1 flag stays noted on s3's record. No web/fetch tool call appears in any candidate log.
- Deps: one install per case snapshot (`npm ci` with lockfile, else `npm install`, falling back to `--legacy-peer-deps` for k950's pre-lockfile tree which trips npm's `edgesOut` peer-dep bug); `../kaplay` ships no node_modules. Symlinked into each worktree before the fan-out so candidates can run tsc; the symlink is a guard exception and never counts toward lines changed.
- Gates: baseline (unmodified snapshot build must FAIL the Gate 1 test, else the build path is wrong — recorded as `baseline_build_gate1`); per candidate Gate 2 = generated test passes + existing suite (kaplay: `tsc` must pass, playwright compared failure-set vs baseline because 18 tests fail at baseline in this env; d1: `none`) + visual (recapture + screenshot + SAM completed; SAM frames recorded, not gated, since frames are vocabulary-dependent — d1/k864/k950 never had a Step-4 SAM hit). Bench state restored to buggy_sha and verified after every stage.
- d2 and k658 reported as Gate 1 failures with no repair, per the step boundary.

| case | eligible | picked strategy | lines | files | overlap |
|---|---|---|---|---|---|
| k1174 | 3/3 | Momentum impulse in physics-resolve handler | 35 | `src/ecs/components/physics/body.ts` | yes |
| d1-modal-zindex | 2/3 (s3 fails Gate 1 on a top-layer variant) | Remove the stacking context from main | 2 | `index.html` | yes (identical to human fix) |
| k864 | 3/3 | Skip only the degenerate slice | 2 | `src/ecs/components/draw/sprite.ts` | yes |
| k950 | 1/3 (s2/s3 fail Gate 1) | Pad and offset atlas rasterization | 69 | `src/core/fontCache.ts`, `src/gfx/formatText.ts` | yes |

- Spend: SAM reruns ≈ $0.05 total (k1174 $0.048, k864 $0.0002, d1/k950 $0 at 0 frames); all `muse exec` on the subscription. Far under the $8 cap.
