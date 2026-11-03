"""The visual-debug skill must fire on plain user reports and enforce the
safe order (capture first, no source reads/edits until diagnose, stop on
tool failure). Step 7 failed because the outer agent read index.html
before loading the skill and hand-patched after a tool error."""

from pathlib import Path

SKILL = (Path(__file__).resolve().parent.parent
         / ".agents" / "skills" / "visual-debug" / "SKILL.md")


def _body() -> str:
    return SKILL.read_text(encoding="utf-8").lower().replace("`", "")


def test_skill_exists_and_fires_on_plain_reports() -> None:
    body = _body()
    for phrase in ("covers", "overlaps", "hidden behind", "looks wrong"):
        assert phrase in body, f"skill description misses {phrase!r}"


def test_skill_orders_capture_bug_first() -> None:
    body = _body()
    assert "capture_bug" in body
    assert body.index("capture_bug") < body.index("diagnose")


def test_skill_bans_source_access_before_diagnose() -> None:
    body = _body()
    assert "until diagnose" in body
    assert "do not read" in body or "don't read" in body


def test_skill_bans_hand_patch_on_tool_failure() -> None:
    body = _body()
    assert "stop and report" in body
    assert "never" in body and "hand" in body


def test_skill_never_marks_done_without_tool_ok() -> None:
    body = _body()
    assert "unless" in body and "ok" in body


def test_skill_documents_repro_steps() -> None:
    body = _body()
    assert "steps" in body
    assert "scroll_to" in body


def test_skill_hard_stop_no_manual_fallback() -> None:
    body = _body()
    assert "after any stop" in body
    assert "end the turn" in body
    assert "manual fallback" in body
    assert "by hand" in body
