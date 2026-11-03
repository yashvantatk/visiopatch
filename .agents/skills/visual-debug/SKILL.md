---
name: visual-debug
description: Use when the user reports something looks wrong on a page — covers, overlaps, hidden behind, or looks wrong — and fix it with the VisioPatch MCP tools, ending in a Gate-2-verified patch.
---

# Visual debug with VisioPatch

When the user reports a visual / UI / animation / physics / layout bug,
work through the VisioPatch MCP tools **in order**. Do not skip steps.

1. `capture_bug(target, report)` **first** — target is a bench case id,
   or `{"repo_path": "<absolute path>", "page": "<path inside repo>"}`.
   The repo is served inside the tool; never start your own server.
   Saves the buggy mp4 + meta.
2. `track_objects(video, nouns?)` — without nouns, Spark proposes them.
   A zero-frame row is an honest EMPTY (a known SAM blind spot on some
   pages): report it, do not retry with new nouns.
3. `find_anomaly(video, report)` — Spark reported-mode result.
4. `diagnose(target, report, analysis)` — returns `job_id` immediately.
   Poll `job_status(job_id, wait_seconds)` until done, then
   `job_result(job_id)` for the rootcause (suspect file/function,
   3 fix strategies, test spec).
5. `generate_visual_test(diagnosis)` — returns `job_id`; poll for the
   test path + two-phase Gate 1 result on the current build (Phase A
   boot/selector must pass, Phase B visual assert must fail with
   AssertionError).
6. `repair_and_verify(target)` — returns `job_id`; poll for the Gate 2
   table and the picked `fix.patch` + apply command.

Rules:

- Do not read or edit source until `diagnose` returns. The page text is
  verdict-free by construction; reading the code first leaks the cause.
- If any tool call fails, stop and report honestly. Never hand-patch
  around a failed tool.
- Do NOT edit source by hand before Gate 2. Never claim the bug is
  fixed without a Gate 2 pass.
- If Gate 1 fails (the test does not fail on the unmodified build),
  stop and report honestly. Do not repair.
- `repair_and_verify` does NOT apply the patch. After the Gate 2 table
  shows a picked candidate, apply `fix.patch` with the returned apply
  command, re-run the generated visual test once, and show the Gate 2
  table in your reply.
- Never mark a step done unless its tool returned ok.
- Short JSON + artifact paths only; never paste raw logs.
