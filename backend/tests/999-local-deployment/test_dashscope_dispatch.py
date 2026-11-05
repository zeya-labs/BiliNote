"""Native DashScope selection survives settings and bypasses OpenAI requests."""
import json
from types import SimpleNamespace

import pytest

from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.services import transcriber_config_manager as config_module
from app.services.transcriber_config_manager import TranscriberConfigManager
from app.transcriber import openai_asr
from app.transcriber.openai_asr import OpenAIASRTranscriber


@pytest.fixture
def isolated_manager(tmp_path, monkeypatch):
    # Config normalization normally reads shared defaults from SQLite. These
    # tests use a temporary JSON file and never open the real application DB.
    limits = SimpleNamespace(
        asr_silence_noise_db=lambda: -30.0,
        asr_min_silence_seconds=lambda: 0.5,
        asr_silence_split=lambda: True,
    )
    monkeypatch.setattr(config_module, "_global_limits", lambda: limits)
    return TranscriberConfigManager(str(tmp_path / "transcriber.json"))


def test_dashscope_selection_round_trips_without_exposing_or_replacing_key(isolated_manager):
    manager = isolated_manager
    key = "test-only-dashscope-secret"
    endpoint_id = manager.add_openai_asr(
        "DashScope",
        "https://dashscope.aliyuncs.com/api/v1",
        key,
        "qwen3-asr-flash-filetrans",
        api_style="dashscope",
    )
    manager.update_config("openai-asr", openai_asr_id=endpoint_id)

    # Reload from disk, as a new process / restart would do.
    restored = TranscriberConfigManager(str(manager.path))
    selected = restored.get_active_openai_asr()
    assert selected["api_style"] == "dashscope"
    assert selected["model"] == "qwen3-asr-flash-filetrans"
    assert selected["api_key"] == key
    assert restored.get_config()["transcriber_type"] == "openai-asr"

    public = restored.get_public_config()
    masked = public["openai_asr_endpoints"][0]["api_key"]
    assert "*" in masked
    assert key not in json.dumps(public)
    restored.update_openai_asr(endpoint_id, api_key=masked, timeout=900)
    saved = restored.get_active_openai_asr()
    assert saved["api_key"] == key
    assert saved["api_style"] == "dashscope"
    assert saved["timeout"] == 900


@pytest.mark.parametrize("with_callback", [False, True])
def test_dashscope_wrapper_delegates_before_openai_client(tmp_path, monkeypatch, with_callback):
    from app.transcriber import dashscope_asr

    expected = TranscriptResult(
        language="zh",
        full_text="带真实时间轴的识别结果。",
        segments=[TranscriptSegment(start=0.12, end=1.45, text="带真实时间轴的识别结果。")],
    )
    calls = {}

    class FakeNative:
        def __init__(self, **kwargs):
            calls["options"] = kwargs

        def transcript(self, file_path):
            calls["file_path"] = file_path
            return expected

    def forbidden_openai_client(*args, **kwargs):
        raise AssertionError("Native Filetrans must not construct an OpenAI client")

    monkeypatch.setattr(dashscope_asr, "DashScopeFiletransTranscriber", FakeNative)
    monkeypatch.setattr(openai_asr, "build_openai_client", forbidden_openai_client)
    transcriber = OpenAIASRTranscriber(
        base_url="https://dashscope.aliyuncs.com/api/v1",
        api_key="test-only-dashscope-secret",
        model="qwen3-asr-flash-filetrans",
        timeout=420,
        max_rpm=9,
        api_style="dashscope",
    )
    callback = lambda *args: None
    if with_callback:
        transcriber.progress_callback = callback
    audio = tmp_path / "speech.mp3"
    audio.write_bytes(b"not-read-by-the-native-stub")

    result = transcriber.transcript(str(audio))

    assert result is expected
    assert calls["file_path"] == str(audio)
    options = calls["options"]
    assert options["base_url"] == "https://dashscope.aliyuncs.com/api/v1"
    assert options["api_key"] == "test-only-dashscope-secret"
    assert options["model"] == "qwen3-asr-flash-filetrans"
    assert options["timeout"] == 420
    assert options["max_rpm"] == 9
    assert options["progress_callback"] is (callback if with_callback else None)
