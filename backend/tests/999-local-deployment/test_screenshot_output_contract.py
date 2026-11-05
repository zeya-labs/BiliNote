"""The screenshot prompt must match the approved-frame postprocessor with OCR off."""

import pytest

from app.gpt import prompt_builder
from app.gpt.universal_gpt import UniversalGPT
from app.services.ocr_assist import WorthDecision


@pytest.fixture(autouse=True)
def base_prompt_without_database(monkeypatch):
    monkeypatch.setattr(
        prompt_builder,
        "resolve_base_prompt",
        lambda: "{video_title}\n{segment_text}\n{tags}",
    )


def make_prompt(**kwargs):
    # create_messages only formats supplied content; no API client is needed.
    gpt = object.__new__(UniversalGPT)
    messages = gpt.create_messages(
        [], title="Video", tags=[], _format=["screenshot"], **kwargs
    )
    return messages[0]["content"]


@pytest.mark.parametrize("ocr_enabled", [False, True])
def test_approved_screenshots_use_numbered_markers_independent_of_ocr(ocr_enabled):
    text = make_prompt(
        ocr_assist=ocr_enabled,
        approved_shots=[WorthDecision(time=12.0, caption="Key diagram")],
    )
    assert "`[[SHOT-1]]` → 00:12（Key diagram）" in text
    assert "禁止自己发明编号或时间点" in text


def test_empty_approved_screenshots_forbid_screenshot_markers_with_ocr_off():
    text = make_prompt(ocr_assist=False, approved_shots=[])
    assert "没有任何画面通过价值判定" in text
    assert "请不要插入任何截图标记" in text
    assert "*Screenshot-[mm:ss]" not in text


def test_legacy_call_without_judgement_retains_timestamp_screenshot_contract():
    text = make_prompt(ocr_assist=False)
    assert prompt_builder.get_screenshot_format() in text
    assert "*Screenshot-[mm:ss]" in text
    assert "[[SHOT-1]]" not in text
