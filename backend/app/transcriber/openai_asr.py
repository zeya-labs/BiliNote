"""OpenAI 兼容音频转写引擎（spec 003）。

支持三种请求风格（端点级 ``api_style``）：

* ``transcriptions``：``POST {base_url}/audio/transcriptions``（OpenAI Whisper / 硅基流动 /
  自建 whisper-server），``response_format`` 支持 ``verbose_json``；不支持时降级 ``json`` → ``text``；
* ``chat``：``POST {base_url}/chat/completions`` + ``messages[].content[].input_audio``
  （小米 MiMo ``mimo-v2.5-asr`` 等）；
* ``dashscope``：DashScope 原生文件转写，临时上传后异步查询并保留真实时间轴。

过大的音频**切割成多段分别转写再合并**（不再整段压缩）；阈值即端点上的 ``chunk_size_mb``。
"""
from __future__ import annotations

import base64
import math
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from typing import Any, List, Optional, Tuple

from app.decorators.timeit import timeit
from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.transcriber.base import Transcriber
from app.utils.audio_helper import (
    cut_audio_regions,
    detect_speech_regions,
    merge_regions_to_limit,
    probe_duration_seconds,
    split_audio,
)
from app.utils.logger import get_logger
from app.utils.openai_client import build_openai_client

logger = get_logger(__name__)

# verbose_json 能拿到分段；部分服务只支持 json/text，按顺序降级
_RESPONSE_FORMATS = ("verbose_json", "json", "text")

# chat 风格（MiMo 等）：base64 后不超过 10MB，原始音频约 7.5MB 以内
_CHAT_MAX_B64_BYTES = 10 * 1024 * 1024
_CHAT_MAX_RAW_BYTES = int(_CHAT_MAX_B64_BYTES * 3 / 4)

# 切割保护：段太小或太多都不合理
_MIN_CHUNK_SECONDS = 10.0
_MAX_CHUNKS = 200

# 按说话切片时的默认参数（可在「音频转写配置」里按端点覆盖）
_DEFAULT_SPEECH_MAX_CHUNKS = 120
_DEFAULT_SILENCE_NOISE_DB = -30.0
_DEFAULT_MIN_SILENCE_SECONDS = 0.5

# 切片转写并发（可在「音频转写配置」里按端点覆盖）：并发只用来填满 max_rpm 允许的
# 请求预算，真正的节流仍由线程安全的 _RateLimiter 负责（spec 006 决策 2/5）。
_DEFAULT_CHUNK_CONCURRENCY = 4
_CHUNK_CONCURRENCY_MAX = 32

# chat 风格（MiMo）默认按静音边界切片。静音检测要完整解码音频，
# 耗时与音频时长成正比；关掉后改走按字节等长切片，省掉这一整轮解码。
_DEFAULT_SILENCE_SPLIT = True


class _RateLimiter:
    """线程安全的匀速限流器，按每分钟请求数计算间隔。"""

    def __init__(self, max_per_minute: float = 30.0):
        self.max_per_minute = max(0.1, float(max_per_minute or 30.0))
        self.interval = 60.0 / self.max_per_minute
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + self.interval
        if delay > 0:
            time.sleep(delay)



def _get(obj: Any, key: str, default: Any = None) -> Any:
    """兼容 SDK 对象与 dict 两种返回。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _is_format_error(exc: Exception) -> bool:
    """只有明确是 response_format 不被支持时才降级，避免把 401/超时也当成格式问题。"""
    msg = str(exc).lower()
    return "response_format" in msg or "verbose_json" in msg


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _shorten(text: str, limit: int = 200) -> str:
    """去掉 HTML 标签、压缩空白并截断，避免把整页 openresty 404 糊到界面上。"""
    text = _HTML_TAG_RE.sub(" ", str(text or ""))
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def _friendly_error(exc: Exception) -> str:
    """把 OpenAI SDK / httpx 的异常翻译成人能看懂的一句话。"""
    status = getattr(exc, "status_code", None)
    raw = str(exc)
    body = ""
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            body = response.text or ""
        except Exception:  # noqa: BLE001
            body = ""
    haystack = f"{raw} {body}".lower()
    looks_html = "<html" in haystack or "<!doctype html" in haystack

    if status == 404 or "404 not found" in haystack:
        return (
            "接口不存在（HTTP 404）：该 Base URL 下没有对应接口。"
            "请确认地址是否要带 /v1、模型名是否正确，以及「接口类型」是否选对"
        )
    if status in (401, 403):
        return f"鉴权失败（HTTP {status}）：API Key 无效或没有访问该模型的权限"
    if status == 400:
        return (
            "请求被拒绝（HTTP 400）：常见原因是模型名写错，或该模型不支持音频转写。"
            f"原始信息：{_shorten(raw)}"
        )
    if status == 429:
        return "请求过于频繁（HTTP 429）：检查配额或稍后重试"
    if isinstance(status, int) and status >= 500:
        return f"服务端错误（HTTP {status}）：{_shorten(raw)}"
    if "timeout" in haystack or "timed out" in haystack:
        return f"请求超时：可适当调大该端点的「超时」设置。原始信息：{_shorten(raw)}"
    if looks_html:
        return f"服务返回了 HTML 而不是 JSON，该地址可能不是 OpenAI 兼容接口：{_shorten(raw)}"
    return _shorten(raw)


class OpenAIASRTranscriber(Transcriber):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 600,
        chunk_size_mb: float = 18,
        name: str = "",
        api_style: str = "transcriptions",
        max_speech_chunks: int = _DEFAULT_SPEECH_MAX_CHUNKS,
        silence_noise_db: float = _DEFAULT_SILENCE_NOISE_DB,
        min_silence_seconds: float = _DEFAULT_MIN_SILENCE_SECONDS,
        max_rpm: Optional[float] = None,
        chunk_concurrency: Optional[int] = None,
        silence_split: Optional[bool] = None,
    ):
        if not str(model or "").strip():
            raise ValueError("OpenAI 兼容 ASR 未配置模型名")
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.timeout = float(timeout or 600)
        self.chunk_size_mb = float(chunk_size_mb or 18)
        self.name = name or base_url or "OpenAI 兼容 ASR"
        # transcriptions=Whisper 风格；chat=chat/completions + input_audio（MiMo 等）
        self.api_style = api_style if api_style in ("transcriptions", "chat", "dashscope") else "transcriptions"
        # 静音切片参数（仅 chat 风格用）
        self.max_speech_chunks = int(max_speech_chunks or _DEFAULT_SPEECH_MAX_CHUNKS)
        self.silence_noise_db = float(silence_noise_db or _DEFAULT_SILENCE_NOISE_DB)
        self.min_silence_seconds = float(min_silence_seconds or _DEFAULT_MIN_SILENCE_SECONDS)
        # 静音分割开关（仅 chat 风格有意义）。None = 未显式指定 → 走默认（开）。
        if silence_split is None:
            self.silence_split = _DEFAULT_SILENCE_SPLIT
        else:
            self.silence_split = bool(silence_split)
        # API 调用限流：优先使用设置页配置；兼容旧的 OPENAI_ASR_MAX_QPS 环境变量。
        configured_rpm = max_rpm
        if configured_rpm is None:
            legacy_qps = os.getenv("OPENAI_ASR_MAX_QPS")
            configured_rpm = (
                float(legacy_qps) * 60
                if legacy_qps
                else float(os.getenv("OPENAI_ASR_MAX_RPM", "30"))
            )
        self.max_rpm = max(1.0, float(configured_rpm or 30.0))
        # 切片转写并发数：显式参数 → 环境变量 → 默认 4；范围 1-32（1 即旧的串行行为）
        if chunk_concurrency is None:
            chunk_concurrency = os.getenv(
                "OPENAI_ASR_CHUNK_CONCURRENCY", _DEFAULT_CHUNK_CONCURRENCY
            )
        try:
            self.chunk_concurrency = max(
                1, min(_CHUNK_CONCURRENCY_MAX, int(chunk_concurrency))
            )
        except (TypeError, ValueError):
            self.chunk_concurrency = _DEFAULT_CHUNK_CONCURRENCY
        self._rate_limiter = _RateLimiter(self.max_rpm)
        logger.info(
            "ASR 请求限流：%.0f 次/分钟，间隔 %.2fs；切片并发 %d",
            self.max_rpm,
            60.0 / self.max_rpm,
            self.chunk_concurrency,
        )


    # ---------------- 主流程 ----------------

    @timeit
    def transcript(self, file_path: str) -> TranscriptResult:
        if self.api_style == "dashscope":
            from app.transcriber.dashscope_asr import DashScopeFiletransTranscriber

            return DashScopeFiletransTranscriber(
                base_url=self.base_url,
                api_key=self.api_key,
                model=self.model,
                timeout=self.timeout,
                max_rpm=self.max_rpm,
                progress_callback=getattr(self, "progress_callback", None),
            ).transcript(file_path)

        client = build_openai_client(
            api_key=self.api_key,
            base_url=self.base_url,
            key_label=f"{self.name} 的 API Key",
            timeout=self.timeout,
            max_retries=0,

        )

        limit = self._chunk_limit_bytes()

        # chat 风格（MiMo 等）不返回时间轴 → 优先按「说话」自动切片，
        # 每片的 start/end 就是真实语音边界，作为该片文本的时间轴。
        #
        # 静音分割开关（设置 → 音频转写配置）关掉时整段跳过：静音检测要完整
        # 解码音频，耗时与时长成正比；跳过之后下面 size 比较那段会走
        # _transcribe_in_chunks，按字节等长切割，时间轴退化为等分切片的偏移。
        if self.api_style == "chat":
            if self.silence_split:
                speech_result = self._transcribe_by_speech(client, file_path, limit)
                if speech_result is not None:
                    return speech_result
            else:
                logger.info(
                    "静音分割已关闭，跳过静音检测，改走按字节等长切片"
                )

        size = os.path.getsize(file_path)
        temp_dir: Optional[str] = None
        try:
            if size <= limit:
                parts = [(0.0, self._safe_transcribe(client, file_path))]
            else:
                parts, temp_dir = self._transcribe_in_chunks(client, file_path, size, limit)
            return self._merge(parts)
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

    def _map_concurrently(self, func, items, label: str = "转写进度"):
        """按 chunk_concurrency 并发执行并保持输入顺序；并发 1 时退化为串行。

        进度用线程安全计数器上报「已完成 done/total 段」；并发只用于填满限流器
        允许的请求预算，真正的节流由 _RateLimiter 负责（spec 006 决策 2/5）。
        """
        total = len(items)
        if total <= 0:
            return []
        workers = max(1, min(self.chunk_concurrency, os.cpu_count() or 4, total))
        state = {"done": 0}
        lock = threading.Lock()
        self._report_progress(0, total, f"{label}：共 {total} 段，并发 {workers}")

        def _wrapped(item):
            result = func(item)
            with lock:
                state["done"] += 1
                done = state["done"]
            logger.info("%s %d/%d 完成", label, done, total)
            self._report_progress(done, total, f"已完成 {done}/{total} 段")
            return result

        if workers <= 1:
            return [_wrapped(item) for item in items]
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="asr") as pool:
            return list(pool.map(_wrapped, items))

    def _transcribe_by_speech(self, client, file_path: str, limit: int):
        """按静音边界切片后逐片转写；没有可用语音区间时返回 None（由调用方退化）。"""
        logger.info("开始静音检测（需要完整解码音频，耗时与音频时长成正比）")
        detect_started = time.perf_counter()

        regions = detect_speech_regions(
            file_path,
            noise_db=self.silence_noise_db,
            min_silence=self.min_silence_seconds,
        )
        logger.info(
            "静音检测完成：%d 个语音区间，耗时 %.1fs",
            len(regions),
            time.perf_counter() - detect_started,
        )

        if len(regions) < 1:
            logger.info("未检测到语音区间，退回按时长均分切片")
            return None

        regions = merge_regions_to_limit(
            regions, max_count=max(1, self.max_speech_chunks)
        )
        logger.info(
            f"静音切片参数：噪声门限 {self.silence_noise_db}dB / 最短静音 "
            f"{self.min_silence_seconds}s / 最多 {self.max_speech_chunks} 段"
        )
        regions = self._split_regions_by_limit(regions, file_path, limit)
        logger.info(f"按说话自动切片：{len(regions)} 段（边界取自静音检测）")

        temp_dir = None
        try:
            cuts, temp_dir = cut_audio_regions(file_path, regions)
            if not cuts:
                return None
            parts = []
            total = len(cuts)
            workers = max(1, min(self.chunk_concurrency, os.cpu_count() or 4, total))
            logger.info(
                "开始逐段转写：%d 段（并发 %d，受 %.0f 次/分钟限流）",
                total,
                workers,
                self.max_rpm,
            )

            def _transcribe_cut(cut):
                start, end, path = cut
                segment_started = time.perf_counter()
                result = self._safe_transcribe(client, path)
                logger.info(
                    "单段转写完成，耗时 %.1fs",
                    time.perf_counter() - segment_started,
                )
                # chat 只回文字 → 用切片时的真实边界当时间轴
                if len(result.segments) == 1:
                    result = TranscriptResult(
                        language=result.language,
                        full_text=result.full_text,
                        segments=[
                            TranscriptSegment(
                                start=start, end=end, text=result.segments[0].text
                            )
                        ],
                        raw=result.raw,
                    )
                return (0.0, result)

            parts = self._map_concurrently(_transcribe_cut, cuts, "转写进度")
            return self._merge(parts) if parts else None
        except Exception:
            raise
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

    def _split_regions_by_limit(self, regions, file_path: str, limit: int):
        """某段语音太长、切出来会超过上传上限时，再按时间等分。"""
        duration = probe_duration_seconds(file_path)
        if duration <= 0:
            return regions
        bytes_per_second = os.path.getsize(file_path) / duration
        if bytes_per_second <= 0:
            return regions

        out = []
        for start, end in regions:
            length = end - start
            # 留 10% 余量，避免压线
            max_seconds = (limit * 0.9) / bytes_per_second
            if length <= max_seconds or max_seconds <= 0:
                out.append((start, end))
                continue
            count = int(math.ceil(length / max_seconds))
            step = length / count
            for i in range(count):
                out.append((start + i * step, min(end, start + (i + 1) * step)))
        return out

    def _transcribe_in_chunks(self, client, file_path: str, size: int, limit: int):
        """返回 ([(offset, result), ...], temp_dir)。"""
        duration = probe_duration_seconds(file_path)
        count = int(math.ceil(size / limit))
        if count > _MAX_CHUNKS:
            raise ValueError(
                f"音频需要切成 {count} 段，超过上限 {_MAX_CHUNKS} 段，请先手动拆分后再上传"
            )
        segment_seconds = max(
            _MIN_CHUNK_SECONDS, (duration / count) if duration > 0 else 600.0
        )
        logger.info(
            f"音频 {size / 1024 / 1024:.1f}MB 超过切割阈值 "
            f"{limit / 1024 / 1024:.1f}MB → 切成约 {count} 段（每段 {segment_seconds:.0f}s）"
        )

        chunks, temp_dir = split_audio(file_path, segment_seconds)
        if len(chunks) <= 1:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise RuntimeError("音频切割失败：请确认 ffmpeg 可用，或该音频格式是否支持切割")

        items = []
        offset = 0.0
        for chunk in chunks:
            items.append((offset, chunk))
            offset += probe_duration_seconds(chunk) or segment_seconds

        try:
            parts = self._map_concurrently(
                lambda item: (item[0], self._safe_transcribe(client, item[1])),
                items,
                "转写进度",
            )
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        return parts, temp_dir

    def _report_progress(self, current: int, total: int, message: str = "") -> None:
        """把逐段进度回调给 NoteGenerator；没有回调时静默忽略。"""
        callback = getattr(self, "progress_callback", None)
        if not callable(callback):
            return
        try:
            callback(current, total, message)
        except Exception as exc:  # noqa: BLE001
            logger.debug("转写进度回调失败：%s", exc)


    def _safe_transcribe(self, client, path: str) -> TranscriptResult:
        try:
            return self._transcribe_one(client, path)
        except Exception as exc:  # noqa: BLE001 - 统一翻成人话再抛给上层
            error = RuntimeError(_friendly_error(exc))
            status = getattr(exc, "status_code", None)
            # 429 与其它 4xx 分开（spec 012）：配额/频率受限是「还没轮到」，应当等
            # 冷却窗口后重试；而鉴权失败、模型不存在重跑整条流程只会重复下载和切片。
            if status == 429:
                error.rate_limited = True  # type: ignore[attr-defined]
            elif status in (400, 401, 403, 404):
                error.non_retryable = True  # type: ignore[attr-defined]
            raise error from exc

    def _transcribe_one(self, client, path: str) -> TranscriptResult:
        if self.api_style == "chat":
            completion = self._create_chat_completion(client, path)
        else:
            completion = self._create_transcription(client, path)
        return self._to_result(completion, source_path=path)

    def _merge(self, parts: List[Tuple[float, TranscriptResult]]) -> TranscriptResult:
        if not parts:
            raise RuntimeError("没有可用的转写结果")
        if len(parts) == 1:
            return parts[0][1]

        segments: List[TranscriptSegment] = []
        texts: List[str] = []
        language = None
        for offset, result in parts:
            if language is None and result.language:
                language = result.language
            if result.full_text:
                texts.append(result.full_text)
            for seg in result.segments:
                segments.append(
                    TranscriptSegment(
                        start=seg.start + offset,
                        end=seg.end + offset,
                        text=seg.text,
                    )
                )
        return TranscriptResult(
            language=language,
            full_text=" ".join(texts).strip(),
            segments=segments,
            raw=None,
        )

    def _chunk_limit_bytes(self) -> int:
        """切割阈值（字节）。chat 风格还要再受 base64 10MB 上限约束。"""
        limit = int(self.chunk_size_mb * 1024 * 1024)
        if self.api_style == "chat":
            limit = min(limit, _CHAT_MAX_RAW_BYTES)
        # 别切得太碎
        return max(limit, 256 * 1024)

    # ---------------- 请求构造 ----------------

    def _create_transcription(self, client, upload_path: str):
        last_error: Optional[Exception] = None
        for response_format in _RESPONSE_FORMATS:
            try:
                self._rate_limiter.wait()

                with open(upload_path, "rb") as f:
                    return client.audio.transcriptions.create(
                        file=(os.path.basename(upload_path), f.read()),
                        model=self.model,
                        response_format=response_format,
                    )
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if not _is_format_error(exc):
                    raise
                logger.warning(
                    f"服务不支持 response_format={response_format}，尝试降级：{exc}"
                )
        raise last_error if last_error else RuntimeError("音频转写失败")

    def _create_chat_completion(self, client, upload_path: str):
        """chat/completions + input_audio（小米 MiMo-V2.5-ASR 等）。"""
        self._rate_limiter.wait()
        with open(upload_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("ascii")
        mime = "audio/mpeg" if upload_path.lower().endswith(".mp3") else "audio/wav"
        return client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": f"data:{mime};base64,{encoded}"},
                        }
                    ],
                }
            ],
        )

    # ---------------- 结果解析 ----------------

    def _to_result(self, transcription: Any, source_path: str) -> TranscriptResult:
        if self.api_style == "chat":
            full_text = self._chat_text(transcription)
            language = None
            raw = self._to_dict(transcription)
        # response_format="text" 时 SDK 直接返回字符串
        elif isinstance(transcription, str):
            full_text = transcription.strip()
            language = None
            raw = None
        else:
            full_text = str(_get(transcription, "text") or "").strip()
            language = _get(transcription, "language")
            raw = self._to_dict(transcription)

        segments = []
        for seg in (_get(transcription, "segments") or []):
            text = str(_get(seg, "text") or "").strip()
            if not text:
                continue
            segments.append(
                TranscriptSegment(
                    start=float(_get(seg, "start") or 0.0),
                    end=float(_get(seg, "end") or 0.0),
                    text=text,
                )
            )

        # 没有分段（json / text / chat）时，造一个覆盖本段的单段，保证下游不空
        if not segments and full_text:
            segments = [
                TranscriptSegment(
                    start=0.0,
                    end=probe_duration_seconds(source_path),
                    text=full_text,
                )
            ]

        if not full_text and segments:
            full_text = " ".join(seg.text for seg in segments).strip()

        return TranscriptResult(
            language=language,
            full_text=full_text,
            segments=segments,
            raw=raw,
        )

    @staticmethod
    def _chat_text(completion: Any) -> str:
        choices = _get(completion, "choices") or []
        if not choices:
            return ""
        message = _get(choices[0], "message")
        content = _get(message, "content")
        if isinstance(content, list):
            return "".join(str(_get(part, "text") or "") for part in content).strip()
        return str(content or "").strip()

    @staticmethod
    def _to_dict(obj: Any) -> Optional[dict]:
        if isinstance(obj, dict):
            return obj
        for method in ("model_dump", "to_dict"):
            fn = getattr(obj, method, None)
            if callable(fn):
                try:
                    return fn()
                except Exception:
                    continue
        return None
