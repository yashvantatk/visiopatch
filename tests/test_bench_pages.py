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


def _git_dir_for(case: Path, tmp: Path) -> Path:
    """Working-tree app repo, or a scratch clone from the committed bundle.

    On a clean clone ``app/`` does not exist (it is gitignored and built
    by ``serve.sh``); the ``app.bundle`` file next to it carries the same
    history, so clone that into ``tmp`` instead of requiring serve.sh.
    """
    app = case / "app"
    if (app / ".git").is_dir():
        return app
    bundle = case / "app.bundle"
    if not bundle.is_file():
        raise AssertionError(
            f"{case.name}: neither app/.git nor app.bundle exists; "
            "run serve.sh first")
    dest = tmp / case.name
    if not (dest / ".git").is_dir():
        subprocess.run(
            ["git", "clone", "-q", str(bundle), str(dest)],
            check=True)
    return dest


def _page_texts(tmp: Path) -> list[tuple[str, str]]:
    texts: list[tuple[str, str]] = []
    for page in sorted(BENCH.glob("*/index.html")):
        texts.append((str(page.relative_to(REPO)), page.read_text()))
    for case in sorted(BENCH.iterdir()):
        if not (case / "repro.json").is_file():
            continue
        if (case / "app.bundle").is_file() or (case / "app").is_dir():
            git_dir = _git_dir_for(case, tmp)
            meta = json.loads((case / "repro.json").read_text())
            if (git_dir == case / "app"
                    and (git_dir / "index.html").is_file()):
                texts.append((f"{case.name}/app (working tree)",
                              (git_dir / "index.html").read_text()))
            for key in ("buggy_sha", "fix_sha"):
                out = subprocess.run(
                    ["git", "-C", str(git_dir), "show",
                     f"{meta[key]}:index.html"],
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


def test_bench_pages_have_no_verdict_words(tmp_path) -> None:
    pages = _page_texts(tmp_path)
    assert len(pages) >= 6
    for name, text in pages:
        cleaned = text.replace("position: fixed", "")
        hit = BANNED.search(cleaned)
        assert hit is None, f"{name}: verdict word {hit.group(0)!r} leaks"


def _visible_text(html: str) -> str:
    """What a viewer (or a model watching the clip) can read: markup,
    styles and scripts stripped, leaving rendered text only. Functional
    CSS/JS (``transform: translateZ(0)`` in a stylesheet, ``Math.round``
    in code) is not visible text and is not checked here."""
    text = re.sub(r"(?is)<style.*?</style>", " ", html)
    text = re.sub(r"(?is)<script.*?</script>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _js_comments(html: str) -> list[str]:
    """``//`` and ``/* */`` comments inside inline scripts: an agent
    reading the source sees these, so cause/verdict words are banned
    there too (code identifiers themselves are the mechanism and stay)."""
    comments: list[str] = []
    for block in re.findall(r"(?is)<script.*?>(.*?)</script>", html):
        comments += re.findall(r"//[^\n]*", block)
        comments += re.findall(r"(?s)/\*.*?\*/", block)
    return comments


# A comment may explain timing but never the mechanism ("loses",
# "cumulative") — same raw-values-only rule as the k1174 HUD.
COMMENT_BANNED = ["cumulative", "loses"]


def test_bench_visible_text_names_no_cause(tmp_path) -> None:
    """Rendered page text must not name the cause (k1174 HUD rule:
    raw values only). Fails on the d1 leak
    ("creates stacking context") until the pages are cleaned."""
    pages = _page_texts(tmp_path)
    assert len(pages) >= 6
    for name, text in pages:
        if not name.endswith(".html") and "/app" not in name:
            continue
        visible = _visible_text(text)
        for word in CAUSE_WORDS:
            assert not re.search(rf"\b{re.escape(word)}\b", visible,
                                 re.IGNORECASE), \
                f"{name}: visible text names cause {word!r}"


def test_d1_d2_comments_name_no_cause(tmp_path) -> None:
    """d1/d2 inline-script comments must not explain the mechanism."""
    pages = _page_texts(tmp_path)
    probed = 0
    for name, text in pages:
        if "/app" not in name and not name.startswith(("d1-", "d2-")):
            continue
        if not text.lstrip().startswith("<!DOCTYPE"):
            continue
        probed += 1
        for comment in _js_comments(text):
            for word in CAUSE_WORDS + COMMENT_BANNED:
                assert not re.search(rf"\b{re.escape(word)}\b", comment,
                                     re.IGNORECASE), \
                    f"{name}: comment names {word!r}: {comment.strip()[:80]}"
    assert probed >= 4
