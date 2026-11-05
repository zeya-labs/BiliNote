# app/routers/note.py
import asyncio
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlparse
from zipfile import ZipFile

import httpx
import orjson
from app.db.video_task_dao import get_task_by_video
from app.downloaders.bilibili_expand import (
    build_video_id,
    build_video_url,
    fetch_collection,
    fetch_video_parts,
)
from app.enmus.exception import NoteErrorEnum
from app.enmus.note_enums import DownloadQuality
from app.enmus.task_status_enums import TaskStatus
from app.exceptions.note import NoteError
from app.services import runtime_limits
from app.services.active_model import resolve_active_model
from app.services.note import NoteGenerator
from app.services.source_intake import resolve_bilibili_inputs
from app.services.task_cleanup import TaskInFlightError
from app.services.task_read_model import TaskReadModel  # pyright: ignore[reportMissingImports]
from app.services.task_serial_executor import task_serial_executor
from app.utils.llm_rate_limit import (
    RATE_LIMIT_RESUME_INTERVAL_SECONDS,
    clear_rate_limit,
    note_rate_limit_hit,
    rate_limit_remaining,
    rate_limit_waited_seconds,
    wait_for_rate_limit,
)
from app.utils.logger import get_logger
from app.utils.note_archive import NoteArchiveError
from app.utils.response import ResponseWrapper as R
from app.utils.url_parser import (
    extract_bilibili_p_number,
    extract_video_id,
    normalize_video_url,
    parse_bilibili_collection_url,
)
from app.validators.video_url_validator import is_supported_video_url
from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

# from app.services.downloader import download_raw_audio
# from app.services.whisperer import transcribe_audio

router = APIRouter()

# 统一日志：之前直接复用 app.services.note 的 logger，导致路由层日志都挂在上游模块名下。
logger = get_logger(__name__)


class IntakeResolveRequest(BaseModel):
    mode: str = "single"
    urls: list[str] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)
    contractVersion: int = 1


@router.post("/v1/intake/resolve")
def resolve_intake(data: IntakeResolveRequest):
    if data.mode not in {"single", "batch"}:
        return R.error("mode must be single or batch", code=400)
    if data.mode == "single" and len(data.urls) > 1:
        return R.error("single mode accepts one URL", code=400)
    return R.success(resolve_bilibili_inputs(data.urls))


class VideoRequest(BaseModel):
    video_url: str
    platform: str
    quality: DownloadQuality
    # Legacy request fields are accepted for client compatibility but are never stored or used.
    model_name: str | None = None
    provider_id: str | None = None
    screenshot: bool | None = False
    link: bool | None = False
    # pi-lens-ignore: python-mutable-class-attr
    format: list | None = []
    scene: str | None = None
    detail: str | None = None
    extras: str | None = None
    video_understanding: bool | None = False
    video_interval: int | None = 0
    # pi-lens-ignore: python-mutable-class-attr
    grid_size: list | None = []
    # spec 002：interval=固定间隔抽帧；smart=画面变化+字幕分段高价值抽帧
    sampling_mode: str | None = "interval"
    max_frames: int | None = 60
    # smart 采样限制方式二选一：total=只用 max_frames；per_second=只用 frames_per_second
    frames_per_second: int | None = 2
    frame_limit_mode: str | None = "total"
    # spec 008 ticket 02：按任务覆盖「设置 → OCR 辅助」的启用状态。
    # None（前端不传）= 沿用全局设置；True/False = 这一次任务强制开/关。
    # 设置页管「配没配好」，这里管「这次用不用」，两者刻意分开。
    ocr_assist: bool | None = None
    # 客户端（如浏览器插件）已经在用户浏览器里抓到字幕，直接传给后端复用，
    # 跳过 download_subtitles 和音频转写。形如：
    #   {"language": "zh", "full_text": "...", "segments": [{"start","end","text"}, ...]}
    prefetched_transcript: dict | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_url(cls, data):
        # 稍后再看/收藏夹/带追踪参数的 B 站链接先规范化成标准 /video/BVxxx 形式，
        # 后续校验和 yt-dlp 下载拿到的都是干净链接
        if (
            isinstance(data, dict)
            and data.get("platform") == "bilibili"
            and data.get("video_url")
        ):
            data["video_url"] = normalize_video_url(str(data["video_url"]))
        return data

    @field_validator("video_url")
    def validate_supported_url(cls, v):
        url = str(v)
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https"):
            # 是网络链接，继续用原有平台校验
            if not is_supported_video_url(url):
                raise NoteError(
                    code=NoteErrorEnum.PLATFORM_NOT_SUPPORTED.code,
                    message=NoteErrorEnum.PLATFORM_NOT_SUPPORTED.message,
                )

        return v


NOTE_OUTPUT_DIR = os.getenv("NOTE_OUTPUT_DIR", "note_results")
IMAGE_OUTPUT_DIR = Path(os.getenv("OUT_DIR", "./static/screenshots")).resolve()
IMAGE_BASE_URL = os.getenv("IMAGE_BASE_URL", "/static/screenshots")
MAX_NOTE_ARCHIVE_FILES = 500
MAX_NOTE_ARCHIVE_BYTES = 512 * 1024 * 1024
#: 一次「打包下载」最多收多少篇。原先写死 100，用户有 195 篇时直接报错，
#: 一篇都下不来（而且报错文案被当成「卡住」）。上限只防误点，不该拦住正常批量。
MAX_BATCH_ARCHIVE_TASKS = 500

UPLOAD_DIR = "uploads"

# 任务级自动重试：首次 + N 次重试（spec §7）。
#
# 次数与退避基数现在都是「设置 → 通用」里的可配置项，走 runtime_limits 门面，
# 改完立刻生效（不必重启后端）。下面两个常量只是兜底与文档，实际不再直接使用。
NOTE_MAX_RETRIES = 3
NOTE_RETRY_BACKOFF_BASE_SECONDS = 5.0  # 第 n 次重试等待 base * 3^n 秒：5 / 15 / 45


# 「最大笔记并行任务数」闸门（设置 → 通用设置）。
#
# 为什么不直接改 task_serial_executor 的 max_workers：那是构造期固定的进程级上限
# （TASK_MAX_WORKERS，默认 5），改它必须重启后端；而这个设置是用户随时可调的。
# 所以用一把可重配的进出门闩叠在线程池外面，每次执行前重新读一次数据库。
_PARALLEL_GATE_LOCK = threading.Lock()
_PARALLEL_GATE: Optional["_ParallelGate"] = None


class _ParallelGate:
    """限制同时进入执行的笔记任务数，并支持运行期改额度。

    实现方式是经典的「先过闸、再进池」：调用方 ``with gate:`` 拿到一个名额后
    才把任务提交给线程池。额度变小不会打断已经在跑的任务（它们已经通过闸门了），
    只影响后续进入的任务 —— 这比强制杀掉正在生成的笔记更安全。

    **为什么用 Condition + 计数器，而不是 BoundedSemaphore**（spec 009 ticket 02）：

    早期实现用 ``threading.BoundedSemaphore``，扩容时直接 ``self._semaphore =
    BoundedSemaphore(wanted)`` 换了一个新对象 —— 此时已经阻塞在**旧**信号量上的
    线程永远等不到新的 release，只能等到天荒地老。而 ``BoundedSemaphore`` 又
    没法原地扩容（它把初始值当上界，多 release 一次直接 ``ValueError``）。
    改成 Condition + 整数计数后：

    * 扩容 = 改 ``_limit`` 后 ``notify_all()``，阻塞线程醒来重新判断条件即可；
    * 缩容 = 只改 ``_limit``，不需要「提前占掉空闲名额」这种补偿动作，
      所以 ``release_excess()`` 这个零调用点的补丁也一并没有了 ——
      它本来就是为旧实现的记账漏洞存在的。
    """

    def __init__(self, limit: int) -> None:
        self._condition = threading.Condition()
        self._limit = max(1, limit)
        # 当前已通过闸门、尚未释放的名额数
        self._active = 0

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def active(self) -> int:
        """当前在闸门内的任务数（测试与诊断用）。"""
        with self._condition:
            return self._active

    def acquire(self, timeout: float | None = None) -> bool:
        """拿一个名额；超时返回 False。默认阻塞直到拿到。"""
        with self._condition:
            if timeout is None:
                while self._active >= self._limit:
                    self._condition.wait()
            else:
                deadline = time.monotonic() + timeout
                while self._active >= self._limit:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._condition.wait(remaining)
            self._active += 1
            return True

    def release(self) -> None:
        """归还一个名额并唤醒等待者。重复 release 不会把计数打成负数。"""
        with self._condition:
            if self._active > 0:
                self._active -= 1
            self._condition.notify_all()

    def reconfigure(self, limit: int) -> bool:
        """把额度改成 limit；返回值表示是否真的变了。

        扩容与缩容都只是改一个整数，然后唤醒所有等待者让它们重新判断条件：
        调大时立刻有新名额可用，调小时正在跑的不受影响、跑完自然收敛。
        """
        wanted = max(1, limit)
        with self._condition:
            if wanted == self._limit:
                return False
            self._limit = wanted
            self._condition.notify_all()
            return True

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


def _get_parallel_gate() -> "_ParallelGate":
    """取当前闸门；发现用户改过并行数就热更新。"""
    global _PARALLEL_GATE

    limit = _safe_max_parallel_notes()
    with _PARALLEL_GATE_LOCK:
        if _PARALLEL_GATE is None:
            _PARALLEL_GATE = _ParallelGate(limit)
            logger.info(f"笔记并行闸门已建立：上限 {_PARALLEL_GATE.limit}")
            return _PARALLEL_GATE
        if _PARALLEL_GATE.reconfigure(limit):
            logger.info(f"笔记并行闸门已更新：上限 {_PARALLEL_GATE.limit}")
        return _PARALLEL_GATE


def _safe_max_parallel_notes() -> int:
    """读并行上限；读不出来就退回默认值。

    这个值决定同时跑几个任务，读失败时宁可保守（串行）也不能让异常冒到任务线程里
    —— 那会让一次纯属配置读取的抖动变成一个失败的任务。
    """
    try:
        from app.db.general_settings_dao import get_max_parallel_notes

        return get_max_parallel_notes()
    except Exception as e:
        logger.warning(f"读取最大笔记并行任务数失败，本次退回串行：{e}")
        return 1


def _safe_general_settings() -> dict:
    """读通用设置；读不到就返回空 dict（所有调用方都必须有默认值兜底）。

    磁盘门禁与熔断都依赖这里：设置读不出来时**绝不能**把生成功能锁死
    （fail-open），否则数据库一次抖动就等于整个生成功能不可用。
    """
    try:
        from app.db.general_settings_dao import get_general_settings

        settings = get_general_settings()
        return settings if isinstance(settings, dict) else {}
    except Exception as e:
        logger.warning(f"读取通用设置失败，本次按默认值处理：{e}")
        return {}


# 「一次批量真正跑几个」的分发池（spec 009 ticket 02）。
#
# 为什么需要它：批量入口原来逐条 background_tasks.add_task(run_note_task, ...)，
# 而 Starlette 的 BackgroundTasks.__call__ 是 ``for task in self.tasks: await task()``
# —— 严格串行。200 条任务实际并发度恒为 1，闸门和线程池根本没被争用。
#
# 为什么池子开在 MAX_PARALLEL_NOTES_LIMIT 而不是 TASK_MAX_WORKERS：
# 池子只是「每个在途任务一个等待位」的容器，真正的并发上限由闸门决定（1..8）。
# 池子必须 ≥ 闸门上限，否则闸门还没跑满就被池子卡住，用户设 8 也只会跑出池子那么大。
# 超出池子的条目留在队列里（ThreadPoolExecutor 的队列无界且只是引用，成本可忽略），
# 有任务跑完就自动补位，不需要为 200 条起 200 个线程。
_DISPATCH_POOL: Optional["ThreadPoolExecutor"] = None
_DISPATCH_POOL_LOCK = threading.Lock()


def _dispatch_pool_size() -> int:
    """分发池大小：默认取闸门上限，可用 NOTE_DISPATCH_WORKERS 覆盖。"""
    try:
        override = int(os.getenv("NOTE_DISPATCH_WORKERS", "0") or 0)
        if override > 0:
            return override
    except ValueError:
        # pi-lens-ignore: python-empty-except
        pass
    try:
        from app.db.models.general_settings import MAX_PARALLEL_NOTES_LIMIT

        return max(1, int(MAX_PARALLEL_NOTES_LIMIT))
    except Exception:
        return 8


def _get_dispatch_pool() -> "ThreadPoolExecutor":
    """取（或惰性创建）批量任务分发池。"""
    global _DISPATCH_POOL
    with _DISPATCH_POOL_LOCK:
        if _DISPATCH_POOL is None:
            size = _dispatch_pool_size()
            _DISPATCH_POOL = ThreadPoolExecutor(
                max_workers=size, thread_name_prefix="note-dispatch"
            )
            logger.info(f"批量任务分发池已建立：{size} 个等待位")
        return _DISPATCH_POOL


def _run_note_task_guarded(task_kwargs: dict):
    """执行一条任务，并在运行登记表里登记/注销它（spec 010）。

    为什么是包一层、而不是把 ``run_note_task`` 整个再缩进一层套 try/finally：那个
    函数有一百多行，手工重新缩进的风险远大于收益；而且
    ``tests/006-note-pipeline-performance/test_vector_index_offthread.py`` 用 AST
    按函数名检查 ``run_note_task`` 的函数体，改名会让它失效。

    这里用**模块全局**查 ``run_note_task``（而不是在 import 期捕获），所以测试里
    ``monkeypatch.setattr(note_module, "run_note_task", fake)`` 依然生效。
    """
    from app.services.task_cleanup import mark_task_running, unmark_task_running

    task_id = str(task_kwargs.get("task_id") or "")
    mark_task_running(task_id)
    try:
        return run_note_task(**task_kwargs)
    finally:
        unmark_task_running(task_id)


def _dispatch_note_task(task_kwargs: dict) -> None:
    """把一条批量任务丢进分发池（**不阻塞**，立即返回）。

    与 ``background_tasks.add_task`` 的关键区别：那个是调用方 await 完一条才轮到
    下一条，这里是 200 条一起进池，由闸门控制真正同时在跑的条数。

    单独抽成函数是为了可测：用例可以 patch 掉它，断言「哪些任务被排队、参数是什么」，
    而不必真的起线程跑生成流程。

    **入队即登记「进行中」**（spec 010）：池子只有 8 个等待位，200 条里绝大多数会
    在这里排很久。那段空窗期状态文件还不存在，不登记的话用户一删就把它们删成孤儿。
    """
    task_id = str(task_kwargs.get("task_id") or "")
    from app.services.task_cleanup import mark_task_running, unmark_task_running

    mark_task_running(task_id)
    try:
        _get_dispatch_pool().submit(_run_note_task_guarded, task_kwargs)
    except Exception as exc:
        # 提交失败（池已关闭等）不该让整批请求 500：这一条会缺执行，但其余照跑。
        # 同时必须注销登记，否则这条任务会永远删不掉。
        unmark_task_running(task_id)
        logger.error(f"提交批量任务到分发池失败：{exc}", exc_info=True)


# ---------------------------------------------------------------------------
# 429 停放区（spec 012）
#
# 等满上限的任务不判失败，而是回到 PENDING 并登记在这里；恢复循环每隔
# RATE_LIMIT_RESUME_INTERVAL_SECONDS 探测一次，冷却结束后重新派发。
#
# 为什么不干脆让线程一直睡：worker 只有 TASK_MAX_WORKERS 个，5 条任务各睡一小时
# 就把后面一百多条全堵死了，而配额恢复可能需要几小时。停放是把 worker 让出来，
# 不是放弃这条任务 —— 用户选的就是「保持排队等配额恢复」。
# ---------------------------------------------------------------------------

_PARKED_TASKS: dict = {}
_PARKED_LOCK = threading.Lock()
_RATE_LIMIT_RESUME_STARTED = False


def _park_rate_limited_task(task_id: str, task_kwargs: dict) -> None:
    """把一条 429 等满上限的任务放回 PENDING 并登记到停放区。"""
    try:
        NoteGenerator()._update_status(
            task_id,
            TaskStatus.PENDING,
            message="供应商配额受限，等待自动恢复",
        )
    except Exception as exc:
        logger.warning(f"写入限流等待状态失败 (task_id={task_id}): {exc}")
    with _PARKED_LOCK:
        _PARKED_TASKS[task_id] = task_kwargs
    logger.warning(
        f"任务 {task_id} 已为 429 连续等待 "
        f"{rate_limit_waited_seconds():.0f}s，交还队列等待配额恢复"
    )
    _ensure_rate_limit_resume_loop()


def parked_task_ids() -> list:
    """当前停放在等配额恢复的任务 id（给测试与观测用）。"""
    with _PARKED_LOCK:
        return list(_PARKED_TASKS)


def _resume_parked_tasks() -> int:
    """冷却结束后把停放的任务重新派发；返回派发条数。"""
    if rate_limit_remaining() > 0:
        return 0
    with _PARKED_LOCK:
        if not _PARKED_TASKS:
            return 0
        pending = list(_PARKED_TASKS.values())
        _PARKED_TASKS.clear()
    # 重新探测 = 新的等待预算。不清的话第一次的 _first_hit_at 还在，任务刚派发
    # 出去就又被判定「已等满一小时」，在停放区与队列之间原地打转。
    clear_rate_limit()
    for kwargs in pending:
        _dispatch_note_task(kwargs)
    logger.info(f"限流恢复循环：重新派发 {len(pending)} 条停放任务")
    return len(pending)


def _rate_limit_resume_loop() -> None:
    # 每次重进循环都重读一次：用户把探测间隔调小之后，正在睡的这轮也能在
    # 下一轮生效（常量来自 RATE_LIMIT_RESUME_INTERVAL_SECONDS 环境变量）。
    while True:
        interval = max(1.0, RATE_LIMIT_RESUME_INTERVAL_SECONDS)
        time.sleep(interval)
        try:
            _resume_parked_tasks()
        except Exception as exc:
            logger.error(f"限流恢复循环异常：{exc}", exc_info=True)


def _ensure_rate_limit_resume_loop() -> None:
    """确保恢复循环已启动（幂等）。"""
    global _RATE_LIMIT_RESUME_STARTED
    with _PARKED_LOCK:
        if _RATE_LIMIT_RESUME_STARTED:
            return
        _RATE_LIMIT_RESUME_STARTED = True
    threading.Thread(
        target=_rate_limit_resume_loop,
        name="rate-limit-resume",
        daemon=True,
    ).start()


# 连续失败熔断（spec 009 ticket 04）。
#
# 为什么需要：NOTE_MAX_RETRIES=3 且每次重试**重跑整条流水线**（重下载、重转写、
# 重总结）。cookie 失效、供应商宕机、413 这类系统性故障会命中整批任务，200 条 ×
# 4 遍完整流水线是纯粹的浪费。连续失败到阈值就把后续尚未开始的任务挂起。
#
# 复位语义（用户决策）：用户**重新提交**批量即归零 —— 重新提交本身就是「我处理过
# 了，再试一次」的信号，不需要再加一个恢复按钮。单条提交（/generate_note）也归零，
# 因为它同样是用户主动重试的动作。
class _CircuitBreaker:
    """进程级连续失败计数 + 同批暂停。"""

    def __init__(self, threshold: int = 5) -> None:
        self._lock = threading.Lock()
        self._threshold = max(0, threshold)
        self._consecutive = 0
        self._paused = False

    @property
    def threshold(self) -> int:
        return self._threshold

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._consecutive

    def configure(self, threshold: int) -> bool:
        """更新阈值（0 = 关闭熔断）；返回是否变化。"""
        wanted = max(0, threshold)
        with self._lock:
            if wanted == self._threshold:
                return False
            self._threshold = wanted
            # 关掉熔断要顺手把暂停状态解除，否则用户关了开关任务还是不动。
            if wanted == 0:
                self._paused = False
                self._consecutive = 0
            return True

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    def record_success(self) -> None:
        with self._lock:
            self._consecutive = 0
            self._paused = False

    def record_failure(self) -> bool:
        """记一次最终失败；返回「本次是否触发了暂停」。"""
        with self._lock:
            self._consecutive += 1
            if self._threshold and self._consecutive >= self._threshold:
                if not self._paused:
                    self._paused = True
                    return True
            return False

    def reset(self) -> None:
        """用户重新提交时归零。"""
        with self._lock:
            self._consecutive = 0
            self._paused = False


_CIRCUIT_BREAKER = _CircuitBreaker()


def _get_circuit_breaker() -> "_CircuitBreaker":
    """取熔断器，并按通用设置热更新阈值（0 = 关闭）。"""
    settings = _safe_general_settings()
    try:
        threshold = int(settings.get("circuit_breaker_threshold", 5) or 0)
    except (TypeError, ValueError):
        threshold = 5
    if _CIRCUIT_BREAKER.configure(threshold):
        logger.info(f"连续失败熔断阈值已更新：{threshold}（0 = 关闭）")
    return _CIRCUIT_BREAKER


def _human_bytes(num: int) -> str:
    """把字节数转成给用户看的大小（磁盘提示里用）。"""
    value = float(max(0, num))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value:.0f} B"
        value /= 1024
    return f"{value:.1f} TB"


def _check_disk_space() -> str | None:
    """磁盘门禁：剩余空间不足时返回给用户看的提示，充足/读不到设置时返回 None。

    为什么 fail-open：``min_free_disk_bytes`` 读不出来（数据库抖动、老库缺列）时
    返回 None 放行。设置是「保护」不是「开关」，读不到设置就把生成功能锁死，比
    磁盘写满更糟 —— 用户会以为程序坏了。这与 ``general_settings_dao`` 既有的
    「读失败回落默认值」方向一致。
    """
    settings = _safe_general_settings()
    try:
        min_free = int(settings.get("min_free_disk_bytes", 0) or 0)
    except (TypeError, ValueError):
        min_free = 0
    if min_free <= 0:
        return None

    try:
        from app.utils.path_helper import get_data_dir

        target = get_data_dir()
        os.makedirs(target, exist_ok=True)
        usage = shutil.disk_usage(target)
    except Exception as exc:
        logger.warning(f"读取磁盘剩余空间失败，本次不拦截：{exc}")
        return None

    if usage.free >= min_free:
        return None
    logger.warning(
        f"拒绝生成提交：磁盘剩余 {_human_bytes(usage.free)}，低于阈值 {_human_bytes(min_free)}"
    )
    return (
        f"磁盘剩余空间不足：当前剩余 {_human_bytes(usage.free)}，"
        f"低于设定的 {_human_bytes(min_free)}。请先清理缓存或调低「通用设置」里的"
        f"磁盘剩余空间下限。"
    )


# 进行中状态：这些状态下不允许对同一视频重复提交（否则并发互踩，见 spec 001 §12）
_IN_FLIGHT_STATUSES = {
    TaskStatus.PENDING.value,
    TaskStatus.PARSING.value,
    TaskStatus.DOWNLOADING.value,
    TaskStatus.TRANSCRIBING.value,
    TaskStatus.SUMMARIZING.value,
    TaskStatus.FORMATTING.value,
    TaskStatus.SAVING.value,
}


def _get_task_record(video_id: str, platform: str):
    """返回 (task_id, status)；无记录/无状态文件时 status 为 None。"""
    if not video_id:
        return None, None
    try:
        task_id = get_task_by_video(video_id, platform)
    except Exception:
        logger.warning(f"查询去重记录失败 video_id={video_id}", exc_info=True)
        return None, None
    if task_id is None:
        return None, None
    if not _valid_task_id(str(task_id)):
        logger.warning("任务记录包含无效 task_id，跳过文件读取")
        return task_id, None
    status_path = os.path.join(NOTE_OUTPUT_DIR, f"{task_id}.status.json")
    if not os.path.exists(status_path):
        return task_id, None
    try:
        with open(status_path, encoding="utf-8") as f:
            return task_id, json.load(f).get("status")
    except Exception:
        return task_id, None


def _is_generated_task(video_id: str, platform: str) -> bool:
    """已成功生成过（spec 001 §6）。"""
    _, status = _get_task_record(video_id, platform)
    return status == TaskStatus.SUCCESS.value


def _is_task_in_flight(video_id: str, platform: str) -> bool:
    """是否有同一视频的任务正在跑（spec 001 §12：进行中也跳过，避免并发互踩）。"""
    _, status = _get_task_record(video_id, platform)
    return status in _IN_FLIGHT_STATUSES


def _resolve_single_video_id(video_id: str, video_url: str, platform: str) -> str:
    """单集提交的规范 video_id（spec 018 决策 8）。

    多P 视频必须与 `/expand` 用同一套 id（`BV…_pN`），否则两个缺陷会一直在：

    * `already_generated` 标记按 `BV…_pN` 查库，而单集写进去的是裸 `BV…` ——
      展开面板永远显示「未生成」；
    * 同一个视频不同分P 共用一把在途锁：P1 在跑时 P2 会被判「该视频正在生成中」。

    分P 数探测失败（网络抖动 / 需要 Cookie）时回退成裸 `BV…`：宁可让去重不精确，
    也不能因为一次探测失败就拒绝生成。
    """
    if platform != "bilibili" or not str(video_id or "").startswith("BV"):
        return video_id
    try:
        parts = fetch_video_parts(video_id)
    except Exception as exc:
        logger.warning(f"探测分P失败，回退裸 video_id（bvid={video_id}）：{exc}")
        return video_id
    if not parts:
        return video_id
    p = extract_bilibili_p_number(str(video_url or "")) or 1
    return build_video_id(video_id, p, len(parts))


def _expand_video_entry(
    bvid: str, title: str = "", duration: int = 0, cover: str = ""
) -> dict:
    """把一个视频展开成 video entry（含分P、去重标记、单条错误隔离）。"""
    try:
        parts = fetch_video_parts(bvid)
    except Exception as e:
        logger.warning(f"获取分P失败 bvid={bvid}: {e}")
        parts = []

    if not parts:
        return {
            "bvid": bvid,
            "title": title,
            "duration": duration,
            "cover": cover,
            "parts": [],
            "error": "未获取到分P信息（视频可能已删除或需要 Cookie）",
        }

    parts_count = len(parts)
    part_dicts = []
    for part in parts:
        video_id = build_video_id(bvid, part.p, parts_count)
        part_dicts.append(
            {
                "p": part.p,
                "title": part.title,
                "duration": part.duration,
                "video_id": video_id,
                "url": build_video_url(bvid, part.p, parts_count),
                "already_generated": _is_generated_task(video_id, "bilibili"),
            }
        )

    return {
        "bvid": bvid,
        "title": title or (parts[0].title if parts else ""),
        "duration": duration or sum(p.duration for p in parts),
        "cover": cover,
        "parts": part_dicts,
        "error": None,
    }


def _run_with_parallel_gate(execute_fn):
    """把任务提交给执行池，并在「最大笔记并行任务数」闸门下排队。

    这个闸门与 ``task_serial_executor`` 的线程池是两件事：线程池的
    ``max_workers``（``TASK_MAX_WORKERS``，默认 5）是**进程级**的硬上限，启动后
    不可改；而用户在「设置 → 通用设置」里改的并行数必须**立即生效**，不能要求
    重启后端。所以这里每次执行前都重新读一次设置，用一把进出门闩
    （:class:`_ParallelGate`）把在途任务数限制在最新的设定值上。

    注意闸门要加在**线程池外面**：先拿到门闩再提交，否则任务已经在线程池里
    占着 worker 空转等门闩，会把池子撑满，反而让别的任务连排队的机会都没有。
    """
    gate = _get_parallel_gate()
    with gate:
        return task_serial_executor.run(execute_fn)


#: 429 退避等满上限后交还队列的哨兵（spec 012）。
#
# 为什么用哨兵而不是 None：None 的语义是「这条任务失败了」——会被计入熔断、会被
# 标成 FAILED。而 429 等满上限只是「配额还没恢复」，任务应当回到 PENDING 排队。
RATE_LIMIT_PARKED = "__rate_limit_parked__"


def _execute_note_with_retry(task_id: str, generator, execute_fn):
    """执行笔记任务；失败自动重试 NOTE_MAX_RETRIES 次（首次 + N 次重试）。

    429 走**独立的一条路**（spec 012）：不消耗 NOTE_MAX_RETRIES 预算、不计熔断、
    不判失败。撞上 429 就等全局冷却窗口（默认 5 分钟）再试；连续等到上限
    （默认 1 小时）仍是 429 就返回 :data:`RATE_LIMIT_PARKED`，把 worker 线程让出来，
    由调用方交还队列 —— 配额恢复可能要几小时，而 worker 只有 TASK_MAX_WORKERS 个。
    """
    # 每次进入重试循环都重读：用户在设置页改完重试次数/退避，下一条任务即生效。
    max_retries = runtime_limits.note_max_retries()
    backoff_base = runtime_limits.note_retry_backoff_seconds()
    attempt = 0
    rate_limit_waits = 0
    while True:
        note = _run_with_parallel_gate(execute_fn)
        if note and getattr(note, "markdown", None):
            # 成功即清冷却窗口：429 已经过去，后面的任务不该再陪等。
            clear_rate_limit()
            return note

        if getattr(generator, "last_error_rate_limited", False) is True:
            if note_rate_limit_hit():
                return RATE_LIMIT_PARKED
            rate_limit_waits += 1
            waited = wait_for_rate_limit()
            logger.warning(
                f"任务 {task_id} 遇到 429，等待 {waited:.0f}s 后重试"
                f"（第 {rate_limit_waits} 次退避，不消耗重试预算）"
            )
            try:
                generator._update_status(
                    task_id,
                    TaskStatus.PENDING,
                    message="供应商限流，等待配额恢复",
                )
            except Exception:
                # pi-lens-ignore: python-empty-except
                pass
            continue

        if getattr(generator, "last_error_non_retryable", False) is True:
            logger.error(f"任务 {task_id} 遇到不可重试错误，停止自动重试")
            return None

        if attempt >= max_retries:
            logger.error(f"任务 {task_id} 重试 {max_retries} 次后仍失败")
            return None
        retry_no = attempt + 1
        # 指数退避：5 / 15 / 45 / 45 …（基数可配，上限按三次方封顶）。
        delay = backoff_base * (3 ** min(attempt, 2))
        attempt += 1
        logger.warning(
            f"任务 {task_id} 失败，第 {retry_no}/{max_retries} 次重试（{delay}s 后）"
        )
        try:
            generator._update_status(
                task_id,
                TaskStatus.PENDING,
                message=f"重试中 ({retry_no}/{max_retries})",
            )
        except Exception:
            # pi-lens-ignore: python-empty-except
            pass
        if delay > 0:
            time.sleep(delay)


def save_note_to_file(task_id: str, note):
    os.makedirs(NOTE_OUTPUT_DIR, exist_ok=True)
    with open(
        os.path.join(NOTE_OUTPUT_DIR, f"{task_id}.json"), "w", encoding="utf-8"
    ) as f:
        json.dump(asdict(note), f, ensure_ascii=False, indent=2)


def _persist_prefetched_transcript(task_id: str, transcript: dict) -> None:
    """把客户端预取的字幕写到 NoteGenerator 期望的转写缓存文件里。

    NoteGenerator.generate 会优先读 <task_id>_transcript.json，命中即跳过 download_subtitles
    与音频转写流程。要求字段：language(可空)/full_text/segments[{start,end,text}]
    """
    segments = transcript.get("segments") or []
    cleaned_segments = []
    for s in segments:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        cleaned_segments.append(
            {
                "start": float(s.get("start", 0)),
                "end": float(s.get("end", 0)),
                "text": text,
            }
        )
    if not cleaned_segments:
        raise ValueError("prefetched_transcript 没有可用的 segments")

    full_text = transcript.get("full_text") or " ".join(
        s["text"] for s in cleaned_segments
    )
    payload = {
        "language": transcript.get("language") or "zh",
        "full_text": full_text,
        "segments": cleaned_segments,
    }

    os.makedirs(NOTE_OUTPUT_DIR, exist_ok=True)
    target = os.path.join(NOTE_OUTPUT_DIR, f"{task_id}_transcript.json")
    with open(target, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logger.info(f"已写入客户端预取字幕缓存: {target} ({len(cleaned_segments)} 段)")


def _safe_file_component(text: str, fallback: str = "note") -> str:
    """把任意标题转成可用于文件名 / zip 目录名的片段。

    保留中文：用户的视频标题基本都是中文，早期这里用
    ``[^A-Za-z0-9._-]`` 过滤会把标题整个抹平，最后所有笔记都叫 "note"。
    只替换真正在 Windows / zip 里危险的字符，并把连续空白压成一个空格。

    zip 条目名里的 ``/`` 是路径分隔符，必须换掉，否则标题里带斜杠会凭空
    造出一层目录。
    """
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(text or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        return fallback
    # 文件名长度上限（Windows 260 路径限制下，留出目录与 .zip 的余量）
    return cleaned[:80].strip(" .") or fallback


def _result_title(result: dict) -> str:
    """从任务结果 JSON 里取视频标题。

    **顶层没有 title**：结果 JSON 只有 markdown / transcript / audio_meta
    三个键，标题在 ``audio_meta.title``。早期代码读 ``result.get("title")``
    永远拿不到值，于是所有导出都退化成 "note"。
    """
    audio_meta = result.get("audio_meta")
    if isinstance(audio_meta, dict):
        title = audio_meta.get("title")
    else:
        title = None
    return str(title or "").strip()


def _group_display_name_for(task_id: str) -> str:
    """取任务所属组名；未分组或查不到时返回空串。"""
    try:
        from app.db.note_group_dao import get_task_group, list_groups

        group_id = get_task_group(task_id)
        if not group_id:
            return ""
        for group in list_groups():
            if str(group.get("id")) == str(group_id):
                return str(group.get("name") or "")
        return ""
    except Exception as exc:
        logger.debug("查询任务分组名失败（%s）：%s", task_id, exc)
        return ""


class BatchGroupError(Exception):
    """批量建组失败 —— 调用方要把它转成明确的错误响应。

    为什么单独定义而不是直接用 ``NoteGroupError``：路由层需要区分「建组失败」
    与「其它异常」，而 ``NoteGroupError`` 是 DAO 层的类型，路由层引用它就得
    把数据访问层的东西泄露到 HTTP 层。
    """


def _resolve_batch_group(group_name) -> str:
    """批量生成时整批建一个组，返回组 id。

    **失败不再静默退化**（spec 009 决策 4–5）：早期实现里建组失败只 warning 一声
    就 ``return ""``，于是用户以为任务进了新组，实际全落到「未分组」—— 而且他
    根本不知道，因为响应和界面都看不出差别。现在改成抛 :class:`BatchGroupError`，
    由 ``generate_notes`` 转成明确错误，用户至少知道「这批没建成组」。

    空名仍然返回空串：前端已保证必填（ticket 01 改动点 4），后端这一层只是
    兜底 —— 空名是个合法语义（用户就是要未分组），不是失败。
    """
    name = str(group_name or "").strip()
    if not name:
        return ""
    # NoteGroupError 要在 except 子句里被引用，所以必须导在 try 外面：
    # 写成「try 里 import、except 里引用」的话，一旦 import 本身失败，
    # 求值 except 子句时这个名字还不存在，会抛 NameError 把真实异常吃掉。
    from app.db.note_group_dao import NoteGroupError, create_group

    try:
        group = create_group(name)
        logger.info("批量生成自动建组：%s（%s）", name, group.get("id"))
        return str(group.get("id") or "")
    except NoteGroupError as exc:
        # 组名不合法、数量超上限（MAX_GROUPS）都会走到这里。
        logger.warning("批量生成建组失败（%s）", exc)
        raise BatchGroupError(str(exc)) from exc
    except Exception as exc:
        logger.error("批量生成建组异常（%s）", exc, exc_info=True)
        raise BatchGroupError(f"新建分组失败：{exc}") from exc


def _enqueue_task(
    task_id: str,
    video_id: str,
    platform: str,
    group_id=None,
    payload: dict | None = None,
) -> None:
    """入队时就把任务行写进数据库（含分组归属与完整生成参数）。

    为什么要在这里写、而不是等生成完由 _save_metadata 写：

    * 任务列表整个迁到了后端（用户决策 m04951），前端不再有本地缓存兜底。
      如果任务行等到生成成功才落库，用户点完「生成」后要盯着空白等十几分钟
      才看见任务，批量生成时尤其难受。
    * 分组同理：批量任务必须**立刻**出现在新建的组里，否则用户会以为自己
      建组失败又去点一次。

    ``_save_metadata`` 那边相应改成「已存在就只补归属」，避免 task_id 唯一
    约束冲突。
    """
    payload_json = None
    if payload is not None:
        try:
            payload_json = json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            # 序列化失败只丢恢复能力，不影响这次生成。
            logger.warning(f"生成参数序列化失败 (task_id={task_id}): {exc}")

    try:
        from app.db.video_task_dao import insert_video_task

        insert_video_task(
            video_id=video_id,
            platform=platform,
            task_id=task_id,
            group_id=group_id,
            payload_json=payload_json,
        )
    except Exception as e:
        # 落库失败不该拦住生成：笔记本身是产物，任务行只是索引。
        logger.warning(f"写入任务记录失败 (task_id={task_id}): {e}")


def _build_task_kwargs(
    task_id: str,
    video_url: str,
    platform: str,
    data,
    group_id: str | None,
) -> dict:
    """把提交参数整理成 ``run_note_task`` 的关键字参数。

    为什么要单独抽出来（spec 009 决策 8–10）：这些参数现在有两个用途 ——
    提交时丢进分发池执行，以及**序列化进 ``payload_json`` 供重启恢复**。
    如果两处各写一份，将来加参数时必然漏掉一处，而漏掉的表现是「重启后任务
    按默认参数重跑」，很难在测试里发现。所以只在这里构造一次。
    """
    return {
        "task_id": task_id,
        "video_url": video_url,
        "platform": platform,
        "quality": data.quality,
        "link": bool(getattr(data, "link", False)),
        "screenshot": bool(getattr(data, "screenshot", False)),
        "_format": getattr(data, "format", None) or [],
        "scene": getattr(data, "scene", None),
        "detail": getattr(data, "detail", None),
        "extras": getattr(data, "extras", None),
        "video_understanding": bool(getattr(data, "video_understanding", False)),
        "video_interval": getattr(data, "video_interval", 0) or 0,
        "grid_size": getattr(data, "grid_size", None) or [],
        "sampling_mode": getattr(data, "sampling_mode", "interval") or "interval",
        "max_frames": getattr(data, "max_frames", 60) or 60,
        "frames_per_second": getattr(data, "frames_per_second", 2) or 2,
        "frame_limit_mode": getattr(data, "frame_limit_mode", "total") or "total",
        "ocr_assist": getattr(data, "ocr_assist", None),
        "group_id": group_id,
        # spec 018：合并任务的载荷。普通单集/批量任务这里是 None/False，
        # 合并提交时由 _submit_merged_batch 填真值。放在这里是为了让
        # 「提交参数 / 恢复载荷 / 重试载荷」三处始终是同一份键集合 ——
        # tests/015-.../test_preset_contract.py 就在钉这条契约。
        "merge_items": getattr(data, "merge_items", None),
        "merge_title": getattr(data, "merge_title", None),
        "merge_polish": bool(getattr(data, "merge_polish", False)),
    }


def _payload_for_storage(task_kwargs: dict) -> dict:
    """把关键字参数转成可 JSON 序列化的形式（枚举取 value、去掉 task_id）。"""
    stored = {}
    for key, value in task_kwargs.items():
        if key in {"task_id", "model_name", "provider_id"}:
            continue
        stored[key] = getattr(value, "value", value)
    stored.pop("provider_id", None)
    return stored


#: 合并任务的合成 video_id 前缀（spec 018 决策 3）。合并产物不对应单一视频，
#: 但任务行需要 video_id 才能落库，所以给一个可辨识的合成 id。
MERGED_VIDEO_ID_PREFIX = "merged_"


def _merged_item_entry(item: "BatchItem", platform: str) -> dict:
    """把一条勾选项整理成合并子任务的输入（spec 018 §5.2）。"""
    video_id = item.video_id or item.bvid
    if item.url:
        url = item.url
    elif item.video_id and "_p" in item.video_id:
        url = f"https://www.bilibili.com/video/{item.bvid}?p={item.p}"
    else:
        url = f"https://www.bilibili.com/video/{item.bvid}"
    return {
        "url": url,
        "video_id": video_id,
        "bvid": item.bvid,
        "p": item.p,
        "title": (item.title or "").strip(),
    }


def _submit_merged_batch(data: "BatchGenerateRequest"):
    """合并任务提交：不建组，只建**一条**任务（spec 018 决策 3/6）。

    与普通批量的两点差别：

    * **不做「已生成」跳过**：合并要的是每个视频的真实内容，跳过会让合并笔记缺章节；
    * **不做建组**：用户勾了合并就是「一篇笔记」，再建组等于给他一个空壳组。
    """
    items = list(data.items or [])
    if not items:
        return R.error(msg="请至少选择一个视频", code=400)

    # 在途判定仍然要做：同一视频并发跑会互踩 data/data/<id>.mp4 与抽帧目录。
    in_flight: list[str] = []
    entries: list[dict] = []
    for item in items:
        entry = _merged_item_entry(item, data.platform)
        if _is_task_in_flight(entry["video_id"], data.platform):
            in_flight.append(entry["video_id"])
        entries.append(entry)
    if in_flight:
        preview = "、".join(in_flight[:3])
        return R.error(
            msg=f"以下视频正在生成中，请稍后再试：{preview}",
            code=300103,
            data={"reason": "task_in_progress", "video_ids": in_flight},
        )

    if not (data.model_name and data.provider_id):
        try:
            resolve_active_model()
        except RuntimeError as exc:
            return R.error(msg=str(exc), code=400)

    title = (data.merge_title or data.group_name or "").strip() or "合并笔记"
    task_id = str(uuid.uuid4())
    merged_video_id = f"{MERGED_VIDEO_ID_PREFIX}{task_id[:8]}"
    NoteGenerator()._update_status(
        task_id, TaskStatus.PENDING, message="合并任务排队中"
    )

    # video_url 取第一条：合并任务本身没有单一链接，但恢复逻辑要求 payload 里有它。
    task_kwargs = _build_task_kwargs(
        task_id, entries[0]["url"], data.platform, data, None
    )
    # merge_title / merge_polish 已经由 _build_task_kwargs 带出；这里补上 items
    # 与解析后的标题（空标题要回落到「合并笔记」，不能把空串写进载荷）。
    task_kwargs["merge_items"] = entries
    task_kwargs["merge_title"] = title
    task_kwargs["merge_polish"] = bool(data.merge_polish)
    _enqueue_task(
        task_id,
        merged_video_id,
        data.platform,
        None,
        _payload_for_storage(task_kwargs),
    )
    _dispatch_note_task(task_kwargs)
    logger.info(
        f"合并任务已提交 task_id={task_id} items={len(entries)} title={title} "
        f"polish={bool(data.merge_polish)}"
    )
    return R.success(
        {
            "results": [
                {"video_id": merged_video_id, "task_id": task_id, "status": "queued"}
            ],
            "group_id": None,
            "merged": True,
        }
    )


def _enforce_cache_limit_in_background() -> None:
    """任务到达终态后，在后台线程里检查一次容量上限（spec 007 §12.1 决策 13/14）。

    为什么不等 ``media_cleanup_loop`` 那一小时一轮：批量导入几十条长视频时，一小时
    足够再写出几十 GB，等下一轮才清就已经把磁盘挤爆了 —— 而这个循环的间隔本身是
    为「到期清理」设计的，不该被容量维度拖短。

    为什么要另起线程而不是直接调：``enforce_cache_limit()`` 要遍历受管目录并逐项
    stat（几千个文件时可能几秒），直接调会把任务工作线程占住 —— 而
    ``TASK_MAX_WORKERS`` 是有限资源，收尾慢就等于整批任务排队变慢。

    整个调用包 try/except：清理是尽力而为的后台工作，绝不能让它把任务的终态搞坏
    （用户看到的应该是「笔记生成成功」，而不是「清理失败」）。
    """
    try:
        from app.services.media_resource_service import enforce_cache_limit

        threading.Thread(
            target=enforce_cache_limit,
            name="cache-limit-enforce",
            daemon=True,
        ).start()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"启动容量边界清理失败（不影响任务终态）：{exc}")


def run_note_task(
    task_id: str,
    video_url: str,
    platform: str,
    quality: DownloadQuality,
    link: bool = False,
    screenshot: bool = False,
    model_name: str | None = None,
    provider_id: str | None = None,
    _format: list | None = None,
    scene: str | None = None,
    detail: str | None = None,
    extras: str | None = None,
    video_understanding: bool = False,
    video_interval=0,
    grid_size=None,
    sampling_mode="interval",
    max_frames=60,
    frames_per_second=2,
    frame_limit_mode="total",
    ocr_assist: bool | None = None,
    group_id: str | None = None,
    # spec 018：合并任务 —— items 为 [{url, video_id, title, p, bvid}]，
    # 命中时在**这一条任务**里逐条生成再合并成一篇。
    merge_items: list | None = None,
    merge_title: str | None = None,
    merge_polish: bool = False,
):
    # 可变默认参数改成 None + 就地初始化：默认列表会被所有调用共享（spec 018 顺手修）。
    grid_size = [] if grid_size is None else grid_size
    active_model = resolve_active_model()
    model_name = active_model["model_name"]
    provider_id = active_model["provider_id"]

    # 连续失败熔断已暂停时，不再开始新任务（spec 009 决策 12）。
    # 检查点放在这里而不是入队时：批量提交是 200 条一次性丢进池子的，入队那一刻
    # 还没有任何失败发生，只有真正轮到这一条执行时才知道「前面已经连挂 N 个了」。
    # 把它标成 FAILED 而不是留在 PENDING：PENDING 会让它永远显示「排队中」，
    # 而且重启恢复会把它再捞起来重跑一遍 —— 那正是熔断要避免的事。
    if _get_circuit_breaker().is_paused():
        logger.warning(f"任务 {task_id} 因连续失败熔断被跳过（等待用户重新提交）")
        try:
            NoteGenerator()._update_status(
                task_id,
                TaskStatus.FAILED,
                message="连续失败已暂停自动执行，请检查配置后重新提交",
            )
        except Exception as exc:
            logger.warning(f"写入熔断跳过状态失败 (task_id={task_id}): {exc}")
        return

    # 429 停放后重新派发要用同一份参数（spec 012）。就地组一份，键名与
    # `_build_task_kwargs` 的返回值一一对应 —— 语义漂移会让停放的任务按默认参数
    # 重跑，而那种 bug 只在配额耗尽时才显形。
    # 放在合并分支之前：合并任务遇到 429 也要用同一份参数交还队列（spec 018）。
    task_kwargs = {
        "task_id": task_id,
        "video_url": video_url,
        "platform": platform,
        "quality": quality,
        "link": link,
        "screenshot": screenshot,
        "_format": _format,
        "scene": scene,
        "detail": detail,
        "extras": extras,
        "video_understanding": video_understanding,
        "video_interval": video_interval,
        "grid_size": grid_size,
        "sampling_mode": sampling_mode,
        "max_frames": max_frames,
        "frames_per_second": frames_per_second,
        "frame_limit_mode": frame_limit_mode,
        "ocr_assist": ocr_assist,
        "group_id": group_id,
        "merge_items": merge_items,
        "merge_title": merge_title,
        "merge_polish": bool(merge_polish),
    }

    # spec 018 决策 3/6：合并任务在**一条**任务里跑完 N 个视频再合并成一篇。
    # 分叉放在建 generator 之前：合并流程会为每个视频各建一个 generator，
    # 这里再建一个纯属浪费（NoteGenerator 初始化要读转写器配置）。
    if merge_items:
        return _run_merged_note_task(
            task_id=task_id,
            items=merge_items,
            merge_title=merge_title or "",
            merge_polish=bool(merge_polish),
            model_name=model_name,
            provider_id=provider_id,
            platform=platform,
            quality=quality,
            link=link,
            screenshot=screenshot,
            _format=_format,
            scene=scene,
            detail=detail,
            extras=extras,
            video_understanding=video_understanding,
            video_interval=video_interval,
            grid_size=grid_size,
            sampling_mode=sampling_mode,
            max_frames=max_frames,
            frames_per_second=frames_per_second,
            frame_limit_mode=frame_limit_mode,
            ocr_assist=ocr_assist,
            park_kwargs=task_kwargs,
        )

    generator = NoteGenerator()

    def _execute_note_task():
        return generator.generate(
            video_url=video_url,
            platform=platform,
            quality=quality,
            task_id=task_id,
            model_name=model_name,
            provider_id=provider_id,
            link=link,
            _format=_format,
            scene=scene,
            detail=detail,
            extras=extras,
            screenshot=screenshot,
            video_understanding=video_understanding,
            video_interval=video_interval,
            grid_size=grid_size,
            sampling_mode=sampling_mode,
            max_frames=max_frames,
            frames_per_second=frames_per_second,
            frame_limit_mode=frame_limit_mode,
            ocr_assist=ocr_assist,
            group_id=group_id,
        )

    logger.info(f"任务进入执行队列 (task_id={task_id})")
    note = _execute_note_with_retry(task_id, generator, _execute_note_task)
    if note == RATE_LIMIT_PARKED:
        # 429 等满上限（spec 012）：交还队列，不判失败、不计熔断。
        _park_rate_limited_task(task_id, task_kwargs)
        return
    logger.info(f"Note generated: {task_id}")
    if not note or not note.markdown:
        logger.warning(f"任务 {task_id} 执行失败，跳过保存")
        # 连续失败熔断（spec 009 决策 12）：单条失败不改重试语义，只累计计数；
        # 达到阈值就暂停后续尚未开始的任务，避免系统性故障让整批各跑 4 遍流水线。
        breaker = _get_circuit_breaker()
        if breaker.record_failure():
            logger.error(
                f"连续 {breaker.consecutive_failures} 个任务失败，"
                f"已暂停后续任务的自动执行（重新提交任意任务即可恢复）"
            )
        # FAILED 同样是终态，也要清一次（spec 007 §12.1 决策 13）：失败的任务往往
        # 已经把音视频下载到磁盘上了，不释放的话「失败」反而比「成功」更占地方。
        _enforce_cache_limit_in_background()
        return
    _get_circuit_breaker().record_success()
    save_note_to_file(task_id, note)

    # 终态即清（spec 007 §12.1 决策 13/14，spec 009 ticket 07）：
    # 任务跑到 SUCCESS 之后立刻检查一次容量上限，而不是干等下一小时那轮
    # `media_cleanup_loop` —— 批量导入几十条长视频时，一小时足够把磁盘写满。
    # 与下面的向量索引同样丢进后台线程：容量清理要扫目录 + 删文件（可能几秒），
    # 绝不能拖住任务工作线程。
    _enforce_cache_limit_in_background()


def _as_float(value, default: float = 0.0) -> float:
    """把音频元数据里可能出现的任意值安全转成 float（'12:34'、None 都回落 default）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _cleanup_merged_subtask_files(sub_task_id: str) -> None:
    """删掉合并子任务留下的状态/缓存文件（spec 018）。

    子任务用「<合并 task_id>-s<i>」作为内部 task_id，只为让 NoteGenerator 的
    状态文件、转写缓存、markdown 缓存互不覆盖。它们不该在 note_results/ 里
    长期堆积 —— 用户看到的应该只有那一条合并任务。
    """
    for suffix in (".status.json", "_transcript.json", "_markdown.md"):
        path = os.path.join(NOTE_OUTPUT_DIR, f"{sub_task_id}{suffix}")
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            logger.debug(f"清理合并子任务文件失败（{path}）：{exc}")


def _polish_merged_markdown(
    title: str,
    markdown: str,
    model_name: str | None,
    provider_id: str | None,
) -> str:
    """合并后的可选模型润色（spec 018 决策 5）。

    失败一律回落到结构拼接结果：润色是锦上添花，为了它丢掉已经合并好的笔记
    是最坏的取舍。
    """
    try:
        gpt = NoteGenerator()._get_gpt(model_name, provider_id)
    except Exception as exc:
        logger.warning(f"合并润色取模型失败，保留结构拼接结果：{exc}")
        return markdown
    polish = getattr(gpt, "polish_markdown", None)
    if polish is None:
        logger.warning("当前模型实现不支持合并润色，保留结构拼接结果")
        return markdown
    try:
        polished = polish(title, markdown)
    except Exception as exc:
        logger.warning(f"合并笔记润色失败，保留结构拼接结果：{exc}")
        return markdown
    return polished or markdown


def _run_merged_note_task(
    *,
    task_id: str,
    items: list,
    merge_title: str,
    merge_polish: bool,
    model_name: str | None,
    provider_id: str | None,
    platform: str,
    quality: DownloadQuality,
    link: bool,
    screenshot: bool,
    _format: list | None,
    scene: str | None,
    detail: str | None,
    extras: str | None,
    video_understanding: bool,
    video_interval,
    grid_size,
    sampling_mode: str,
    max_frames,
    frames_per_second,
    frame_limit_mode: str,
    ocr_assist: bool | None,
    park_kwargs: dict,
):
    """合并任务执行：逐条生成 → 合并 → 存成**一条**任务（spec 018 §5.2）。

    为什么逐条串行而不是并发：合并要按用户勾选顺序排列章节，而且并发跑多个
    视频会让「最大笔记并行任务数」的语义变得不可预期（一条任务吃掉 N 个槽位）。
    每次子生成仍各自过并行闸门，所以别的任务不会被这条长任务饿死。
    """
    from app.enmus.task_status_enums import TaskStage
    from app.models.notes_model import NoteResult
    from app.services.note_merge import (
        MergedEntry,
        MergedFailure,
        build_merged_audio_meta,
        build_merged_markdown,
        merge_transcripts,
    )

    entries: list[MergedEntry] = []
    failures: list[MergedFailure] = []
    total = len(items)
    status_writer = NoteGenerator()

    for index, raw in enumerate(items, start=1):
        item = raw if isinstance(raw, dict) else {}
        url = str(item.get("url") or "")
        label = str(item.get("title") or "") or f"第 {index} 个视频"
        if not url:
            failures.append(MergedFailure(title=label, reason="链接为空"))
            continue

        try:
            status_writer._update_status(
                task_id,
                TaskStatus.DOWNLOADING,
                stage=TaskStage.DOWNLOADING_VIDEO,
                message=f"合并生成 {index}/{total}：{label}",
                progress=int((index - 1) / max(total, 1) * 90),
            )
        except Exception as exc:  # noqa: BLE001 - 进度写失败不该打断合并
            logger.debug(f"写入合并进度失败（{task_id}）：{exc}")

        sub_task_id = f"{task_id}-s{index}"
        generator = NoteGenerator()

        def _execute(sub_id=sub_task_id, sub_url=url, sub_generator=generator):
            return sub_generator.generate(
                video_url=sub_url,
                platform=platform,
                quality=quality,
                task_id=sub_id,
                model_name=model_name,
                provider_id=provider_id,
                link=link,
                _format=_format,
                scene=scene,
                detail=detail,
                extras=extras,
                screenshot=screenshot,
                video_understanding=video_understanding,
                video_interval=video_interval,
                grid_size=grid_size,
                sampling_mode=sampling_mode,
                max_frames=max_frames,
                frames_per_second=frames_per_second,
                frame_limit_mode=frame_limit_mode,
                ocr_assist=ocr_assist,
                # 子任务不写任务行：用户只该看到一条合并任务（spec 018 决策 3）。
                persist_metadata=False,
            )

        note = _execute_note_with_retry(sub_task_id, generator, _execute)
        _cleanup_merged_subtask_files(sub_task_id)

        if note == RATE_LIMIT_PARKED:
            # 配额一直恢复不了：交还队列。已合并的部分不落盘，重跑时会重新生成。
            logger.warning(
                f"合并任务 {task_id} 因 429 交还队列（第 {index}/{total} 条）"
            )
            _park_rate_limited_task(task_id, park_kwargs)
            return

        if not note or not getattr(note, "markdown", None):
            logger.warning(f"合并子任务失败 task_id={task_id} url={url}")
            failures.append(MergedFailure(title=label, reason="生成失败"))
            continue

        audio = getattr(note, "audio_meta", None)
        entries.append(
            MergedEntry(
                title=(getattr(audio, "title", "") or label),
                markdown=note.markdown,
                duration=_as_float(getattr(audio, "duration", 0)),
                video_id=str(
                    getattr(audio, "video_id", "") or item.get("video_id") or ""
                ),
                cover_url=getattr(audio, "cover_url", None),
                transcript=getattr(note, "transcript", None),
                source_url=url,
            )
        )

    if not entries:
        status_writer._update_status(
            task_id,
            TaskStatus.FAILED,
            message=f"合并失败：{total} 个视频都没能生成",
        )
        _enforce_cache_limit_in_background()
        return

    title = (merge_title or "").strip() or "合并笔记"
    merged_markdown = build_merged_markdown(title, entries, failures)

    if merge_polish:
        try:
            status_writer._update_status(
                task_id,
                TaskStatus.FORMATTING,
                stage=TaskStage.POST_PROCESSING,
                message="正在润色合并笔记",
                progress=95,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"写入润色进度失败（{task_id}）：{exc}")
        merged_markdown = _polish_merged_markdown(
            title, merged_markdown, model_name, provider_id
        )

    merged_note = NoteResult(
        markdown=merged_markdown,
        transcript=merge_transcripts(entries),
        audio_meta=build_merged_audio_meta(
            title,
            entries,
            platform,
            video_id=f"{MERGED_VIDEO_ID_PREFIX}{task_id[:8]}",
        ),
    )

    try:
        status_writer._update_status(
            task_id,
            TaskStatus.SAVING,
            stage=TaskStage.SAVING,
            message="正在保存合并笔记",
            stage_progress=50,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"写入保存进度失败（{task_id}）：{exc}")

    save_note_to_file(task_id, merged_note)
    _get_circuit_breaker().record_success()

    message = f"已合并 {len(entries)}/{total} 个视频"
    if failures:
        message += f"，{len(failures)} 个失败（见笔记末尾）"
    status_writer._update_status(task_id, TaskStatus.SUCCESS, message=message)
    logger.info(f"合并笔记生成成功 (task_id={task_id})：{message}")
    _enforce_cache_limit_in_background()


@router.post("/upload")
async def upload(file: UploadFile = File(...)):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    file_location = os.path.join(UPLOAD_DIR, file.filename or "upload.bin")

    with open(file_location, "wb+") as f:
        f.write(await file.read())

    # 假设你静态目录挂载了 /uploads
    return R.success({"url": f"/uploads/{file.filename}"})


@router.post("/generate_note")
def generate_note(data: VideoRequest, background_tasks: BackgroundTasks):
    try:
        resolve_active_model()
        # 就绪门禁：本地转写引擎（fast-whisper / mlx-whisper）必须等模型下载完才能跑视频，
        # 否则任务会卡在首次下载（慢 / OOM / 截断），用户只看到一个静默失败的任务。
        # 客户端已抓好字幕（prefetched_transcript）则不需要转写，跳过检查。
        if not data.prefetched_transcript:
            from app.services.transcriber_config_manager import TranscriberConfigManager

            readiness = TranscriberConfigManager().is_model_ready()
            if not readiness["ready"]:
                logger.warning(f"拒绝 generate_note：{readiness['reason']}")
                return R.error(
                    msg=readiness["reason"],
                    code=300102,
                    data={
                        "reason": "transcriber_model_not_ready",
                        "transcriber_type": readiness["transcriber_type"],
                        "model_size": readiness["model_size"],
                        "downloading": readiness["downloading"],
                    },
                )

        # 磁盘门禁（spec 009 决策 11）：媒体要落盘，剩余空间不够就别开跑 ——
        # 跑到一半以 IO 错误失败，比一开始就拒绝更浪费（下载、转写都白做）。
        disk_error = _check_disk_space()
        if disk_error:
            return R.error(
                msg=disk_error,
                code=300104,
                data={"reason": "insufficient_disk_space"},
            )

        video_id = extract_video_id(data.video_url, data.platform)
        if video_id is None:
            return R.error(msg="无法识别视频标识", code=400)
        # spec 018 决策 8：多P 视频要与 /expand 用同一套 id（BV…_pN），
        # 否则去重标记与在途判定都会把不同分P 当成同一个视频。
        video_id = _resolve_single_video_id(video_id, data.video_url, data.platform)

        # 同一视频正在生成时不再重复起任务（spec 001 §12）：
        # 并发跑同一视频会互踩 data/data/<id>.mp4 与抽帧目录。
        # spec 008 ticket 03：已无「带 task_id 重试」入口，无需再排除重试场景。
        if _is_task_in_flight(video_id, data.platform):
            return R.error(
                msg="该视频正在生成中，请稍候",
                code=300103,
                data={"reason": "task_in_progress"},
            )

        # spec 008 ticket 03：task_id 一律由服务端生成，客户端不再传入。
        task_id = str(uuid.uuid4())

        # 统一先写入 PENDING，表示已进入队列等待串行执行
        NoteGenerator()._update_status(task_id, TaskStatus.PENDING)

        # 用户主动提交 = 熔断复位信号（spec 009 决策 12）：重新提交本身就说明
        # 用户处理过故障了，不需要再给他一个「恢复」按钮。
        _get_circuit_breaker().reset()

        task_kwargs = _build_task_kwargs(
            task_id, data.video_url, data.platform, data, None
        )
        # spec 007：单条新建的任务默认「未分组」，入队即落库（见 _enqueue_task）。
        _enqueue_task(
            task_id,
            video_id,
            data.platform,
            None,
            _payload_for_storage(task_kwargs),
        )

        # 客户端已经抓好字幕的话，写到转写缓存文件，NoteGenerator 的 cache-hit 逻辑会直接用上
        if data.prefetched_transcript:
            try:
                _persist_prefetched_transcript(task_id, data.prefetched_transcript)
            except Exception as e:
                logger.warning(f"写入预取字幕失败 (task_id={task_id}): {e}")

        # 单条任务仍走 FastAPI 后台任务：它只有一条，不存在「批量串行」问题，
        # 而且这样能保持既有的 BackgroundTasks 行为（测试也依赖它）。
        # 走同一个受登记的入口，让单条任务也进运行登记表（spec 010）。
        background_tasks.add_task(_run_note_task_guarded, task_kwargs)
        return R.success({"task_id": task_id})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class ExpandRequest(BaseModel):
    url: str


class BatchItem(BaseModel):
    bvid: str
    p: int = 1
    video_id: str | None = None
    url: str | None = None
    # spec 018：合并任务要用它做小节标题（前端在 /expand 结果里已经有了）。
    title: str | None = None


class BatchGenerateRequest(BaseModel):
    """批量生成请求（spec §5.2）。items 为前端勾选后的明确列表。"""

    platform: str = "bilibili"
    items: list[BatchItem]
    quality: DownloadQuality = DownloadQuality.fast
    # Legacy request fields are accepted for client compatibility but are never stored or used.
    model_name: str | None = None
    provider_id: str | None = None
    # pi-lens-ignore: python-mutable-class-attr
    format: list | None = []
    scene: str | None = None
    detail: str | None = None
    extras: str | None = None
    screenshot: bool | None = False
    link: bool | None = False
    video_understanding: bool | None = False
    video_interval: int | None = 0
    # pi-lens-ignore: python-mutable-class-attr
    grid_size: list | None = []
    # spec 002：interval=固定间隔抽帧；smart=画面变化+字幕分段高价值抽帧
    sampling_mode: str | None = "interval"
    max_frames: int | None = 60
    # smart 采样限制方式二选一：total=只用 max_frames；per_second=只用 frames_per_second
    frames_per_second: int | None = 2
    frame_limit_mode: str | None = "total"
    # spec 007：批量新建时整批放进一个组。为空 = 全部归入「未分组」。
    group_name: str | None = None
    # 与单条 /generate_note 对齐：这三项此前漏了，批量任务无法覆盖 OCR 与预取字幕。
    ocr_assist: bool | None = None
    # spec 018 决策 3：合并任务 —— 不建组，只产生一条任务，把所选视频的笔记合成一篇。
    merge: bool | None = False
    # 合并笔记标题（merge=true 时生效）；为空时回落到 group_name，再回落到「合并笔记」。
    merge_title: str | None = None
    # spec 018 决策 5：合并后再让模型润色一次（默认关，结构拼接更快且不丢内容）。
    merge_polish: bool | None = False


@router.post("/expand")
def expand(data: ExpandRequest):
    """两阶段流程第一阶段：把合集/多P 链接展开成可勾选的视频+分P 列表。"""
    try:
        url = (data.url or "").strip()
        if not url:
            return R.error(msg="链接不能为空", code=400)

        ref = parse_bilibili_collection_url(url)
        if ref:
            info = fetch_collection(ref["mid"], ref["sid"], ref["kind"])
            videos = [
                _expand_video_entry(v.bvid, v.title, v.duration, v.cover)
                for v in info.videos
            ]
            return R.success(
                {
                    "platform": "bilibili",
                    "type": "collection",
                    "title": info.title,
                    "cover": info.cover,
                    "owner": {"mid": info.owner_mid, "name": info.owner_name},
                    "videos": videos,
                }
            )

        bvid = extract_video_id(url, "bilibili")
        if not bvid:
            return R.error(msg="无法从链接中解析 BV 号", code=400)
        entry = _expand_video_entry(bvid)
        return R.success(
            {
                "platform": "bilibili",
                "type": "video",
                "title": entry.get("title") or "",
                "cover": entry.get("cover") or "",
                "owner": {"mid": "", "name": ""},
                "videos": [entry],
            }
        )
    except Exception as e:
        logger.error(f"展开失败: {e}", exc_info=True)
        return R.error(msg=f"展开失败: {e}")


@router.post("/generate_notes")
def generate_notes(data: BatchGenerateRequest, background_tasks: BackgroundTasks):
    """两阶段流程第二阶段：接收勾选列表，去重后逐条入队。"""
    try:
        # 就绪门禁：与单条 /generate_note 保持一致
        from app.services.transcriber_config_manager import TranscriberConfigManager

        readiness = TranscriberConfigManager().is_model_ready()
        if not readiness["ready"]:
            logger.warning(f"拒绝 generate_notes：{readiness['reason']}")
            return R.error(
                msg=readiness["reason"],
                code=300102,
                data={
                    "reason": "transcriber_model_not_ready",
                    "transcriber_type": readiness["transcriber_type"],
                    "model_size": readiness["model_size"],
                    "downloading": readiness["downloading"],
                },
            )

        # 磁盘门禁（spec 009 决策 11）：整批要下载的媒体可能好几个 GB，
        # 剩余空间不够时拒绝整批，而不是让它们各自跑到一半失败。
        disk_error = _check_disk_space()
        if disk_error:
            logger.warning(f"拒绝 generate_notes：{disk_error}")
            return R.error(
                msg=disk_error,
                code=300104,
                data={"reason": "insufficient_disk_space"},
            )

        # 用户主动提交 = 熔断复位信号（spec 009 决策 12）。
        _get_circuit_breaker().reset()

        # spec 018 决策 3：合并任务走另一条路 —— 不建组、只建一条任务。
        # 必须在去重/建组之前分叉：合并要的是「每个分P 都真的生成一遍」，
        # 跳过已生成的分P 会让合并笔记缺内容。
        if data.merge:
            return _submit_merged_batch(data)

        # 先过一遍去重/在途判定，收集**真正要入队**的条目。
        # 为什么先收集再建组（spec 009 决策 5）：整批都被跳过时不该留下一个空组
        # —— 用户看到「新建的分组里什么都没有」会以为任务丢了。
        pending: list[tuple[BatchItem, str, str]] = []
        results = []
        for item in data.items:
            video_id = item.video_id or item.bvid

            if _is_generated_task(video_id, data.platform):
                results.append(
                    {
                        "video_id": video_id,
                        "status": "skipped",
                        "reason": "already_generated",
                    }
                )
                continue

            if _is_task_in_flight(video_id, data.platform):
                results.append(
                    {
                        "video_id": video_id,
                        "status": "skipped",
                        "reason": "in_progress",
                    }
                )
                continue

            if item.url:
                note_url = item.url
            elif item.video_id and "_p" in item.video_id:
                note_url = f"https://www.bilibili.com/video/{item.bvid}?p={item.p}"
            else:
                note_url = f"https://www.bilibili.com/video/{item.bvid}"

            pending.append((item, video_id, note_url))

        if pending and not (data.model_name and data.provider_id):
            try:
                resolve_active_model()
            except RuntimeError as exc:
                return R.error(msg=str(exc), code=400)

        # 确有任务要入队时才建组。建组失败会抛 BatchGroupError，由外层转成
        # 明确错误 —— 绝不静默把这一批塞进「未分组」（spec 009 决策 4）。
        group_id = _resolve_batch_group(data.group_name) or None if pending else None

        for _item, video_id, note_url in pending:
            task_id = str(uuid.uuid4())
            NoteGenerator()._update_status(task_id, TaskStatus.PENDING)
            task_kwargs = _build_task_kwargs(
                task_id, note_url, data.platform, data, group_id
            )
            # spec 007：入队即落库并带上分组归属，任务立刻出现在对应组里。
            _enqueue_task(
                task_id,
                video_id,
                data.platform,
                group_id,
                _payload_for_storage(task_kwargs),
            )
            # spec 009 决策 6：这里不再用 background_tasks.add_task ——
            # Starlette 的 BackgroundTasks 是 for 循环 await，严格串行，
            # 200 条任务实际并发度恒为 1。改为丢进分发池，由并行闸门决定真正
            # 同时在跑的条数（默认 3）。
            _dispatch_note_task(task_kwargs)
            results.append(
                {"video_id": video_id, "task_id": task_id, "status": "queued"}
            )

        return R.success({"results": results, "group_id": group_id})
    except BatchGroupError as e:
        # 建组失败必须是明确错误：静默退化成「未分组」会让用户以为分组成功了。
        logger.warning(f"批量生成建组失败：{e}")
        return R.error(
            msg=f"新建分组失败：{e}",
            code=300105,
            data={"reason": "group_create_failed"},
        )
    except Exception as e:
        logger.error(f"批量生成失败: {e}", exc_info=True)
        return R.error(msg=f"批量生成失败: {e}")


class BatchArchiveRequest(BaseModel):
    task_ids: list[str]


@router.post("/task_archives")
def download_task_archives(data: BatchArchiveRequest):
    """Download several completed notes as one ZIP without changing source files."""
    task_ids = list(
        dict.fromkeys(str(item).strip() for item in data.task_ids if str(item).strip())
    )
    if not task_ids:
        return R.error("请选择至少一篇已完成笔记", code=400)
    if len(task_ids) > MAX_BATCH_ARCHIVE_TASKS:
        return R.error(f"一次最多归档 {MAX_BATCH_ARCHIVE_TASKS} 篇笔记", code=400)

    from app.utils.note_archive import write_note_archive

    temp_dir = Path(tempfile.mkdtemp(prefix="note-archives-"))
    try:
        archive_path = temp_dir / "notes.zip"
        included = []
        # 逐篇隔离：一篇笔记的问题（结果文件损坏、引用越界、图片超限）绝不能让
        # 另外 194 篇一起下载失败 —— 用户看到的是「打包下载卡住了」，却根本
        # 不知道是哪一篇、也不知道为什么。
        failed: list[dict] = []
        missing_assets = 0
        with ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for task_id in task_ids:
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
                    continue
                result_path = Path(NOTE_OUTPUT_DIR) / f"{task_id}.json"
                if not result_path.is_file():
                    continue
                try:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    failed.append({"task_id": task_id, "error": f"结果文件损坏：{exc}"})
                    logger.warning(
                        "批量归档跳过一篇笔记：task_id=%s 结果文件损坏：%s",
                        task_id,
                        exc,
                    )
                    continue
                markdown = result.get("markdown")
                if not isinstance(markdown, str) or not markdown.strip():
                    continue
                # spec 007 / m04885：zip 内的目录名用「组名/视频标题」而不是
                # task_id。用户拿到的是一串 uuid 时根本认不出哪篇是哪篇。
                title = _safe_file_component(_result_title(result), task_id)
                group_name = _group_display_name_for(task_id)
                if group_name:
                    inner_dir = _safe_file_component(group_name, "未分组")
                    entry_prefix = f"{inner_dir}/{title}"
                else:
                    entry_prefix = f"未分组/{title}"
                note_dir = temp_dir / task_id
                note_dir.mkdir()
                note_zip = note_dir / "note.zip"
                # 已被容量清理删掉的截图只跳过、不报错：实测 195 篇里 64 篇、
                # 共 258 张图已不在磁盘上，严格模式会让整批一篇都下不来。
                skipped_assets: list[str] = []
                try:
                    assets, total_bytes = write_note_archive(
                        markdown,
                        IMAGE_OUTPUT_DIR,
                        note_zip,
                        image_base_url=IMAGE_BASE_URL,
                        max_files=MAX_NOTE_ARCHIVE_FILES,
                        max_bytes=MAX_NOTE_ARCHIVE_BYTES,
                        skip_missing=True,
                        skipped=skipped_assets,
                        # 中间 zip 不再压缩：外层归档还会压一次，压两遍白烧 CPU。
                        compression=zipfile.ZIP_STORED,
                    )
                except NoteArchiveError as exc:
                    failed.append({"task_id": task_id, "error": str(exc)})
                    logger.warning(
                        "批量归档跳过一篇笔记：task_id=%s 原因=%s", task_id, exc
                    )
                    continue
                with ZipFile(note_zip) as single:
                    for item in single.infolist():
                        bundle.writestr(
                            f"{entry_prefix}/{item.filename}", single.read(item)
                        )
                if skipped_assets:
                    missing_assets += len(skipped_assets)
                    logger.warning(
                        "批量归档：笔记 %s 有 %d 张截图已被清理，已跳过",
                        task_id,
                        len(skipped_assets),
                    )
                included.append(
                    {
                        "task_id": task_id,
                        "title": title,
                        "assets": assets,
                        "bytes": total_bytes,
                    }
                )
        if not included:
            raise NoteArchiveError("没有找到可下载的已完成笔记")
        # m04885：下载文件名也要用用户认得出的「组名」，不能再是 notes.zip
        # （一次选中多个组时几个同名文件完全分不清）。
        archive_dir_names = []
        for item in included:
            name = (
                _group_display_name_for(item["task_id"]) or item["title"] or "笔记归档"
            )
            if name not in archive_dir_names:
                archive_dir_names.append(name)
        if len(archive_dir_names) == 1:
            archive_name = archive_dir_names[0]
        else:
            archive_name = f"{archive_dir_names[0]} 等{len(archive_dir_names)}个组"
        archive_filename = f"{_safe_file_component(archive_name, '笔记归档')}.zip"
        if failed:
            logger.warning(
                "批量归档完成，但有 %d 篇被跳过：%s",
                len(failed),
                "; ".join(f"{item['task_id']}: {item['error']}" for item in failed[:5]),
            )
        logger.info(
            "批量归档完成：请求=%d 收录=%d 跳过=%d 缺图=%d",
            len(task_ids),
            len(included),
            len(failed),
            missing_assets,
        )
        # 跳过与缺图必须如实带回前端：用户看到的「下载成功」如果少了 64 篇，
        # 那是误导而不是成功。
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=archive_filename,
            headers={
                "X-Archive-Notes": str(len(included)),
                "X-Archive-Skipped": str(len(failed)),
                "X-Archive-Missing-Assets": str(missing_assets),
            },
            background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
        )
    except NoteArchiveError as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return R.error(str(exc), code=400)
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        logger.exception("批量笔记归档导出失败")
        return R.error(f"批量笔记归档导出失败：{exc}", code=500)


@router.get("/task_pdf/{task_id}")
def download_task_pdf(task_id: str):
    """Export a completed note as a PDF with local images and clickable links."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
        return R.error("任务标识无效", code=400)
    result_path = Path(NOTE_OUTPUT_DIR) / f"{task_id}.json"
    if not result_path.is_file():
        return R.error("笔记不存在或尚未生成", code=404)
    from app.services.note_pdf import NotePdfError, build_note_pdf

    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
        markdown = result.get("markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            return R.error("笔记内容为空，无法导出 PDF", code=404)
        title = _result_title(result) or "视频笔记"
        content = build_note_pdf(markdown, title, IMAGE_OUTPUT_DIR, image_base_url=IMAGE_BASE_URL)
        filename = _safe_file_component(title, "note") + ".pdf"
        return Response(
            content,
            media_type="application/pdf",
            headers={
                "Content-Disposition": "attachment; filename=note.pdf; filename*=UTF-8''" + quote(filename),
                "Cache-Control": "no-store",
            },
        )
    except NotePdfError as exc:
        return R.error(str(exc), code=400)
    except Exception:
        logger.exception("笔记 PDF 导出失败 task_id=%s", task_id)
        return R.error("PDF 导出失败，请重试或检查笔记内容", code=500)


@router.get("/task_archive/{task_id}")
def download_task_archive(task_id: str):
    """Download a generated note and its locally referenced screenshots as ZIP."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
        return R.error("任务标识无效", code=400)
    result_path = Path(NOTE_OUTPUT_DIR) / f"{task_id}.json"
    if not result_path.is_file():
        return R.error("笔记不存在或尚未生成", code=404)
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
        markdown = result.get("markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            return R.error("笔记内容为空，无法导出", code=404)
        from app.utils.note_archive import write_note_archive

        temp_dir = Path(tempfile.mkdtemp(prefix="note-archive-"))
        try:
            archive_path = temp_dir / f"{task_id}.zip"
            assets, total_bytes = write_note_archive(
                markdown,
                IMAGE_OUTPUT_DIR,
                archive_path,
                image_base_url=IMAGE_BASE_URL,
                max_files=MAX_NOTE_ARCHIVE_FILES,
                max_bytes=MAX_NOTE_ARCHIVE_BYTES,
            )
            # 顶层没有 title，标题在 audio_meta.title（早期读了不存在的键，
            # 于是所有导出都叫 note.zip）。保留中文，只过滤文件系统危险字符。
            title = _safe_file_component(_result_title(result), "note")
            return FileResponse(
                archive_path,
                media_type="application/zip",
                filename=f"{title}.zip",
                headers={
                    "X-Archive-Assets": str(assets),
                    "X-Archive-Asset-Bytes": str(total_bytes),
                },
                background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
            )
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
    except NoteArchiveError as exc:
        return R.error(str(exc), code=400)
    except Exception as exc:
        logger.exception("笔记归档导出失败 task_id=%s", task_id)
        return R.error(f"笔记归档导出失败：{exc}", code=500)


_STATUS_LIGHT_KEYS = (
    "status",
    "stage",
    "stage_label",
    "progress",
    "stage_progress",
    "message",
    "detail",
    "updated_at",
)


def _pending_status(task_id: str) -> dict:
    """没有任何落盘痕迹时的「排队中」占位状态。"""
    return {
        "task_id": task_id,
        "status": TaskStatus.PENDING.value,
        "message": "任务排队中",
        "stage": "queued",
        "stage_label": "排队中",
        "progress": 0,
        "stage_progress": 0,
    }


def _task_row_exists(task_id: str) -> bool:
    """库里是否还有这条任务记录（用于区分「刚入队」与「已被删除」）。"""
    try:
        from app.db.video_task_dao import get_video_task

        return get_video_task(task_id) is not None
    except Exception as exc:
        # 读库失败时按「存在」处理：宁可多给一个占位状态，也不要把在跑的任务
        # 报成 null 让前端以为它消失了。
        logger.debug("查询任务记录失败（%s）：%s", task_id, exc)
        return True


def _light_status_payload(task_id: str, source: dict) -> dict:
    """把任意来源的状态字典裁成对外的轻量 payload。

    契约的唯一出口：无论 payload 来自内存注册表还是 ``.status.json``，都经过这里，
    保证两条路径返回的字段集合完全一致（``_STATUS_LIGHT_KEYS`` + ``message`` 默认值）。
    """
    payload = {"task_id": task_id}
    for key in _STATUS_LIGHT_KEYS:
        if key in source:
            payload[key] = source[key]
    payload.setdefault("message", "")
    return payload


def _read_result_payload(task_id: str):
    """读 ``<task_id>.json`` 的完整结果；文件不存在或读失败返回 ``None``。

    **结果不进内存注册表**：实测 873 个结果文件共 47.6MB、平均 55.9KB，而注册表
    存的是 216 字节量级的状态。详情页本来就不是高频路径（批量端点传
    ``include_result=False``），把 markdown 常驻内存换不来任何东西。
    """
    result_path = os.path.join(NOTE_OUTPUT_DIR, f"{task_id}.json")
    if not os.path.exists(result_path):
        return None
    try:
        with open(result_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.debug("读取任务结果失败（%s）：%s", task_id, exc)
        return None


def _read_task_status(
    task_id: str,
    include_result: bool = True,
    unknown_is_none: bool = False,
):
    """读一条任务的状态；``include_result=False`` 时**不带** markdown/transcript。

    为什么要有这个开关（spec 009 决策 13–14）：批量轮询 200 个任务时，每个任务
    成功都会把整份 markdown + transcript 塞进响应体 —— 几百 KB × 200 个任务，
    每 3 秒一轮。批量端点只要状态，详情由详情页按需拉。

    ``unknown_is_none`` 控制「状态文件与结果文件都不存在」时的返回值。单任务
    端点（``GET /task_status/{task_id}``）历来把这种情况当成 PENDING 占位，保持
    不变；批量端点要求未知 id 返回 ``None``，由调用方按需回落到占位。

    **内存优先（spec 014 ticket 02）**：先查进程内注册表（纯 dict 查找，零系统
    调用），未命中才回落文件。这是收益的全部来源 —— 基线 N=200 时每轮 899.8 次
    ``stat``、198.46ms，全是同步系统调用，在单进程 uvicorn 里每 3 秒阻塞一次
    事件循环。**不做指纹校验**（该机制已在 spec §12.1 作废）：注册表的权威性靠
    写方主动 ``put`` 与删除/重试路径主动 ``drop`` 维持，不是靠比对文件时间戳。
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(task_id or "")):
        return None

    from app.services import task_status_cache

    cached = task_status_cache.get(task_id)
    if cached is not None:
        # _light_status_payload 建的是新 dict，所以下面补 result 不会污染注册表。
        payload = _light_status_payload(task_id, cached)
        if payload.get("status") == TaskStatus.SUCCESS.value and include_result:
            result = _read_result_payload(task_id)
            if result is not None:
                payload["result"] = result
        return payload

    status_path = os.path.join(NOTE_OUTPUT_DIR, f"{task_id}.status.json")
    result_path = os.path.join(NOTE_OUTPUT_DIR, f"{task_id}.json")

    if os.path.exists(status_path):
        try:
            with open(status_path, encoding="utf-8") as f:
                status_content = json.load(f)
        except Exception as exc:
            logger.debug("读取任务状态失败（%s）：%s", task_id, exc)
            return None

        payload = _light_status_payload(task_id, status_content)
        # 存副本：payload 随后可能被塞入 result（几十 KB），不能连带写进注册表。
        task_status_cache.put(task_id, dict(payload))

        if payload.get("status") == TaskStatus.SUCCESS.value and include_result:
            result = _read_result_payload(task_id)
            if result is not None:
                payload["result"] = result
        return payload

    if os.path.exists(result_path):
        payload = {
            "task_id": task_id,
            "status": TaskStatus.SUCCESS.value,
            "stage": "success",
            "stage_label": "已完成",
            "progress": 100,
            "stage_progress": 100,
            "message": "",
        }
        task_status_cache.put(task_id, dict(payload))
        if include_result:
            result = _read_result_payload(task_id)
            if result is not None:
                payload["result"] = result
        return payload

    if unknown_is_none:
        return None
    # 「排队中」占位**不进注册表**：它表达的是「没有任何落盘痕迹」，不是权威状态。
    return _pending_status(task_id)


_TASK_READ_MODEL = TaskReadModel()


@router.get("/v1/task-groups/events")
async def get_task_groups_events(
    request: Request,
    task_ids: list[str] = Query(default_factory=list),
    source: str = "sse",
    since: int = 0,
):
    """Stream versioned task snapshots; clients can resume with the last revision.

    The handler and the generator are **async** on purpose. A sync generator is iterated
    inside Starlette's bounded worker pool, so one open stream parks a worker thread for
    the whole 300s window. With a couple of open tabs (each EventSource reconnects on
    error) the pool is gone and every other sync route — task list, note groups — stops
    answering while this one looks perfectly healthy. Awaiting the sleep instead means an
    idle stream costs no thread at all; only the snapshot read hops into a worker.
    """
    ids = list(
        dict.fromkeys(
            str(task_id).strip() for task_id in task_ids if str(task_id).strip()
        )
    )
    if len(ids) > 1000:
        return R.error("一次最多订阅 1000 个任务", code=400)

    def read_snapshot() -> dict[str, Any]:
        return _TASK_READ_MODEL.snapshot(
            ids,
            lambda task_id: _read_task_status(
                task_id, include_result=False, unknown_is_none=True
            ),
            source=source,
        )

    async def event_stream():
        last_revision = since
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if await request.is_disconnected():
                break
            snapshot = await run_in_threadpool(read_snapshot)
            revision = int(snapshot.get("snapshotRevision", 0))
            if revision > last_revision or last_revision == since:
                last_revision = revision
                yield f"id: {revision}\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
            if all(
                task.get("status") in {"succeeded", "failed", "cancelled"}
                for task in snapshot.get("tasks", [])
            ):
                break
            await asyncio.sleep(1)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.get("/v1/task-groups/snapshot")
def get_task_groups_snapshot(
    task_ids: list[str] = Query(default_factory=list), source: str = "snapshot"
):
    """Return the versioned task read model while legacy endpoints remain available."""
    ids = list(
        dict.fromkeys(
            str(task_id).strip() for task_id in task_ids if str(task_id).strip()
        )
    )
    snapshot = _TASK_READ_MODEL.snapshot(
        ids,
        lambda task_id: _read_task_status(
            task_id, include_result=False, unknown_is_none=True
        ),
        source=source,
    )
    return R.success(snapshot)


class BatchStatusRequest(BaseModel):
    """批量状态查询：一次拿回多个 task_id 的轻量状态。"""

    task_ids: list[str]


@router.post("/task_status/batch")
def get_task_status_batch(data: BatchStatusRequest):
    """批量取状态（spec 009 决策 13–14）。

    为什么用 POST：200 个 task_id 拼进 GET query 会超长（uuid 36 字符 × 200
    ≈ 7.5 KB），部分代理直接 414。请求体没有这个限制。

    未知 / 非法 id 返回 ``null`` 而不是报错：轮询期间任务可能刚被删除，报错会
    让前端把整轮结果丢掉。
    """
    try:
        # 去重但保持顺序：前端可能重复提交同一个 id，重复读文件纯属浪费。
        task_ids = list(
            dict.fromkeys(str(t).strip() for t in data.task_ids if str(t).strip())
        )
        # 上限保护：轮询本来就只有几百个待办，几千个说明调用方有问题。
        if len(task_ids) > 1000:
            return R.error("一次最多查询 1000 个任务状态", code=400)
        # 未知 id 返回 null（见 docstring）。但「没有状态文件」不等于「任务不存在」：
        # 状态文件与结果文件都可能还没落盘（例如任务刚入队就被查询），这种要按
        # PENDING 占位返回，否则前端会以为任务消失了。区分办法是查库里的任务行。
        states = {}
        for tid in task_ids:
            state = _read_task_status(tid, include_result=False, unknown_is_none=True)
            if state is None and _task_row_exists(tid):
                state = _pending_status(tid)
            states[tid] = state
        return R.success({"states": states})
    except Exception as exc:
        logger.exception("批量查询任务状态失败")
        return R.error(f"批量查询任务状态失败：{exc}", code=500)


@router.get("/task_status/{task_id}")
def get_task_status(task_id: str):
    status_path = os.path.join(NOTE_OUTPUT_DIR, f"{task_id}.status.json")
    result_path = os.path.join(NOTE_OUTPUT_DIR, f"{task_id}.json")

    # 优先读状态文件
    if os.path.exists(status_path):
        with open(status_path, encoding="utf-8") as f:
            status_content = json.load(f)

        status = status_content.get("status")
        message = status_content.get("message", "")
        # Preserve legacy fields while forwarding optional progress metadata.
        progress_data = {
            key: status_content[key]
            for key in (
                "stage",
                "stage_label",
                "progress",
                "stage_progress",
                "detail",
                "updated_at",
            )
            if key in status_content
        }

        if status == TaskStatus.SUCCESS.value:
            # 成功状态的话，继续读取最终笔记内容
            if os.path.exists(result_path):
                with open(result_path, encoding="utf-8") as rf:
                    result_content = json.load(rf)
                return R.success(
                    {
                        "status": status,
                        "result": result_content,
                        "message": message,
                        "task_id": task_id,
                        **progress_data,
                    }
                )
            else:
                # 理论上不会出现，保险处理
                return R.success(
                    {
                        "status": TaskStatus.PENDING.value,
                        "message": "任务完成，但结果文件未找到",
                        "task_id": task_id,
                        **progress_data,
                    }
                )

        if status == TaskStatus.FAILED.value:
            return R.error(
                message or "任务失败",
                code=500,
                data={"status": status, "task_id": task_id, **progress_data},
            )

        # 处理中状态
        return R.success(
            {
                "status": status,
                "message": message,
                "task_id": task_id,
                **progress_data,
            }
        )

    # 没有状态文件，但有结果
    if os.path.exists(result_path):
        with open(result_path, encoding="utf-8") as f:
            result_content = json.load(f)
        return R.success(
            {
                "status": TaskStatus.SUCCESS.value,
                "result": result_content,
                "task_id": task_id,
                "stage": "success",
                "stage_label": "已完成",
                "progress": 100,
                "stage_progress": 100,
            }
        )

    # 什么都没有，默认PENDING
    return R.success(
        {
            "status": TaskStatus.PENDING.value,
            "message": "任务排队中",
            "task_id": task_id,
            "stage": "queued",
            "stage_label": "排队中",
            "progress": 0,
            "stage_progress": 0,
        }
    )


class TaskIdRequest(BaseModel):
    """删除任务记录只需要 task_id。"""

    task_id: str


def _loads_result(raw: bytes):
    """解析结果文件内容：``orjson`` 优先，标准库 ``json`` 兜底。

    **为什么必须留兜底**：``orjson`` 比标准库严格的不是一个档次 ——

    - 它拒绝裸 ``NaN`` / ``Infinity``（严格 JSON），而标准库 ``json.dump`` 在默认
      ``allow_nan=True`` 下就会把它们写出来，结果文件恰恰是 ``save_note_to_file``
      用 ``json.dump(asdict(note), f, ensure_ascii=False, indent=2)`` 写的；
    - 它把超出 64 位的整数**降级成 float**（丢精度），标准库保留精确整数。本函数只取
      ``markdown`` 与 ``audio_meta.title`` 两个字符串字段，所以不影响结果 —— 但别拿
      这两个函数去解析需要精确大整数的数据。

    少了这层兜底，历史上一直读得动的结果文件会突然解析失败，用户看到的是「标题丢了、
    笔记标记没了」这种**静默降级**。这两条差异都有 ``test_task_list_single_read.py``
    的 ``test_orjson_strictness_is_absorbed_by_the_fallback`` 用例钉住。

    显式 ``decode("utf-8")`` 而不是把 bytes 直接喂给 ``json.loads``：后者会自动嗅探
    UTF-16/32，等于悄悄放宽了改动前 ``read_text(encoding="utf-8")`` 的语义。
    """
    try:
        return orjson.loads(raw)
    except Exception:
        return json.loads(raw.decode("utf-8"))


def _read_result_summary(task_id: str) -> tuple:
    """一次读盘同时算出 ``(title, has_note)``。

    ``/task_list`` 的每一行原本**分别**调 :func:`_task_title_from_file` 与
    :func:`_task_has_note`，而这两个函数读的是**同一个** ``note_results/<task_id>.json``、
    做的是**同一遍**解析。结果文件实测平均 **55.9KB**（中位 18.1KB，873 个共 47.6MB），
    重复解析的代价是实打实的：N=200 时 ``/task_list`` p50 78.09ms 里有 151.4ms 花在这
    两次读上，而纯 SQL 只有 2.9ms。

    解析用 ``orjson``（已在 ``requirements.txt`` 且装好，此前全仓零处使用）：它比标准库
    ``json`` 快 2–5 倍，而这里是本 spec 唯一真正的「大文件重解析」场景——状态文件只有
    216B，换过去毫无收益，所以全仓其它 ``json.loads`` 一律不动。
    ``orjson.loads`` 吃 ``bytes``，所以走 ``read_bytes()``（顺带省掉一次 UTF-8 解码）。
    解析本身在 :func:`_loads_result`：``orjson`` 优先、标准库兜底 —— 它比标准库严格，
    裸 ``NaN`` / 超 64 位整数它都拒收，而结果文件正是标准库 ``json.dump`` 写的。

    失败态与原来的两个函数保持一致：文件缺失 / 读失败 / JSON 损坏 → ``("", False)``。
    """
    result_path = Path(NOTE_OUTPUT_DIR) / f"{task_id}.json"
    if not result_path.is_file():
        return "", False
    try:
        result = _loads_result(result_path.read_bytes())
    except Exception as exc:
        logger.debug("读取任务结果失败（%s）：%s", task_id, exc)
        return "", False
    if not isinstance(result, dict):
        # 合法 JSON 但不是对象（比如 ``[1,2,3]``）：原实现在 ``result.get(...)`` 处抛
        # AttributeError，会顺着 ``list_tasks`` 的兜底 except 把**整个列表接口**打成 500
        # ——一个坏文件毁掉全表。这里退化成「这个任务没有标题也没有笔记」。
        logger.debug("任务结果不是对象（%s）：%s", task_id, type(result).__name__)
        return "", False
    markdown = result.get("markdown")
    has_note = isinstance(markdown, str) and bool(markdown.strip())
    return _result_title(result), has_note


def _task_title_from_file(task_id: str) -> str:
    """从 ``note_results/<task_id>.json`` 里取视频标题；读不到就返回空串。

    薄封装：真正的读盘与解析在 :func:`_read_result_summary`（``/task_list`` 用它一次
    读出一行需要的两个字段）。保留本函数是因为测量脚本按函数粒度计开销。

    结果 JSON 顶层只有 markdown / transcript / audio_meta，标题在 audio_meta.title
    （见 :func:`_result_title`）。
    """
    return _read_result_summary(task_id)[0]


def _task_has_note(task_id: str) -> bool:
    """结果文件存在且 markdown 非空，才算「已有笔记」。

    薄封装，原因见 :func:`_task_title_from_file`。
    """
    return _read_result_summary(task_id)[1]


@router.get("/task_list")
def list_tasks():
    """列出全部任务记录（含分组归属与标题），供前端刷新后重建列表。

    任务列表整个迁到后端之后，前端刷新就会丢掉全部任务（用户决策 m04951）。
    这个接口提供「重建列表」所需的最小字段：``task_id`` / ``video_id`` /
    ``platform`` / ``group_id`` / ``title`` / ``has_note`` / ``created_at``。
    """
    try:
        from app.db.note_group_dao import list_groups
        from app.db.video_task_dao import list_video_tasks

        rows = list_video_tasks()
        group_names = {
            str(item.get("id")): str(item.get("name") or "") for item in list_groups()
        }
        tasks = []
        for row in rows:
            task_id = str(row.get("task_id") or "")
            if not task_id:
                continue
            group_id = row.get("group_id")
            # 一次读盘同时拿 title 与 has_note：原本这里分别调 _task_title_from_file 与
            # _task_has_note，两个函数读的是同一个结果文件、做同一遍解析。
            title, has_note = _read_result_summary(task_id)
            tasks.append(
                {
                    "task_id": task_id,
                    "video_id": str(row.get("video_id") or ""),
                    "platform": str(row.get("platform") or ""),
                    "group_id": str(group_id) if group_id else None,
                    "group_name": group_names.get(str(group_id), "")
                    if group_id
                    else "",
                    "title": title,
                    "has_note": has_note,
                    "created_at": str(row.get("created_at") or ""),
                }
            )
        return R.success({"tasks": tasks, "total": len(tasks)})
    except Exception as exc:
        logger.exception("列出任务失败")
        return R.error(f"列出任务失败：{exc}", code=500)


#: task_id 的合法形态。批量删除时逐个校验，避免把路径片段当成 id 用。
_TASK_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")

#: 单次批量删除的上限。与 ``/task_status/batch`` 同规格（spec 009 决策 13）。
MAX_BATCH_DELETE = 1000

#: 「任务仍在执行，拒绝删除」的业务码（spec 010）。紧接 300104/300105。
CODE_TASK_IN_FLIGHT = 300106


def _valid_task_id(task_id: str) -> bool:
    return bool(_TASK_ID_PATTERN.fullmatch(task_id))


@router.post("/delete_task_record")
def delete_task_record(data: TaskIdRequest):
    """删除服务端任务记录（含状态/结果/转写/封面等派生文件）。

    老的 ``/delete_task`` 是空壳（只有 TODO），前端删掉本地那条之后一刷新又冒出来
    —— 因为任务列表现在整个来自后端。

    真正的删除动作在 :func:`app.services.task_cleanup.delete_task_completely`，
    与「删除分组时连组内笔记一起删」共用同一份实现（spec 010）—— 两份各自漂移的
    「什么算删干净」正是 ``utils/video_reader.py`` 那个 Windows ``os.rename`` 缺陷的成因。
    """
    task_id = str(getattr(data, "task_id", "") or "").strip()
    if not task_id:
        return R.error("缺少 task_id", code=400)
    if not _valid_task_id(task_id):
        return R.error("任务标识无效", code=400)
    try:
        from app.services.task_cleanup import delete_task_completely

        return R.success(delete_task_completely(task_id))
    except TaskInFlightError as exc:
        return R.error(str(exc), code=CODE_TASK_IN_FLIGHT)
    except Exception as exc:
        logger.exception("删除任务记录失败")
        return R.error(f"删除任务记录失败：{exc}", code=500)


class BatchDeleteRequest(BaseModel):
    """批量删除任务记录（前端多选删除）。"""

    task_ids: list[str]


@router.post("/delete_task_records")
def delete_task_records(data: BatchDeleteRequest):
    """批量删除任务记录（spec 010）。

    与 ``/delete_task_record`` 的差别只有「一次多条」，删除语义完全一致（同一个
    ``delete_task_completely``）。**逐条隔离**：某一条失败不影响其余条目，失败原因
    随 ``failed`` 一起返回，让前端能如实说清「哪几条没删掉、为什么」——批量接口
    最怕的就是一条坏数据把整批回滚，用户重试一次还是全挂。

    进行中的任务会被拒绝并归入 ``failed``。
    """
    raw_ids = list(getattr(data, "task_ids", None) or [])
    if not raw_ids:
        return R.error("缺少 task_ids", code=400)
    if len(raw_ids) > MAX_BATCH_DELETE:
        return R.error(f"一次最多删除 {MAX_BATCH_DELETE} 条", code=400)

    # 去重保序：同一个 id 出现两次只删一次，处理顺序与请求顺序一致。
    seen: dict = {}
    for item in raw_ids:
        text = str(item or "").strip()
        if text:
            seen.setdefault(text, None)

    from app.services.task_cleanup import delete_task_completely

    deleted: list = []
    failed: list = []
    for task_id in seen:
        if not _valid_task_id(task_id):
            failed.append({"task_id": task_id, "error": "任务标识无效"})
            continue
        try:
            deleted.append(delete_task_completely(task_id))
        except TaskInFlightError as exc:
            failed.append({"task_id": task_id, "error": str(exc)})
        except Exception as exc:
            logger.warning("批量删除任务 %s 失败：%s", task_id, exc)
            failed.append({"task_id": task_id, "error": str(exc)})

    return R.success(
        {
            "deleted": [item["task_id"] for item in deleted],
            "failed": failed,
            "removed_rows": sum(int(item["removed"]) for item in deleted),
            "files": [name for item in deleted for name in item["files"]],
        }
    )


@router.get("/image_proxy")
async def image_proxy(request: Request, url: str):
    headers = {
        "Referer": "https://www.bilibili.com/",
        "User-Agent": request.headers.get("User-Agent", ""),
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)

            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code, detail="图片获取失败")

            content_type = resp.headers.get("Content-Type", "image/jpeg")
            return StreamingResponse(
                resp.aiter_bytes(),
                media_type=content_type,
                headers={
                    "Cache-Control": "public, max-age=86400",  #  缓存一天
                    "Content-Type": content_type,
                },
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class RetryFailedRequest(BaseModel):
    """批量重试失败任务（spec 011）。

    ``scope="all"``：库里全部失败任务。
    ``scope="group"``：``group_id`` 指定分组内 —— ``None`` 表示「未分组」。
    """

    scope: str = "all"
    group_id: str | None = None


@router.post("/retry_failed_tasks")
def retry_failed_tasks_endpoint(data: RetryFailedRequest):
    """把失败的任务原地重跑（spec 011）。

    用户需求（m02871）：全选数据库里的失败任务、或某个组内的失败任务重做，加一个
    按钮能自动重试失败的任务。

    **为什么复用同一个 task_id（决策 2）**：任务列表里那条记录从「失败」变回
    「排队中」，分组归属保留，历史不会因为一次重试多出一条重复记录。参数取自创建时
    存下的 ``payload_json``（决策 3）—— 用户在生成表单里改过的参数不会影响已创建
    的任务，重试出来的是同一篇笔记。

    **范围由后端算（决策 1）**：``scope="all"`` 走全表，``scope="group"`` 走
    ``list_task_ids_in_group``。前端只负责把「当前可见范围」翻译成一个请求，
    不自己筛一遍 —— 两处各判一次必然会漂移。

    **只有 FAILED 会重试**：SUCCESS 不重试（会覆盖用户已看过的东西），在途不重试
    （同一个 task_id 跑两遍会互写文件）。跳过项连同原因一起返回，前端如实展示。
    """
    try:
        scope = str(getattr(data, "scope", "all") or "all").strip().lower()
        if scope not in {"all", "group"}:
            return R.error("scope 只能是 all 或 group", code=400)

        # 就绪门禁：与 /generate_notes 保持一致。任务之所以失败，很常见的原因就是
        # 转写模型没配好 —— 那种情况下重试只会原样再失败一遍，不如直接拒绝。
        from app.services.transcriber_config_manager import TranscriberConfigManager

        readiness = TranscriberConfigManager().is_model_ready()
        if not readiness["ready"]:
            logger.warning(f"拒绝 retry_failed_tasks：{readiness['reason']}")
            return R.error(
                msg=readiness["reason"],
                code=300102,
                data={
                    "reason": "transcriber_model_not_ready",
                    "transcriber_type": readiness["transcriber_type"],
                    "model_size": readiness["model_size"],
                    "downloading": readiness["downloading"],
                },
            )

        # 磁盘门禁（spec 009 决策 11）：重试同样要重新下载媒体。
        disk_error = _check_disk_space()
        if disk_error:
            logger.warning(f"拒绝 retry_failed_tasks：{disk_error}")
            return R.error(
                msg=disk_error,
                code=300104,
                data={"reason": "insufficient_disk_space"},
            )

        # 用户主动重试 = 熔断复位信号（spec 009 决策 12）。熔断本来就是为了拦住
        # 「连续失败还在自动重排」；用户明确要求重试，就不该被它挡住。
        _get_circuit_breaker().reset()

        from app.services.task_retry import retry_failed_tasks

        if scope == "all":
            stats = retry_failed_tasks(None)
        else:
            from app.db.note_group_dao import list_task_ids_in_group

            task_ids = list_task_ids_in_group(data.group_id)
            stats = retry_failed_tasks(task_ids)

        return R.success(
            {
                "scope": scope,
                "group_id": data.group_id if scope == "group" else None,
                "candidates": stats["candidates"],
                "requeued": stats["requeued"],
                "skipped": stats["skipped"],
                "missing": stats["missing"],
            }
        )
    except Exception as exc:
        logger.exception("重试失败任务失败")
        return R.error(f"重试失败任务失败：{exc}", code=500)
