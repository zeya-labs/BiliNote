"""DashScope native asynchronous file transcription, including real timestamps.

Local audio uses DashScope's account-bound temporary OSS upload policy. The
DashScope API key is sent only to the configured native gateway, never to OSS
or to the signed result URL. No upload policy or signed URL is persisted.
"""
from __future__ import annotations

import math
import mimetypes
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

import httpx

from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.services.proxy_config_manager import ProxyConfigManager
from app.transcriber.base import Transcriber


class DashScopeFiletransTranscriber(Transcriber):
    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: float = 600, max_rpm: float = 30,
                 progress_callback: Callable | None = None):
        parsed = urlsplit(str(base_url).rstrip("/"))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("DashScope 文件转写需要不含凭据或查询参数的 HTTPS 原生 API 地址")
        if not str(api_key or "").strip():
            raise ValueError("DashScope API Key 未配置")
        if not str(model or "").strip():
            raise ValueError("DashScope 文件转写模型未配置")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.timeout = max(1.0, float(timeout))
        self.interval = 60.0 / max(1.0, float(max_rpm))
        self.progress_callback = progress_callback

    def _report(self, current: int, total: int, message: str) -> None:
        if callable(self.progress_callback):
            try:
                self.progress_callback(current, total, message)
            except Exception:
                pass

    @staticmethod
    def _remaining(deadline: float) -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("DashScope 文件转写超时，请稍后查询任务或增加端点超时")
        return value

    def _pause(self, seconds: float, deadline: float) -> None:
        if seconds >= self._remaining(deadline):
            raise TimeoutError("DashScope 文件转写超时，请稍后重试或增加端点超时")
        if seconds > 0:
            time.sleep(seconds)

    def _clean(self, value: Any) -> str:
        text = str(value or "").replace(self.api_key, "[已隐藏]")
        text = re.sub(r"(?:https?|oss)://\S+", "[地址已隐藏]", text)
        text = re.sub(r"sk-[A-Za-z0-9_-]+", "[已隐藏]", text)
        return " ".join(text.split())[:240]

    def _provider_error(self, payload: dict, stage: str, status: int | None = None):
        code = self._clean(payload.get("code") or "UnknownError")
        message = self._clean(payload.get("message") or "请检查模型、地域、额度和接口配置")
        suffix = f"（HTTP {status}）" if status else ""
        raise RuntimeError(f"DashScope {stage}失败{suffix}：{code}，{message}")

    def _request(self, client, method: str, url: str, deadline: float, stage: str, **kwargs):
        try:
            response = client.request(method, url, timeout=self._remaining(deadline), **kwargs)
        except httpx.TimeoutException:
            raise TimeoutError(f"DashScope {stage}请求超时，请检查网络或增加端点超时") from None
        except httpx.RequestError:
            raise RuntimeError(f"DashScope {stage}网络请求失败，请检查网络与代理配置") from None
        if not 200 <= response.status_code < 300:
            # OSS errors may echo signed form credentials. Never expose the body.
            if stage == "上传音频":
                raise RuntimeError(f"DashScope 上传音频失败（HTTP {response.status_code}）")
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            self._provider_error(payload if isinstance(payload, dict) else {}, stage, response.status_code)
        return response

    def _json(self, response, stage: str) -> dict:
        try:
            payload = response.json()
        except ValueError:
            raise RuntimeError(f"DashScope {stage}返回了无法解析的结果") from None
        if not isinstance(payload, dict):
            raise RuntimeError(f"DashScope {stage}返回格式不正确")
        if payload.get("code"):
            self._provider_error(payload, stage)
        return payload

    @staticmethod
    def _resource_url(value: Any) -> str:
        url = urlsplit(str(value or ""))
        # Official examples sometimes contain http:// OSS result URLs. OSS also
        # serves them over HTTPS; do not downgrade the signed URL transport.
        if url.scheme == "http" and (url.hostname or "").endswith(".aliyuncs.com"):
            url = url._replace(scheme="https")
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise RuntimeError("DashScope 返回了无效的 HTTPS 资源地址")
        return urlunsplit(url)

    def _api(self, client, method, path, deadline, next_call, **kwargs):
        self._pause(max(0.0, next_call[0] - time.monotonic()), deadline)
        next_call[0] = time.monotonic() + self.interval
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        headers.update(kwargs.pop("headers", {}))
        response = self._request(client, method, self.base_url + path, deadline,
                                 "接口调用", headers=headers, **kwargs)
        return self._json(response, "接口调用")

    def _upload(self, client, path: Path, deadline, next_call) -> str:
        self._report(0, 1, "正在获取 DashScope 临时上传凭证")
        policy = self._api(client, "GET", "/uploads", deadline, next_call,
                           params={"action": "getPolicy", "model": self.model}).get("data") or {}
        required = ("upload_host", "upload_dir", "policy", "signature", "oss_access_key_id",
                    "x_oss_object_acl", "x_oss_forbid_overwrite")
        if not isinstance(policy, dict) or any(not str(policy.get(k) or "") for k in required):
            raise RuntimeError("DashScope 临时上传凭证不完整")
        limit = float(policy.get("max_file_size_mb") or 2048) * 1024 * 1024
        if path.stat().st_size > limit:
            raise ValueError("音频超过 DashScope 临时上传大小上限")
        basename = uuid4().hex + path.suffix.lower()
        key = str(policy["upload_dir"]).rstrip("/") + "/" + basename
        fields = {
            "OSSAccessKeyId": policy["oss_access_key_id"], "Signature": policy["signature"],
            "policy": policy["policy"], "x-oss-object-acl": policy["x_oss_object_acl"],
            "x-oss-forbid-overwrite": policy["x_oss_forbid_overwrite"],
            "key": key, "success_action_status": "200",
        }
        self._report(0, 1, "正在上传音频到 DashScope 临时存储")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as stream:
            self._request(client, "POST", self._resource_url(policy["upload_host"]), deadline,
                          "上传音频", data=fields, files={"file": (basename, stream, mime)})
        return "oss://" + key

    def _result_location(self, output: dict) -> str:
        result = output.get("result")
        if result is None:
            results = output.get("results") or []
            result = results[0] if results else {}
        if not isinstance(result, dict):
            raise RuntimeError("DashScope 转写成功但未返回结果地址")
        if result.get("subtask_status") not in (None, "SUCCEEDED"):
            self._provider_error(result, "转写子任务")
        return self._resource_url(result.get("transcription_url"))

    def _to_result(self, payload: dict, task_id: str) -> TranscriptResult:
        segments, texts, languages = [], [], []
        transcripts = payload.get("transcripts")
        if not isinstance(transcripts, list):
            raise RuntimeError("DashScope 识别结果缺少 transcripts 字段")
        for transcript in transcripts:
            if not isinstance(transcript, dict):
                continue
            text = str(transcript.get("text") or "").strip()
            sentence_texts = []
            for sentence in transcript.get("sentences") or []:
                text_value = str(sentence.get("text") or "").strip()
                if not text_value:
                    continue
                try:
                    start = float(sentence["begin_time"]) / 1000.0
                    end = float(sentence["end_time"]) / 1000.0
                except (KeyError, ValueError, TypeError):
                    raise RuntimeError("DashScope 识别结果缺少有效句子时间轴") from None
                if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
                    raise RuntimeError("DashScope 返回的句子时间轴无效")
                segments.append(TranscriptSegment(start, end, text_value))
                sentence_texts.append(text_value)
                if sentence.get("language"):
                    languages.append(str(sentence["language"]))
            if text or sentence_texts:
                texts.append(text or "".join(sentence_texts))
        if texts and not segments:
            raise RuntimeError("DashScope 只返回了文字，没有可用于视频定位的真实时间轴")
        segments.sort(key=lambda s: (s.start, s.end))
        return TranscriptResult(
            language=languages[0] if languages and len(set(languages)) == 1 else None,
            full_text="\n".join(texts), segments=segments,
            raw={"provider": "dashscope", "model": self.model, "task_id": task_id,
                 "timestamp_source": "provider", "sentence_count": len(segments)},
        )

    def transcript(self, file_path: str) -> TranscriptResult:
        path = Path(file_path)
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError("待转写的音频文件不存在或为空")
        deadline = time.monotonic() + self.timeout
        next_call = [0.0]
        proxy = ProxyConfigManager().get_proxy_url()
        # No Authorization at client level: upload/result hosts receive none.
        with httpx.Client(proxy=proxy, follow_redirects=False) as client:
            location = self._upload(client, path, deadline, next_call)
            qwen3 = self.model.startswith("qwen3-asr-")
            parameters = {"channel_id": [0]}
            if qwen3:
                parameters.update({"enable_itn": False, "enable_words": True})
            body = {"model": self.model,
                    "input": {"file_url": location} if qwen3 else {"file_urls": [location]},
                    "parameters": parameters}
            self._report(0, 1, "正在提交 DashScope 文件转写任务")
            submitted = self._api(client, "POST", "/services/audio/asr/transcription", deadline, next_call,
                                  headers={"X-DashScope-Async": "enable", "X-DashScope-OssResourceResolve": "enable"},
                                  json=body)
            output = submitted.get("output") or {}
            task_id = str(output.get("task_id") or "")
            if not task_id:
                self._provider_error(submitted, "提交任务")
            while True:
                status = str(output.get("task_status") or "").upper()
                if status == "SUCCEEDED":
                    break
                if status in ("FAILED", "CANCELED", "CANCELLED", "UNKNOWN"):
                    self._provider_error(output, "文件转写")
                self._report(0, 1, "DashScope 正在转写音频" if status == "RUNNING" else "DashScope 转写任务排队中")
                self._pause(1.0, deadline)
                queried = self._api(client, "GET", "/tasks/" + quote(task_id, safe=""), deadline, next_call)
                output = queried.get("output") or {}
            self._report(0, 1, "正在下载带时间轴的识别结果")
            response = self._request(client, "GET", self._result_location(output), deadline, "下载结果")
            result = self._to_result(self._json(response, "下载结果"), task_id)
            self._report(1, 1, f"DashScope 转写完成，共 {len(result.segments)} 个带时间轴的分段")
            return result
