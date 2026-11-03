"""Wonder 1.0 Playwright test generation for VisioPatch bench cases.

``generate_test`` asks Wonder (reasoning effort high, same client shape
as ``scripts/smoke.py``) to write one deterministic pytest test for a
case. The test loads the served page at 1280x720 through
``visiopatch.browser.launch_chromium`` and asserts the reported
symptom: DOM geometry for page cases, canvas pixel sampling or
``page.evaluate`` on game state for canvas cases.

The generated code is never trusted: it must compile and pass AST
checks (no sleeps beyond the case duration, no model or network
imports, no banned terms) before it may run. Results are cached by
sha256 of the inputs plus a prompt version.

No leakage: the prompt receives only the report line, the sanitized
reported analysis, the diagnosis produced from the current revision,
the page URL and duration, and — on a retry — test output produced
against the current revision only. Revision hashes, PR or issue text,
diffs, git history, and state labels never enter a prompt.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

from visiopatch.bench import REPO_ROOT, read_meta
from visiopatch.perceive import CACHE_DIR
from visiopatch.rootcause import (
    WONDER_MODEL,
    WonderFn,
    _call_wonder,
    load_reported,
    token_cost_usd,
)

TESTGEN_PROMPT_VERSION = 7
TESTGEN_EFFORT = "high"

GENERATED_DIR = REPO_ROOT / "tests_generated"

# Two-phase Gate 1 markers. Phase A proves the test exercises the right
# page (boot + target selector present); Phase B asserts the correct
# visual behaviour. Gate 1 passes only when A prints and B raises
# AssertionError — a TimeoutError is flakiness, never a pass.
PHASE_A_MARKER = "VISIOPATCH_PHASE_A_OK"
PHASE_B_MARKER = "VISIOPATCH_PHASE_B"

_BANNED_IMPORTS = {"openai", "requests", "urllib", "socket", "subprocess",
                   "httpx", "aiohttp"}
_BANNED_CALLS = {"sleep"}  # time.sleep / asyncio.sleep by attribute name
_BANNED_TEXT = ("fix_sha", "buggy_sha", "MODEL_API_KEY", "api.meta.ai")


def assertion_strategy(case_id: str) -> str:
    """Harness-side assertion family for a case; carries no cause info."""
    if case_id.startswith("d"):
        return ("Assert with DOM geometry: use getBoundingClientRect via "
                "page.evaluate on the relevant elements and compare "
                "positions, sizes, or overlap numerically.")
    return ("Assert on the canvas game: first look for state the page "
            "itself exposes — element text or readable objects via "
            "page.evaluate — and use that when present. Otherwise sample "
            "canvas pixels via page.screenshot (not by drawing the "
            "WebGL canvas into a 2d canvas), classifying a pixel by its "
            "dominant colour channel with wide margins rather than "
            "narrow absolute channel ranges, since rendered colours "
            "are rarely pure. Compare positions or motion numerically "
            "over time.")


def build_testgen_prompt(report: str, reported: dict[str, Any],
                         diagnosis: dict[str, Any], url: str,
                         duration_s: float, strategy: str,
                         feedback: str | None = None,
                         steps: list[dict[str, Any]] | None = None) -> str:
    diag = {k: diagnosis.get(k) for k in
            ("root_cause", "file", "line_range", "suspect_function",
             "mechanism", "test_spec", "confidence")}
    prompt = (
        "Write one deterministic pytest test in Python that checks a "
        "reported visual behaviour of a web page served locally.\n"
        f"A user reported: {report}\n"
        "An automated video analysis of the page, prompted with that "
        "report, produced this observation (a hint that may be wrong):\n"
        f"{json.dumps(reported, indent=None)}\n"
        "A source-code diagnosis of the current page (also a hint that "
        "may be wrong) is:\n"
        f"{json.dumps(diag, indent=None)}\n"
        "Build the test on the diagnosis test_spec: assert the measured "
        "quantities and direction of change it names, translated into "
        "page-level measurements. Where the test_spec and the observed "
        "description disagree, follow the measured quantities.\n"
        f"The page is served at the URL in the environment variable "
        f"VISIOPATCH_CASE_URL. It shows its behaviour within "
        f"{duration_s} seconds with no input.\n"
        f"{strategy}\n"
        "Requirements:\n"
        "- Reply with Python code only, no other text.\n"
        "- One test function named test_reported_symptom.\n"
        "- Use sync_playwright from playwright.sync_api, and launch the "
        "browser with launch_chromium from visiopatch.browser (import "
        "it as: from visiopatch.browser import launch_chromium). Call "
        "it exactly as browser = launch_chromium(playwright), where "
        "playwright is the object from sync_playwright(); do not pass "
        "playwright.chromium. Create the page with viewport 1280x720.\n"
        "- Read the URL from os.environ['VISIOPATCH_CASE_URL']; do not "
        "hard-code a URL.\n"
        "- Assert the behaviour the user reported should have, so the "
        "test passes only when the page behaves correctly. Base the "
        "assertions on the observed behaviour and numbers in the "
        "inputs, not on source-code text.\n"
        "- The 'expected' description in the inputs is a hint that may "
        "be wrong in detail. Do not turn it into assertions verbatim. "
        "Assert measured quantities instead: how positions or "
        "velocities actually change, or an invariant the correct "
        "behaviour must satisfy.\n"
        "- Keep assertions minimal: one or two numeric checks on the "
        "object the report is about. Do not add checks about secondary "
        "motion (such as a moving object stopping, slowing, or "
        "rebounding by a specific amount) unless the inputs numerically "
        "establish that exact motion.\n"
        "- First reproduce the reported behaviour through page "
        "interaction when it is not present at load (scroll the "
        "reported element into the relevant region, open the popup, "
        "hover): drive the page with page.evaluate (scroll, "
        "scrollIntoView), mouse, or keyboard, then assert the measured "
        "quantities. Never call time.sleep or asyncio.sleep; poll page "
        "state in a short loop instead.\n"
        "- Be deterministic: no randomness, no current-time or date "
        "dependence, no external network requests.\n"
        f"- Do not sleep in total longer than {duration_s} seconds. "
        "Prefer polling page state with page.evaluate in a short loop "
        "over long waits.\n"
        "- Close the browser in a finally block.\n"
        "- Structure the test in two phases. Phase A: load the page, "
        "assert it booted (body or the reported element exists) and "
        "assert the target selector the report is about exists; then "
        "print exactly VISIOPATCH_PHASE_A_OK. Phase B: assert the "
        "correct visual behaviour with one or two numeric checks whose "
        "failure messages start with exactly VISIOPATCH_PHASE_B:. "
        "Never catch AssertionError; never assert inside a retry loop "
        "that could time out instead of failing.\n"
    )
    if steps:
        prompt += (
            "Operator-supplied reproduction steps for this page "
            "(perform in order, before asserting):\n"
            f"{json.dumps(steps)}\n"
        )
    if feedback:
        prompt += (
            "A previous version of this test did not behave as required "
            "when run against the current page. Its test-runner output "
            "was:\n"
            f"{feedback}\n"
            "Rewrite the test so it checks the reported behaviour "
            "correctly.\n"
        )
    return prompt


def extract_code(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t.strip())
    return t.strip() + "\n"


def _phase_b_asserts(tree: ast.AST, code: str) -> int:
    """Count asserts carrying the Phase B marker in their message."""
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        segment = ast.get_source_segment(code, node) or ""
        if PHASE_B_MARKER in segment:
            count += 1
    return count


def gate1_verdict(exit_code: int, output: str) -> dict[str, Any]:
    """Two-phase Gate 1 verdict for one run on the current (buggy) build.

    Pure (offline-testable): Phase A must have passed (marker printed)
    and Phase B must have failed with AssertionError. A pass, a missing
    Phase A, a TimeoutError, or any other error all fail the gate —
    each with a retryable reason, never a bare boolean.
    """
    out = output or ""
    if exit_code == 0:
        return {"gate_pass": False,
                "reason": "test passed on the current build: Phase B did "
                          "not fail, so no bug is demonstrated"}
    if PHASE_A_MARKER not in out:
        return {"gate_pass": False,
                "reason": "Phase A did not pass (no "
                          f"{PHASE_A_MARKER} in output): the test never "
                          "proved it found the page and target selector"}
    if "TimeoutError" in out:
        return {"gate_pass": False,
                "reason": "Phase B timed out (TimeoutError), which is "
                          "flakiness rather than a visual assertion failure"}
    if "AssertionError" not in out:
        return {"gate_pass": False,
                "reason": "Phase B did not raise AssertionError: "
                          "no visual assertion failed"}
    return {"gate_pass": True,
            "reason": "Phase A passed and Phase B failed with "
                      "AssertionError as required"}


# Identifiers that only ever name the build state under test. Bare
# "bug"/"fix" and CSS values such as "fixed"/"sticky" are normal words
# and are never banned on their own.
_STATE_LABEL_NAMES = {"buggy", "fixed", "patched"}
_STATE_LABEL_PHRASES = ("fix build", "fixed build", "buggy build",
                        "before fix", "after fix",
                        "before the fix", "after the fix")


def _state_label_errors(code: str) -> list[str]:
    """Token-aware state-label check.

    Comments are prose and are ignored entirely; only NAME and STRING
    tokens are examined. A NAME token counts when it is (or contains)
    a state label; a STRING counts on whole-word labels or the
    label phrases above. Each error names the matched word and its
    line.
    """
    import io
    import tokenize
    errs: list[str] = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return []  # the compile check already reported; don't pile on
    for tok_type, tok_str, (srow, _), _, line in tokens:
        low = tok_str.lower()
        if tok_type == tokenize.NAME:
            if low in _STATE_LABEL_NAMES or "buggy" in low:
                errs.append(f"state label {tok_str!r} at line {srow}: "
                            f"{line.strip()[:120]}")
        elif tok_type == tokenize.STRING:
            hit = next((p for p in _STATE_LABEL_PHRASES if p in low), None)
            if hit is None:
                word = re.search(r"\b(buggy|patched)\b", low)
                hit = word.group(0) if word else None
            if hit is not None:
                errs.append(f"state label {hit!r} at line {srow}: "
                            f"{line.strip()[:120]}")
    return errs


def validate_generated_code(code: str, duration_s: float) -> list[str]:
    """Compile + AST checks. Empty list means the code may run."""
    errs: list[str] = []
    try:
        compile(code, "<generated>", "exec")
    except SyntaxError as exc:
        return [f"does not compile: {exc}"]
    low = code.lower()
    for term in _BANNED_TEXT:
        if term.lower() in low:
            errs.append(f"contains banned term: {term}")
    errs.extend(_state_label_errors(code))
    if re.search(r"[0-9a-f]{40}", low) is not None:
        errs.append("contains a 40-char hex string")
    tree = ast.parse(code)
    test_fns = [n.name for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef)
                and n.name.startswith("test_")]
    if test_fns != ["test_reported_symptom"]:
        errs.append("must define exactly one test: test_reported_symptom")
    if PHASE_A_MARKER not in code:
        errs.append(f"must print {PHASE_A_MARKER} after the Phase A checks")
    asserts = sum(isinstance(n, ast.Assert) for n in ast.walk(tree))
    if asserts < 2:
        errs.append("must have at least two asserts (Phase A boot/selector "
                    "plus Phase B visual)")
    elif _phase_b_asserts(tree, code) < 1:
        errs.append(f"must have a Phase B assert messaging {PHASE_B_MARKER}")
    calls_launch = any(
        isinstance(n, ast.Call) and (
            (isinstance(n.func, ast.Name) and n.func.id == "launch_chromium")
            or (isinstance(n.func, ast.Attribute)
                and n.func.attr == "launch_chromium"))
        for n in ast.walk(tree))
    if not calls_launch:
        errs.append("must use launch_chromium from visiopatch.browser")
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "launch_chromium" and node.args
                and isinstance(node.args[0], ast.Attribute)):
            errs.append(
                "launch_chromium takes the playwright object, not an attribute of it")
    if "VISIOPATCH_CASE_URL" not in code:
        errs.append("must read the URL from VISIOPATCH_CASE_URL")
    if "1280" not in code or "720" not in code:
        errs.append("must create the page at 1280x720")
    has_assert = any(isinstance(n, ast.Assert) for n in ast.walk(tree))
    if not has_assert:
        errs.append("must contain at least one assert")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _BANNED_IMPORTS:
                    errs.append(f"banned import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod in _BANNED_IMPORTS:
                errs.append(f"banned import: {node.module}")
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else "")
            if name in _BANNED_CALLS:
                errs.append("must not call sleep")
            if name == "wait_for_timeout" and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, (int, float)):
                    if arg.value > duration_s * 1000:
                        errs.append(
                            "wait_for_timeout exceeds the case duration")
                else:
                    errs.append("wait_for_timeout must use a literal number")
    return errs


def _cache_path(payload: dict[str, Any]) -> Path:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return CACHE_DIR / (
        f"{digest[:16]}_wonder-testgen-v{TESTGEN_PROMPT_VERSION}"
        f"-{digest[16:28]}.json")


def generate_test(case_id: str, diagnosis: dict[str, Any],
                  wonder_fn: WonderFn = _call_wonder,
                  budget_check: Callable[[float], None] | None = None,
                  feedback: str | None = None) -> dict[str, Any]:
    """One Wonder call writing the generated test source for a case.

    Returns {"code", "cost_usd", "cached", "usage", ...}. The caller
    writes the code to disk; retries pass ``feedback`` (test output from
    the current revision only), which changes the cache key.
    """
    from visiopatch.capture import validate_actions
    meta = read_meta(case_id)
    report = str(meta["report"])
    duration_s = float(meta["duration_s"])
    reported = load_reported(case_id)
    steps = validate_actions(meta.get("actions"))
    prompt = build_testgen_prompt(
        report, reported, diagnosis, str(meta["url"]), duration_s,
        assertion_strategy(case_id), feedback, steps)
    payload = {"kind": "testgen", "report": report, "reported": reported,
               "diagnosis": {k: diagnosis.get(k) for k in
                             ("root_cause", "file", "line_range",
                              "suspect_function", "mechanism", "test_spec",
                              "confidence")},
               "duration_s": duration_s,
               "steps": steps,
               "feedback": feedback}
    path = _cache_path(payload)
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            code = doc["code"]
            if not validate_generated_code(code, duration_s):
                return {"code": code, "cost_usd": 0.0, "cached": True,
                        "usage": None, "model": WONDER_MODEL,
                        "prompt_version": TESTGEN_PROMPT_VERSION,
                        "effort": TESTGEN_EFFORT}
        except (json.JSONDecodeError, OSError, KeyError):
            pass
    if budget_check is not None:
        budget_check(0.75)
    text, usage = wonder_fn(prompt, TESTGEN_EFFORT)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{path.stem}_raw.txt").write_text(text + "\n", encoding="utf-8")
    code = extract_code(text)
    errs = validate_generated_code(code, duration_s)
    if errs:
        raise ValueError("invalid generated test: " + "; ".join(errs))
    path.write_text(json.dumps({"code": code}, indent=2) + "\n",
                    encoding="utf-8")
    return {"code": code, "cost_usd": token_cost_usd(usage),
            "cached": False, "usage": usage, "model": WONDER_MODEL,
            "prompt_version": TESTGEN_PROMPT_VERSION,
            "effort": TESTGEN_EFFORT}


def write_generated_test(case_id: str, code: str) -> Path:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    dest = GENERATED_DIR / f"test_{case_id}.py"
    dest.write_text(code, encoding="utf-8")
    return dest


def scrub_secrets(text: str) -> str:
    """Redact key material before a reply is logged."""
    import os
    out = text or ""
    key = os.environ.get("MODEL_API_KEY")
    if key and len(key) >= 4:
        out = out.replace(key, "[redacted-api-key]")
    out = re.sub(r"(?i)\bmodel_api_key\b", "[redacted-key-name]", out)
    out = re.sub(r"(?i)\b(bearer|api[_-]?key)\s*[:=]\s*\S+",
                 r"\1=[redacted]", out)
    return out


def generate_repo_test(report: str, reported: dict[str, Any],
                       diagnosis: dict[str, Any], url: str,
                       duration_s: float, strategy: str,
                       feedback: str | None,
                       wonder_fn: WonderFn = _call_wonder,
                       budget_check: Callable[[float], None] | None = None,
                       raw_dir: Path | None = None,
                       steps: list[dict[str, Any]] | None = None
                       ) -> dict[str, Any]:
    """Step-5-shaped testgen for an arbitrary repo (the live MCP path).

    Same code-shaped call as :func:`generate_test` (prompt asks for
    Python code only; parsed with :func:`extract_code`, never as JSON —
    the old repo path sent that prompt but parsed ``{"code": ...}`` JSON
    and failed every live run with "testgen-repo reply is not JSON").
    The first 500 chars of the scrubbed raw reply are written to
    ``raw_dir`` (the job dir) so a failed call can be diagnosed offline.
    """
    diag = {k: diagnosis.get(k) for k in
            ("root_cause", "file", "line_range",
             "suspect_function", "mechanism", "test_spec",
             "confidence")}
    from visiopatch.capture import validate_actions
    steps = validate_actions(steps)
    prompt = build_testgen_prompt(report, reported, diagnosis, url,
                                  duration_s, strategy, feedback, steps)
    payload = {"kind": "testgen-repo", "report": report,
               "reported": reported, "diagnosis": diag, "url": url,
               "duration_s": duration_s, "steps": steps,
               "feedback": feedback}
    path = _cache_path(payload)
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            code = doc["code"]
            if not validate_generated_code(code, duration_s):
                return {"code": code, "cost_usd": 0.0, "cached": True,
                        "usage": None, "model": WONDER_MODEL,
                        "prompt_version": TESTGEN_PROMPT_VERSION,
                        "effort": TESTGEN_EFFORT}
        except (json.JSONDecodeError, OSError, KeyError):
            pass
    if budget_check is not None:
        budget_check(0.75)
    text, usage = wonder_fn(prompt, TESTGEN_EFFORT)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{path.stem}_raw.txt").write_text(
        (text or "") + "\n", encoding="utf-8")
    if raw_dir is not None:
        raw_dir.mkdir(parents=True, exist_ok=True)
        seed = ("first" if feedback is None
                else f"retry:{feedback[:200]}")
        tag = hashlib.sha256(seed.encode()).hexdigest()[:8]
        (raw_dir / f"testgen_{tag}_raw500.txt").write_text(
            scrub_secrets(text or "")[:500], encoding="utf-8")
    code = extract_code(text)
    errs = validate_generated_code(code, duration_s)
    if errs:
        raise ValueError("invalid generated test: " + "; ".join(errs))
    path.write_text(json.dumps({"code": code}, indent=2) + "\n",
                    encoding="utf-8")
    return {"code": code, "cost_usd": token_cost_usd(usage),
            "cached": False, "usage": usage, "model": WONDER_MODEL,
            "prompt_version": TESTGEN_PROMPT_VERSION,
            "effort": TESTGEN_EFFORT}
