---
name: visiopatch-conventions
description: Project rules for VisioPatch. Load for any code change in this repo.
---
# VisioPatch conventions

## What we build
Video of a visual bug -> SAM 3.1 tracking -> Spark 1.3 anomaly frame -> Wonder root cause
+ deterministic Playwright test (Gate 1: must fail) -> Muse Code worktree fan-out fixes
(Gate 2: generated + existing tests pass, SAM confirms).

## Stack
Python 3.10+, openai SDK (base_url https://api.meta.ai/v1, key MODEL_API_KEY from .env),
meta_sam_parser, Playwright (Chromium, 1280x720), pytest + pytest-playwright, ffmpeg,
OpenCV/numpy, static HTML + Chart.js for reports.

## Layout
visiopatch/  capture.py perceive.py analyze.py testgen.py repair.py mcp_server.py report.py
tests/       unit tests;  tests/visual/ generated visual tests
bench/<id>/  repro.json, serve.sh
prompts/     prompt templates;  runs/<id>/ outputs (gitignored)

## Hard rules
- Tests must NOT call any model or network API. Deterministic only.
- One SAM noun phrase per request; multiple objects = parallel requests on same file_id.
- Videos are mp4 only (convert Playwright webm with ffmpeg, 15 fps).
- Cache API results by file hash; never re-pay for the same clip.
- Models: muse-wonder-1.0 for analyze/testgen/repair, muse-spark-1.3 for video + routine.
- Validate every model JSON output against a schema; never trust raw text.
- Never commit .env or keys.

## Cut list (do not build)
Muse Image cards, voice input, Docker, React, robotics sim, partner credits.
