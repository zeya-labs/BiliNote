"""Native DashScope filetrans wire contract and genuine timestamp preservation."""
import json
from types import SimpleNamespace

import httpx
import pytest

from app.transcriber import dashscope_asr as module
from app.transcriber.dashscope_asr import DashScopeFiletransTranscriber


API = "https://dashscope.aliyuncs.com/api/v1"
UPLOAD = "https://upload.oss-cn-beijing.aliyuncs.com/"
RESULT = "https://result.oss-cn-beijing.aliyuncs.com/result.json?Signature=private-signature"
KEY = "sk-test-not-a-real-credential"


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "private-audio.mp3"
    path.write_bytes(b"audio-bytes")
    return path


@pytest.fixture
def clock(monkeypatch):
    current = [0.0]

    def sleep(seconds):
        current[0] += seconds

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: current[0], sleep=sleep))
    return current


def install_client(monkeypatch, handler):
    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(module, "ProxyConfigManager", lambda: SimpleNamespace(get_proxy_url=lambda: None))


def policy():
    return {"data": {
        "upload_host": UPLOAD, "upload_dir": "account-bound/random-prefix",
        "policy": "private-policy", "signature": "private-form-signature",
        "oss_access_key_id": "private-oss-key", "x_oss_object_acl": "private",
        "x_oss_forbid_overwrite": "true", "max_file_size_mb": 10,
    }}


def transcript_payload():
    return {"file_url": "oss://private/path", "transcripts": [{
        "channel_id": 0, "text": "你好，code review。",
        "sentences": [
            {"begin_time": 750, "end_time": 1950, "text": "你好，", "language": "zh"},
            {"begin_time": 2125, "end_time": 4900, "text": "code review。", "language": "en"},
        ],
    }]}


@pytest.mark.parametrize("model,plural", [
    ("qwen3-asr-flash-filetrans", False),
    ("qwen-audio-3.1-asr-flash-filetrans", True),
])
def test_full_upload_submit_poll_download_contract(monkeypatch, clock, audio, model, plural):
    calls, progress, uploaded = [], [], {}

    def handler(request):
        calls.append(request)
        url = str(request.url)
        if request.url.host == "dashscope.aliyuncs.com":
            assert request.headers["authorization"] == f"Bearer {KEY}"
        else:
            assert "authorization" not in request.headers
        if url.startswith(API + "/uploads?"):
            assert request.url.params["action"] == "getPolicy"
            assert request.url.params["model"] == model
            return httpx.Response(200, json=policy())
        if url == UPLOAD:
            body = request.content
            assert b"name=\"OSSAccessKeyId\"" in body
            assert b"private-oss-key" in body
            assert b"name=\"Signature\"" in body
            assert b"private-form-signature" in body
            assert b"name=\"x-oss-object-acl\"" in body
            assert b"audio-bytes" in body
            assert body.index(b"name=\"key\"") < body.index(b"name=\"file\"")
            assert b"private-audio.mp3" not in body
            uploaded["done"] = True
            return httpx.Response(200)
        if url == API + "/services/audio/asr/transcription":
            assert uploaded["done"]
            assert request.headers["x-dashscope-async"] == "enable"
            assert request.headers["x-dashscope-ossresourceresolve"] == "enable"
            payload = json.loads(request.content)
            assert payload["model"] == model
            if plural:
                assert list(payload["input"]) == ["file_urls"]
                assert payload["input"]["file_urls"][0].startswith("oss://account-bound/")
            else:
                assert list(payload["input"]) == ["file_url"]
                assert payload["input"]["file_url"].startswith("oss://account-bound/")
                assert payload["parameters"]["enable_words"] is True
            assert payload["parameters"]["channel_id"] == [0]
            return httpx.Response(200, json={"output": {"task_id": "task-123", "task_status": "PENDING"}})
        if url == API + "/tasks/task-123":
            result = {"subtask_status": "SUCCEEDED", "transcription_url": RESULT}
            shape = {"results": [result]} if plural else {"result": result}
            return httpx.Response(200, json={"output": {"task_status": "SUCCEEDED", **shape}})
        if url == RESULT:
            return httpx.Response(200, json=transcript_payload())
        raise AssertionError(url)

    install_client(monkeypatch, handler)
    result = DashScopeFiletransTranscriber(API, KEY, model,
                                          progress_callback=lambda *args: progress.append(args)).transcript(str(audio))
    assert result.full_text == "你好，code review。"
    assert [(s.start, s.end, s.text) for s in result.segments] == [
        (0.75, 1.95, "你好，"), (2.125, 4.9, "code review。"),
    ]
    assert result.raw["timestamp_source"] == "provider"
    assert result.raw["task_id"] == "task-123"
    assert result.language is None  # mixed Chinese/English
    assert progress[-1][:2] == (1, 1)
    saved = json.dumps(result.raw)
    for secret in (KEY, "Signature", "private-policy", "private/path", "private-signature"):
        assert secret not in saved
    assert len(calls) == 5


def test_failed_task_is_not_treated_as_success(monkeypatch, clock, audio):
    def handler(request):
        if request.url.path.endswith("/uploads"):
            return httpx.Response(200, json=policy())
        if request.url.host.startswith("upload."):
            return httpx.Response(200)
        return httpx.Response(200, json={"output": {
            "task_id": "task-failed", "task_status": "FAILED",
            "code": "InvalidApiKey", "message": f"bad key {KEY}; https://host/path?Signature=secret",
        }})

    install_client(monkeypatch, handler)
    with pytest.raises(RuntimeError) as info:
        DashScopeFiletransTranscriber(API, KEY, "qwen3-asr-flash-filetrans").transcript(str(audio))
    assert "InvalidApiKey" in str(info.value)
    assert KEY not in str(info.value)
    assert "Signature=secret" not in str(info.value)


def test_polling_has_a_total_deadline(monkeypatch, clock, audio):
    def handler(request):
        if request.url.path.endswith("/uploads"):
            return httpx.Response(200, json=policy())
        if request.url.host.startswith("upload."):
            return httpx.Response(200)
        return httpx.Response(200, json={"output": {"task_id": "queued", "task_status": "RUNNING"}})

    install_client(monkeypatch, handler)
    with pytest.raises(TimeoutError, match="超时"):
        DashScopeFiletransTranscriber(API, KEY, "qwen3-asr-flash-filetrans", timeout=4).transcript(str(audio))
    assert clock[0] <= 4


def test_upload_failure_does_not_echo_temporary_credentials(monkeypatch, clock, audio):
    def handler(request):
        if request.url.path.endswith("/uploads"):
            return httpx.Response(200, json=policy())
        return httpx.Response(403, text="private-policy private-form-signature private-oss-key")

    install_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="HTTP 403") as info:
        DashScopeFiletransTranscriber(API, KEY, "qwen3-asr-flash-filetrans").transcript(str(audio))
    assert "private-" not in str(info.value)


def test_no_speech_stays_empty_instead_of_inventing_timestamps():
    result = DashScopeFiletransTranscriber(API, KEY, "qwen3-asr-flash-filetrans")._to_result(
        {"transcripts": [{"text": "", "sentences": []}]}, "silent-task"
    )
    assert result.full_text == ""
    assert result.segments == []


def test_text_without_time_alignment_is_rejected():
    with pytest.raises(RuntimeError, match="真实时间轴"):
        DashScopeFiletransTranscriber(API, KEY, "qwen3-asr-flash-filetrans")._to_result(
            {"transcripts": [{"text": "No timestamps", "sentences": []}]}, "broken-result"
        )


@pytest.mark.parametrize("start,end", [(None, 500), (float("nan"), 500), (700, 500), (-5, 500)])
def test_invalid_provider_timestamps_are_rejected(start, end):
    with pytest.raises(RuntimeError, match="时间轴"):
        DashScopeFiletransTranscriber(API, KEY, "qwen3-asr-flash-filetrans")._to_result(
            {"transcripts": [{"sentences": [{"text": "Hello", "begin_time": start, "end_time": end}]}]}, "invalid"
        )


def test_documented_http_oss_result_uses_https():
    assert module.DashScopeFiletransTranscriber._resource_url(
        "http://result.oss-cn-beijing.aliyuncs.com/file?Signature=abc"
    ) == "https://result.oss-cn-beijing.aliyuncs.com/file?Signature=abc"


@pytest.mark.parametrize("url", ["http://outside.example/file", "https://user:secret@host/file", "file:///tmp/a"])
def test_insecure_resource_urls_are_rejected(url):
    with pytest.raises(RuntimeError, match="HTTPS"):
        module.DashScopeFiletransTranscriber._resource_url(url)
