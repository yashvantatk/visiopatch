"""Bench pages must not leak the verdict on screen (or anywhere in file).

A model watching the clip must judge from pixels and raw values alone:
no labels saying something is a bug, fixed, drifted, wrong or OK.
"""

import json
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BENCH = REPO / "bench"

# `position: fixed` is CSS layout, not a verdict; strip it before scanning.
BANNED = re.compile(
    r"\b(bug|buggy|fix|fixed|drift|error|wrong|broken)\b|\bOK\b"
    r"|not conserved|cropped|cut off", re.IGNORECASE)


def _page_texts() -> list[tuple[str, str]]:
    texts: list[tuple[str, str]] = []
    for page in sorted(BENCH.glob("*/index.html")):
        texts.append((str(page.relative_to(REPO)), page.read_text()))
    for case in sorted(BENCH.iterdir()):
        app = case / "app"
        if not (app / ".git").is_dir():
            continue
        meta = json.loads((case / "repro.json").read_text())
        texts.append((f"{case.name}/app (working tree)",
                      (app / "index.html").read_text()))
        for key in ("buggy_sha", "fix_sha"):
            out = subprocess.run(
                ["git", "-C", str(app), "show", f"{meta[key]}:index.html"],
                capture_output=True, text=True, check=True).stdout
            texts.append((f"{case.name}/app @{key}", out))
    return texts


# Cause words a user report must never name (checked as whole words, so
# symptom language like "anchored" is allowed but "anchor" is not).
CAUSE_WORDS = ["round", "stacking", "z-index", "transform", "ascent",
               "anchor", "quad", "impulse", "fix"]


def test_repro_report_lines_name_no_cause() -> None:
    for repro in sorted(BENCH.glob("*/repro.json")):
        meta = json.loads(repro.read_text())
        report = meta.get("report", "")
        assert isinstance(report, str) and report.strip(), repro.parent.name
        assert "\n" not in report.strip(), repro.parent.name
        for word in CAUSE_WORDS:
            assert not re.search(rf"\b{re.escape(word)}\b", report,
                                 re.IGNORECASE), \
                f"{repro.parent.name}: report names {word!r}"


def test_bench_pages_have_no_verdict_words() -> None:
    pages = _page_texts()
    assert len(pages) >= 6
    for name, text in pages:
        cleaned = text.replace("position: fixed", "")
        hit = BANNED.search(cleaned)
        assert hit is None, f"{name}: verdict word {hit.group(0)!r} leaks"
