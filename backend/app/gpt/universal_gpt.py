import hashlib
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.gpt.base import GPT
from app.gpt.prompt import MERGE_PROMPT
from app.gpt.prompt_builder import generate_base_prompt
from app.gpt.request_chunker import RequestChunker
from app.gpt.utils import load_llm_json
from app.models.gpt_model import GPTSource
from app.models.transcriber_model import TranscriptSegment
from app.services import runtime_limits
from app.utils.llm_rate_limit import _null_slot, provider_slot
from app.utils.logger import get_logger


def _as_float(value, default: float = 0.0) -> float:
    """把时长/数字收敛成 float：脏数据（None / '12:34' / 空串）一律回退默认值。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    """环境变量转 int：填错时不让模块 import 直接炸掉，回退默认值。"""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


logger = get_logger(__name__)


#: 上游网关（Kouri）实测的请求体上限。超限时返回
#: ``413 request body too large: >10485760 bytes``，且 413 **不在**
#: :meth:`UniversalGPT._is_retryable_error` 的可重试清单里——请求级立即失败。
#:
#: 注意它与 ``OPENAI_MAX_REQUEST_BYTES``（45MB 默认值）**不是一回事**：后者是
#: 应用侧给自己定的预算，从来没和网关对齐过。判定层必须遵守的是本常量。
GATEWAY_MAX_REQUEST_BYTES = _env_int(
    "OPENAI_GATEWAY_MAX_REQUEST_BYTES", 9 * 1024 * 1024
)

#: 判定层每次请求留给图片的字节预算。
#:
#: 历史事故（2026-10-06）：这里原本等于 `GATEWAY_MAX_REQUEST_BYTES - 1MB`（8MB），
#: 于是判定层会切出 ~8MB 的图片批次，而网关在**大请求**上并不可靠。同一个模型、
#: 同一个 key 实测：
#:     2.5MB 稳定成功 | 5.7MB 连接被断 / SSL 错误 | 8.6MB 返回 503
#: 那个 503 的 body 是 ``{"code":"model_not_found"}``，**完全误导** —— 模型确实
#: 存在（``GET /v1/models`` 里在，纯文本与小批量带图都正常），它其实是网关在大
#: 请求上超时/断连后给出的兜底错误码。判定层必须远低于网关的 413 硬上限。
JUDGE_IMAGE_BUDGET_BYTES = _env_int("OPENAI_JUDGE_IMAGE_BUDGET_BYTES", 3 * 1024 * 1024)
JUDGE_IMAGE_BUDGET_BYTES = min(
    JUDGE_IMAGE_BUDGET_BYTES, max(0, GATEWAY_MAX_REQUEST_BYTES - (1024 * 1024))
)


def gateway_max_request_bytes() -> int:
    """当前生效的网关硬上限（字节）。

    模块常量 :data:`GATEWAY_MAX_REQUEST_BYTES` 在导入期求值一次；这个函数每次
    调用都重新读 ``OPENAI_GATEWAY_MAX_REQUEST_BYTES``（默认仍是 9 MiB），所以
    测试或运行时改环境变量后立刻生效。

    用户在「设置 → 通用 → LLM 请求体预算」里配的值只能**低于**它：超限时网关
    直接回 413，而 413 不在可重试清单里。
    """
    return runtime_limits.llm_gateway_ceiling_bytes()


class UniversalGPT(GPT):
    def __init__(
        self,
        client,
        model: str,
        temperature: float = 0.7,
        provider_key: str | None = None,
        max_concurrency: int | None = None,
        max_rpm: float | None = None,
    ):
        self.client = client
        self.model = model
        self.temperature = temperature
        # 供应商级限流（spec 007）。provider_key 为空 = 不限流，保持升级前行为；
        # 这样 _get_gpt 之外的调用点（chat_service / 连通性测试）不受影响。
        self.provider_key = provider_key
        self.max_concurrency = max_concurrency
        self.max_rpm = max_rpm
        self.screenshot = False
        self.link = False
        # 出站请求预算必须服从网关上限，而不是自说自话定一个更大的数。
        #
        # 历史事故：这里原本默认 45MB，而网关（Kouri）只允许 10MB。RequestChunker
        # 严格按 max_request_bytes 切分（request_chunker.py:78/105/127/153 每条放置
        # 路径都只在 <= max_bytes 时才放入），于是它切出来的「合规」分块本身就有
        # 40MB+，判定层与总结层先后撞 413：
        #     413 request body too large: >10485760 bytes
        # 而 413 不在 _is_retryable_error 的可重试清单里，任务三次重试全废。
        #
        # 现在默认对齐 GATEWAY_MAX_REQUEST_BYTES；仍允许用
        # OPENAI_MAX_REQUEST_BYTES 显式覆盖（比如换成自建网关、上限更宽），
        # 但任何时候都不会超过网关上限。
        # 预算来源：环境变量 > 用户设置（通用设置页）> 默认值；再与网关硬上限收敛。
        # 每次构造实例读一次（runtime_limits 内部有 5 秒缓存），所以用户在设置页
        # 改完不需要重启服务，下一个任务就生效。
        configured = runtime_limits.llm_max_request_bytes()
        ceiling = gateway_max_request_bytes()
        self.max_request_bytes = min(configured, ceiling)
        if configured > ceiling:
            logger.warning(
                "LLM 请求体预算 %d 超过网关上限 %d，已收敛到网关上限；"
                "否则请求会被网关以 413 拒绝",
                configured,
                ceiling,
            )
        self.checkpoint_dir = Path(os.getenv("NOTE_OUTPUT_DIR", "note_results"))
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        # 初始化时缓存重试配置，避免每次请求重复读取环境变量 / 数据库
        self._max_retry_attempts = runtime_limits.llm_retry_attempts()
        self._retry_base_backoff = runtime_limits.llm_retry_backoff_seconds()

    def _format_time(self, seconds: float) -> str:
        return str(timedelta(seconds=int(_as_float(seconds))))[2:]

    def _build_segment_text(self, segments: list[TranscriptSegment]) -> str:
        return "\n".join(
            f"{self._format_time(seg.start)} - {seg.text.strip()}" for seg in segments
        )

    def ensure_segments_type(self, segments) -> list[TranscriptSegment]:
        return [
            TranscriptSegment(**seg) if isinstance(seg, dict) else seg
            for seg in segments
        ]

    def create_messages(self, segments: list[TranscriptSegment], **kwargs):

        content_text = generate_base_prompt(
            title=kwargs.get("title"),
            segment_text=self._build_segment_text(segments),
            tags=kwargs.get("tags"),
            _format=kwargs.get("_format"),
            scene=kwargs.get("scene"),
            detail=kwargs.get("detail"),
            extras=kwargs.get("extras"),
            # spec 008 ticket 02：OCR 辅助模式下截图指令换成编号占位符版本
            ocr_assist=bool(kwargs.get("ocr_assist")),
            approved_shots=kwargs.get("approved_shots"),
            # m04445 pivot：OCR 参考内容（q1：全部输出）。
            ocr_reference=kwargs.get("ocr_reference"),
        )

        video_img_urls = kwargs.get("video_img_urls", [])

        content: list[dict] | str
        if video_img_urls:
            # 有截图时走 OpenAI 多模态 content 数组（text + image_url）
            content = [{"type": "text", "text": content_text}]
            for url in video_img_urls:
                content.append(
                    {"type": "image_url", "image_url": {"url": url, "detail": "auto"}}
                )
        else:
            # 纯文本场景退回 string content：DeepSeek deepseek-chat 等非多模态模型
            # 不识别 [{"type":"text",...}] 数组形态，会返回 invalid_request_error
            # （issue #282）。OpenAI 规范本身也允许 content 为 string。
            content = content_text

        messages = [{"role": "user", "content": content}]

        return messages

    # ---------------- 视频理解：按字幕挑截图时间点（spec 002 综合采样） ----------------

    def plan_frame_timestamps(
        self,
        segments: list[TranscriptSegment],
        duration: float = 0.0,
        max_items: int = 24,
    ) -> list[float]:
        """让 LLM 读字幕，挑出「值得截图给多模态模型看」的时间点（秒）。失败返回 []。"""
        segments = self.ensure_segments_type(segments or [])
        if not segments:
            return []
        lines = self._build_plan_lines(segments)
        if not duration or _as_float(duration) <= 0:
            # 时长探测失败时用字幕末尾时间兜底：没有时长就没有「均匀覆盖」，
            # 而均匀覆盖正是模型交白卷时唯一的救生索。
            duration = _as_float(segments[-1].end or segments[-1].start or 0)
        # 用 replace 而不是 str.format：模板里有 JSON 示例的花括号。
        system_prompt = build_frame_plan_prompt(duration, max_items)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "字幕如下：" + chr(10) + lines},
        ]
        response = self.client.chat.completions.create(
            model=self.model, messages=messages, temperature=0.2
        )
        text = response.choices[0].message.content or ""
        picks = parse_frame_plan(text, duration=duration, max_items=max_items)
        if picks:
            return picks
        # 兜底：字幕读不通时模型仍可能交白卷，而空结果在 OCR 辅助模式下会让整个
        # 任务失败。这里退回按时间均匀覆盖，把画面交给多模态模型判断——
        # 漏挑一帧只是少一张图，交白卷是整条任务失败，两者代价不对称。
        fallback = uniform_frame_plan(duration, max_items)
        if fallback:
            logger.warning(
                "画面挑选层返回空结果，改用均匀覆盖兜底：时长=%.1fs 兜底=%d 个时间点",
                _as_float(duration),
                len(fallback),
            )
        return fallback

    def _build_plan_lines(self, segments: list[TranscriptSegment]) -> str:
        """字幕太长时按时间均匀抽稀，控制 prompt 体积（保留整段覆盖）。"""
        segs = list(segments)
        if len(segs) > MAX_PLAN_SEGMENTS:
            step = len(segs) / MAX_PLAN_SEGMENTS
            segs = [segs[int(i * step)] for i in range(MAX_PLAN_SEGMENTS)]
        return chr(10).join(f"[{seg.start:.1f}s] {seg.text.strip()}" for seg in segs)

    # ------------- OCR 辅助：判定哪些候选帧值得编入笔记（spec 008 ticket 02） -------------

    def judge_note_frames(
        self,
        segments: list[TranscriptSegment],
        candidates: list[float],
        duration: float = 0.0,
        max_items: int | None = None,
        image_urls: list | None = None,
    ):
        """判定候选帧里哪些值得编入笔记，返回 JudgementOutcome。

        与 `plan_frame_timestamps` 的区别（这是本功能的核心，别混用）：
          - `plan_frame_timestamps` 决定「抽哪些帧」，服务于 hybrid 采样的配额；
          - 本方法决定「抽出来的帧里哪些真的进笔记」，是插图逻辑的唯一准入闸门。

        解析不出结构化结果时抛 WorthJudgementError，**绝不**返回空清单——否则一次
        网络抖动就会让整篇笔记静默地一张图都没有（spec 008 §5.6b）。模型明确判定
        「都不值得」时返回空 approved，那是合法结果。
        """
        from app.services.ocr_assist import (
            WorthJudgementError,
            build_judge_system_prompt,
            parse_worth_judgement,
        )

        segments = self.ensure_segments_type(segments or [])
        # 上限来自「设置 → 通用 → 单篇插图上限」，用户在任务运行中改了也立刻生效。
        limit = max_items or runtime_limits.note_max_images()
        system_prompt = build_judge_system_prompt(
            candidates, duration=duration, max_items=limit
        )
        user_text = "字幕如下：" + chr(10) + self._build_plan_lines(segments)

        def _build_messages(batch: list) -> list:
            """按一批图片装配判定请求。"""
            content: list | str
            if batch:
                content = [{"type": "text", "text": user_text}]
                for url in batch:
                    content.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": url, "detail": "auto"},
                        }
                    )
            else:
                content = user_text
            return [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ]

        if not image_urls:
            response = self._chat_completion_create(_build_messages([]))
            text = response.choices[0].message.content or ""
            return parse_worth_judgement(text, candidates, max_images=limit)

        # base 必须带一张图：_build_messages([]) 在无图时把 content 装配成字符串，
        # 往里追加图片会因为没有 list 可 append 而静默丢图（体积估算恒为空）。
        batches = self._split_image_batches(image_urls, _build_messages(image_urls[:1]))
        logger.info(
            "判定层分批：图片=%d 批次=%d 预算=%dB",
            len(image_urls),
            len(batches),
            runtime_limits.llm_judge_image_budget_bytes(),
        )
        items: list = []
        for index, batch in enumerate(batches, 1):
            items.extend(
                self._judge_batch_with_split(
                    batch,
                    _build_messages,
                    batch_index=index,
                    batch_total=len(batches),
                )
            )

        if not items:
            # 每一块都解析不出条目时按「判定失败」处理：静默返回空清单会让整篇
            # 笔记一张图都没有，而且完全看不出出过错（spec 008 §5.6b）。
            raise WorthJudgementError(
                "判定结果里找不到帧列表（既不是数组，也没有 frames 字段）"
            )

        # 在**完整候选清单**上只解析一次：这样 max_images 上限、去重、排序和
        # 统计字段都是全局口径，而不是「每块各算一遍再拼起来」。
        merged = json.dumps(items, ensure_ascii=False)
        outcome = parse_worth_judgement(merged, candidates, max_images=limit)
        logger.info(
            "判定层分批完成：图片=%d 批次=%d 条目=%d 最终通过=%d",
            len(image_urls),
            len(batches),
            len(items),
            len(outcome.approved),
        )
        return outcome

    def _judge_batch_with_split(
        self,
        batch: list,
        build_messages,
        batch_index: int = 1,
        batch_total: int = 1,
    ) -> list:
        """判定一批图片；失败就把这一批**对半拆开**再试，拆到单张仍失败才跳过。

        为什么需要它：上游网关在大请求上不可靠（见 ``JUDGE_IMAGE_BUDGET_BYTES``
        的注释）。单纯重试**同一批没有意义**——批次大小是确定的，三次重试必然
        以同样的方式失败，于是整个任务连带三次任务级重试全部作废。改成减半重试，
        才能把请求压回稳定区间并让任务真正跑完。

        单张仍然失败时只记 warning 并跳过这一帧，不拖垮整篇笔记；如果**所有**
        批次都没能返回条目，调用方仍会按 ``WorthJudgementError`` 处理
        （spec 008 §5.6b：绝不静默返回空清单）。
        """
        from app.services.ocr_assist import extract_judgement_items

        try:
            response = self._chat_completion_create(build_messages(batch))
            return extract_judgement_items(response.choices[0].message.content or "")
        except Exception as exc:
            if len(batch) <= 1:
                logger.warning(
                    "判定层单张图片判定失败（第 %d/%d 批，跳过该帧）：%s",
                    batch_index,
                    batch_total,
                    exc,
                )
                return []
            mid = len(batch) // 2
            logger.warning(
                "判定层第 %d/%d 批（%d 张）失败，对半拆成 %d+%d 张重试：%s",
                batch_index,
                batch_total,
                len(batch),
                mid,
                len(batch) - mid,
                exc,
            )
            head = self._judge_batch_with_split(
                batch[:mid], build_messages, batch_index, batch_total
            )
            tail = self._judge_batch_with_split(
                batch[mid:], build_messages, batch_index, batch_total
            )
            return head + tail

    def _split_image_batches(self, image_urls: list, sample_messages: list) -> list:
        """按字节预算把图片切成若干批，保证每批请求不超网关上限。

        以**真实装配出的 messages 字节数**为准，而不是估算单张图的体积和：
        data URL 的 base64 膨胀、JSON 转义都会让实际体积明显大于原始字节数。

        ``sample_messages`` 必须是**已经带了一张图**的样本（调用点传
        ``_build_messages(image_urls[:1])``）。这里先把它还原成「无图骨架 + 一张
        待追加图」的形式再逐张累加，否则拿空 base 估算会得到恒定的空体积，
        分块会静默退化成单批。

        单张图本身就超预算时单独成批——那说明这张图无论怎么切都发不出去，
        让它自己失败比拖着整批一起失败更容易定位。
        """
        import copy

        # 每次分批都重新取：用户可能在任务运行中改了预算，而实例是长生命周期的。
        budget = runtime_limits.llm_judge_image_budget_bytes()
        if budget <= 0 or not image_urls:
            return [list(image_urls)]

        # 骨架 = 样本去掉首图；文本部分（系统提示词 + 字幕）保留，因为它也占预算。
        skeleton = copy.deepcopy(sample_messages)
        user = skeleton[-1]["content"]
        if not isinstance(user, list):
            raise ValueError(
                "_split_image_batches 需要带图的样本 messages（content 必须是 list）"
            )
        if len(user) > 1:
            user.pop()  # 去掉样本里那张占位图

        def _with(batch: list) -> list:
            messages = copy.deepcopy(skeleton)
            target = messages[-1]["content"]
            for url in batch:
                target.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": url, "detail": "auto"},
                    }
                )
            return messages

        batches: list = []
        current: list = []
        for url in image_urls:
            candidate = current + [url]
            if current and self._estimate_messages_bytes(_with(candidate)) > budget:
                batches.append(current)
                current = [url]
            else:
                current = candidate
        if current:
            batches.append(current)
        return batches

    def list_models(self):
        return self.client.models.list()

    def _estimate_messages_bytes(self, messages: list) -> int:
        import json

        return len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))

    def _build_merge_messages(self, partials: list) -> list:
        merge_text = MERGE_PROMPT + "\n\n" + "\n\n---\n\n".join(partials)
        # 合并阶段没有图片，直接用 string content 兼容非多模态模型（issue #282）
        return [{"role": "user", "content": merge_text}]

    def _checkpoint_path(self, checkpoint_key: str) -> Path:
        safe_key = "".join(
            ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in checkpoint_key
        )
        return self.checkpoint_dir / f"{safe_key}.gpt.checkpoint.json"

    def _build_source_signature(self, source: GPTSource) -> str:
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_request_bytes": self.max_request_bytes,
            "title": source.title,
            "tags": source.tags,
            "format": source._format,
            "scene": source.scene,
            "detail": source.detail,
            "extras": source.extras,
            "video_img_urls": source.video_img_urls or [],
            # spec 008 ticket 02：OCR 辅助模式会改变提示词与截图契约，不进签名的话
            # 切换开关会命中旧 checkpoint，产出与当前模式不符的笔记。
            "ocr_assist": bool(source.ocr_assist),
            "approved_shots": [
                {
                    "time": getattr(shot, "time", None),
                    "caption": getattr(shot, "caption", ""),
                }
                for shot in (source.approved_shots or [])
            ],
            "segments": [
                {
                    "start": getattr(seg, "start", None),
                    "end": getattr(seg, "end", None),
                    "text": getattr(seg, "text", ""),
                }
                for seg in source.segment
            ],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _load_checkpoint(
        self, checkpoint_key: str, source_signature: str
    ) -> dict | None:
        path = self._checkpoint_path(checkpoint_key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("source_signature") != source_signature:
                path.unlink(missing_ok=True)
                return None
            return data
        except Exception:
            path.unlink(missing_ok=True)
            return None

    def _save_checkpoint(
        self, checkpoint_key: str, source_signature: str, partials: list, phase: str
    ) -> None:
        path = self._checkpoint_path(checkpoint_key)
        data = {
            "version": 1,
            "source_signature": source_signature,
            "phase": phase,
            "partials": partials,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp_path.replace(path)

    def _clear_checkpoint(self, checkpoint_key: str) -> None:
        self._checkpoint_path(checkpoint_key).unlink(missing_ok=True)

    @staticmethod
    def _is_insufficient_quota_error(exc: Exception) -> bool:
        raw = str(exc)
        return (
            "insufficient_user_quota" in raw
            or "预扣费额度失败" in raw
            or "insufficient quota" in raw.lower()
        )

    @staticmethod
    def _is_retryable_error(exc: Exception) -> bool:
        raw = str(exc).lower()
        retryable_tokens = (
            "error code: 524",
            "bad_response_status_code",
            "timed out",
            "timeout",
            "rate limit",
            "error code: 429",
            "error code: 500",
            "error code: 502",
            "error code: 503",
            "error code: 504",
            "apiconnectionerror",
            "connection error",
            "service unavailable",
        )
        if any(token in raw for token in retryable_tokens):
            return True

        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        return status in {408, 409, 429, 500, 502, 503, 504, 524}

    @staticmethod
    def _is_temperature_unsupported_error(exc: Exception) -> bool:
        """OpenAI o1/o3/gpt-5 系列等新模型不接受自定义 temperature，
        只允许默认值 1，传 0.7 会报 `'temperature' does not support 0.7 ...`。"""
        raw = str(exc).lower()
        return "temperature" in raw and (
            "does not support" in raw
            or "unsupported_value" in raw
            or "only the default" in raw
        )

    def _do_create(self, messages: list):
        """单次调用。如果模型拒绝自定义 temperature，就地去掉该参数再试一次
        （不消耗外层的重试次数预算），仍失败则把异常抛给外层重试逻辑。

        供应商级限流在这里排队（spec 007）：``_do_create`` 是本文件所有 LLM 请求的
        唯一出口，判定/挑选/总结/合并四层都经由它，所以只需在这一处包限流。
        放在重试循环的**内层**：重试之间要重新排队，否则退避重试会插队把别人挤掉。
        """
        slot = (
            provider_slot(self.provider_key, self.max_concurrency, self.max_rpm)
            if self.provider_key
            else _null_slot()
        )
        with slot:
            try:
                return self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                )
            except Exception as exc:
                if self._is_temperature_unsupported_error(exc):
                    print(
                        f"[universal_gpt] 模型 {self.model} 不支持自定义 temperature，改用默认值重试"
                    )
                    return self.client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                    )
                raise

    def _chat_completion_create(self, messages: list):
        last_exc = None
        for attempt in range(self._max_retry_attempts):
            try:
                return self._do_create(messages)
            except Exception as exc:
                last_exc = exc
                if (
                    attempt == self._max_retry_attempts - 1
                    or not self._is_retryable_error(exc)
                ):
                    raise
                sleep_seconds = self._retry_base_backoff * (2**attempt)
                time.sleep(sleep_seconds)

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("chat completion failed without exception")

    def _merge_partials(
        self, partials: list, checkpoint_key: str | None, source_signature: str | None
    ) -> str:
        def build_messages(texts, *_args, **_kwargs):
            return self._build_merge_messages(texts)

        merge_chunker = RequestChunker(
            lambda *_args, **_kwargs: [],
            self.max_request_bytes,
            self._estimate_messages_bytes,
        )

        current_partials = list(partials)
        while len(current_partials) > 1:
            groups = merge_chunker.group_texts_by_budget(
                current_partials, build_messages
            )
            new_partials = []
            for group_idx, group in enumerate(groups):
                messages = build_messages(group)
                try:
                    response = self._chat_completion_create(messages)
                except Exception:
                    if checkpoint_key and source_signature:
                        self._save_checkpoint(
                            checkpoint_key, source_signature, current_partials, "merge"
                        )
                    raise

                new_partials.append(response.choices[0].message.content.strip())

                if checkpoint_key and source_signature:
                    remaining_partials = []
                    for remaining_group in groups[group_idx + 1 :]:
                        remaining_partials.extend(remaining_group)
                    resumable_partials = new_partials + remaining_partials
                    self._save_checkpoint(
                        checkpoint_key, source_signature, resumable_partials, "merge"
                    )

            current_partials = new_partials

        return current_partials[0]

    def polish_markdown(self, title: str, markdown: str) -> str:
        """把合并后的笔记整体润色一次（spec 018 决策 5）。

        超过请求体预算时**直接返回原文**：切块润色会让各块之间的衔接重新断开
        （等于白润），硬发又会撞网关 413。两者都不如让结构拼接结果原样落盘。
        """
        text = markdown or ""
        if not text.strip():
            return text
        messages = [
            {"role": "system", "content": MERGE_NOTES_PROMPT},
            {"role": "user", "content": f"合并标题：{title}\n\n{text}"},
        ]
        try:
            estimated = self._estimate_messages_bytes(messages)
        except Exception:
            estimated = None
        if estimated is not None and estimated > self.max_request_bytes:
            logger.warning(
                "合并笔记润色被跳过：请求体约 %.1f MB 超过预算 %.1f MB（保留结构拼接结果）",
                estimated / 1024 / 1024,
                self.max_request_bytes / 1024 / 1024,
            )
            return text
        response = self._chat_completion_create(messages)
        content = (response.choices[0].message.content or "").strip()
        return content or text

    def summarize(self, source: GPTSource) -> str:
        self.screenshot = source.screenshot
        self.link = source.link
        source.segment = self.ensure_segments_type(source.segment)
        checkpoint_key = source.checkpoint_key
        source_signature = (
            self._build_source_signature(source) if checkpoint_key else None
        )

        def message_builder(segments, image_urls, **kwargs):
            return self.create_messages(segments, video_img_urls=image_urls, **kwargs)

        chunker = RequestChunker(
            message_builder, self.max_request_bytes, self._estimate_messages_bytes
        )

        try:
            chunks = chunker.chunk(
                source.segment,
                source.video_img_urls or [],
                title=source.title,
                tags=source.tags,
                _format=source._format,
                scene=source.scene,
                detail=source.detail,
                extras=source.extras,
                ocr_assist=source.ocr_assist,
                approved_shots=source.approved_shots,
            )
        except ValueError:
            chunks = chunker.chunk(
                source.segment,
                [],
                title=source.title,
                tags=source.tags,
                _format=source._format,
                scene=source.scene,
                detail=source.detail,
                extras=source.extras,
                ocr_assist=source.ocr_assist,
                approved_shots=source.approved_shots,
            )

        partials = []
        if checkpoint_key and source_signature:
            checkpoint = self._load_checkpoint(checkpoint_key, source_signature)
            if checkpoint and isinstance(checkpoint.get("partials"), list):
                partials = checkpoint["partials"]

        if len(partials) > len(chunks):
            partials = []

        for chunk in chunks[len(partials) :]:
            messages = self.create_messages(
                chunk.segments,
                title=source.title,
                tags=source.tags,
                video_img_urls=chunk.image_urls,
                _format=source._format,
                scene=source.scene,
                detail=source.detail,
                extras=source.extras,
                ocr_assist=source.ocr_assist,
                approved_shots=source.approved_shots,
            )
            try:
                response = self._chat_completion_create(messages)
            except Exception:
                if checkpoint_key and source_signature:
                    self._save_checkpoint(
                        checkpoint_key, source_signature, partials, "summarize"
                    )
                raise

            partials.append(response.choices[0].message.content.strip())
            if checkpoint_key and source_signature:
                self._save_checkpoint(
                    checkpoint_key, source_signature, partials, "summarize"
                )

        if len(partials) == 1:
            if checkpoint_key:
                self._clear_checkpoint(checkpoint_key)
            return partials[0]
        merged = self._merge_partials(partials, checkpoint_key, source_signature)
        if checkpoint_key:
            self._clear_checkpoint(checkpoint_key)
        return merged


# ---------------- 综合采样：LLM 选帧的提示词与解析（spec 002） ----------------

MAX_PLAN_SEGMENTS = 600

#: 挑选层的覆盖密度：每这么多秒至少要有一个候选时间点。
#:
#: 旧提示词只列了「优先挑什么」，没有任何召回下限，最后一条还给了一个
#: 「确实没有值得截图的地方就返回 {"frames": []}」的逃生口。结果是模型在
#: 短字幕 / 语音识别质量差的视频上直接交白卷（实测 ``BV147411K7xu?p=198``：
#: 7 段乱码字幕 → 模型原样返回 ``{"frames":[]}``），而生产把空结果当硬异常，
#: 整条任务三次重试全废。现在改成「按时间覆盖」的硬性数量要求：字幕读不通
#: 时也要均匀取样，把画面交给后面的多模态模型判断。
PLAN_COVERAGE_SECONDS = 30

FRAME_PLAN_PROMPT = """你是视频课程内容分析师。下面是视频的转写字幕（方括号内是秒级时间戳）。
请挑出「画面值得截图」的时间点，交给多模态模型看图。

重要前提：字幕来自自动语音识别，可能有错别字、术语识别错误，甚至整段读不通。
这**不是**「没有值得截图的地方」的理由——字幕读不通时，请改为按时间均匀覆盖，
把整段视频都取样到，让后面的多模态模型有机会看到画面。

必须挑（出现任意一条迹象就挑）：
- 出现板书 / 幻灯片 / 讲义 / 图表 / 公式 / 代码 / 表格 / 流程图 / 软件演示 的位置；
- 讲题、例题、习题、题干、选项、答案、解题步骤的位置；
- 开始讲一个新知识点、话题明显切换、章节小结的位置；
- 举例子、做演示、画图、给出结论的位置。

数量是硬性要求，不是建议：
- 至少 {MIN_ITEMS} 项；相邻两个时间点之间不要超过 {MAX_GAP} 秒，
  把整段视频都覆盖到，不要只挑开头几处；
- 宁多勿少：多挑一些交给多模态模型看，远好过漏掉题目和板书；
- 只有整段视频确实没有任何画面内容（纯音频、纯口述）时，
  才允许返回 {"frames": []}。

只输出 JSON，不要解释、不要 markdown 代码块，格式：
{"frames": [{"time": 123.4, "reason": "开始讲解 XX 流程图"}]}

要求：
- time 必须是数字（秒），{DURATION_RANGE}；
- 最多 {MAX_ITEMS} 项，按时间升序；
- 必须给出至少 {MIN_ITEMS} 项，均匀覆盖整段视频。
"""


def plan_min_items(duration: float, max_items: int) -> int:
    """按视频时长算出挑选层必须给出的最少时间点数。

    覆盖密度见 :data:`PLAN_COVERAGE_SECONDS`：时长未知时退回一个保守下限，
    但绝不退回 0——0 就是旧提示词里那个逃生口，正是本函数要堵掉的东西。
    """
    limit = max(1, int(max_items or 1))
    try:
        seconds = float(duration or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds <= 0:
        return min(limit, 3)
    needed = int(-(-seconds // PLAN_COVERAGE_SECONDS))  # 向上取整
    return max(1, min(limit, needed))


def build_frame_plan_prompt(duration: float, max_items: int) -> str:
    """渲染挑选层 system prompt。

    用 ``replace`` 而不是 ``str.format``：模板里有 JSON 示例的花括号。
    """
    seconds = round(float(duration or 0), 1)
    if seconds > 0:
        duration_range = f"且在 0 ~ {seconds} 之间"
    else:
        duration_range = "不要超过字幕里最后一条时间戳"
    return (
        FRAME_PLAN_PROMPT.replace("{DURATION}", str(seconds))
        .replace("{DURATION_RANGE}", duration_range)
        .replace("{MAX_ITEMS}", str(max(1, int(max_items or 1))))
        .replace("{MIN_ITEMS}", str(plan_min_items(duration, max_items)))
        .replace("{MAX_GAP}", str(PLAN_COVERAGE_SECONDS))
    )


def parse_frame_plan(text: str, duration: float = 0.0, max_items: int = 24) -> list:
    """解析 LLM 返回的选帧 JSON；容错处理代码块、纯数组、字段名差异。"""
    import re as _re

    raw = (text or "").strip()
    # 与 OCR 辅助的价值判定共用同一套容错，避免两处实现逐渐分叉。
    data = load_llm_json(raw)

    if isinstance(data, dict):
        items = (
            data.get("frames")
            or data.get("timestamps")
            or data.get("data")
            or data.get("result")
            or []
        )
    elif isinstance(data, list):
        items = data
    else:
        items = []

    times = []
    for item in items:
        if isinstance(item, dict):
            value = item.get("time", item.get("timestamp", item.get("start")))
        else:
            value = item
        try:
            times.append(round(float(value), 3))
        except (TypeError, ValueError):
            continue

    # 兜底：JSON 整体解析失败时，正则捞 "time": 数字
    if not times:
        for value in _re.findall(r'"time"\s*:\s*([0-9]+(?:\.[0-9]+)?)', raw):
            try:
                times.append(round(float(value), 3))
            except ValueError:
                continue

    # 统一过滤越界（正则兜底也要过这一关）
    times = sorted({t for t in times if t >= 0 and (duration <= 0 or t <= duration)})
    if max_items and len(times) > max_items:
        times = times[:max_items]
    return times


def uniform_frame_plan(duration: float, max_items: int) -> list:
    """按 :data:`PLAN_COVERAGE_SECONDS` 的密度均匀取样，作为挑选层的兜底。

    只在模型交白卷时使用（见 :meth:`UniversalGPT.plan_frame_timestamps`）。
    时长未知时返回空清单——不知道视频多长就没法谈覆盖，交给调用方按
    「没有候选帧」处理（那条路径本来就不算失败）。
    """
    try:
        seconds = float(duration or 0)
    except (TypeError, ValueError):
        return []
    if seconds <= 0:
        return []
    limit = max(1, int(max_items or 1))
    count = max(1, min(limit, int(-(-seconds // PLAN_COVERAGE_SECONDS))))
    step = seconds / (count + 1)
    return [round(step * (index + 1), 3) for index in range(count)]
