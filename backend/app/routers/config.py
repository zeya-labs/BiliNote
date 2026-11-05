import os
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import Any, Optional
from app.utils.response import ResponseWrapper as R
from app.utils.logger import get_logger
from app.utils.path_helper import get_model_dir

from app.services import task_status_cache
from app.services.cookie_manager import CookieConfigManager
from app.services.transcriber_config_manager import TranscriberConfigManager
from app.transcriber import model_download_state as dl_state
from ffmpeg_helper import ensure_ffmpeg_or_raise

logger = get_logger(__name__)

router = APIRouter()
cookie_manager = CookieConfigManager()
transcriber_config_manager = TranscriberConfigManager()


class CookieUpdateRequest(BaseModel):
    platform: str
    cookie: str


@router.get("/get_downloader_cookie/{platform}")
def get_cookie(platform: str):
    cookie = cookie_manager.get(platform)
    if not cookie:
        return R.success(msg='未找到Cookies')
    return R.success(
        data={"platform": platform, "cookie": cookie}
    )


@router.post("/update_downloader_cookie")
def update_cookie(data: CookieUpdateRequest):
    cookie_manager.set(data.platform, data.cookie)
    return R.success(

    )

class TranscriberConfigRequest(BaseModel):
    transcriber_type: str
    whisper_model_size: Optional[str] = None
    openai_asr_id: Optional[str] = None
    whisper_device: Optional[str] = None
    whisper_compute_type: Optional[str] = None
    whisper_cpu_threads: Optional[int] = None
    whisper_num_workers: Optional[int] = None


class OpenAISREndpointRequest(BaseModel):
    name: str
    base_url: str
    api_key: str
    model: str
    timeout: Optional[int] = None
    # 切割阈值（MB）：超过就切成多段分别转写
    chunk_size_mb: Optional[float] = None
    # transcriptions=Whisper 风格 /audio/transcriptions；chat=chat/completions + input_audio（MiMo 等）
    # 默认 None：更新接口不传时保持原值，不会被悄悄重置
    api_style: Optional[str] = None
    # 静音切片参数（chat 风格用）
    max_speech_chunks: Optional[int] = None
    silence_noise_db: Optional[float] = None
    min_silence_seconds: Optional[float] = None
    # 每分钟最多请求次数，防止触发服务端 429
    max_rpm: Optional[int] = None
    # 切片转写的并发请求数（1-32，1 即串行）；并发只填满 max_rpm 允许的额度
    chunk_concurrency: Optional[int] = None
    # chat 风格是否按静音边界切片。默认 None：更新接口不传时保持原值。
    # 关掉可省去「完整解码音频 + silencedetect」这一整轮耗时。
    silence_split: Optional[bool] = None


AVAILABLE_TRANSCRIBER_TYPES = [
    {"value": "fast-whisper", "label": "Faster Whisper（本地）"},
    {"value": "openai-asr", "label": "云端音频转写"},
]

WHISPER_MODEL_SIZES = ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"]


@router.get("/transcriber_config")
def get_transcriber_config():
    from app.transcriber.whisper_models import get_registry, BUILTIN_WHISPER_MODELS

    registry = get_registry()
    # 用打码版：openai_asr_endpoints 里的 api_key 不能明文回传
    config = transcriber_config_manager.get_public_config()
    return R.success(data={
        **config,
        "available_types": AVAILABLE_TRANSCRIBER_TYPES,
        # 内置可见档位 + 用户自定义模型，供前端下拉
        "whisper_model_sizes": registry.visible_model_names(),
        "whisper_builtin_models": BUILTIN_WHISPER_MODELS,
        "whisper_custom_models": registry.get_custom_models(),
        # 供设置页展示本地推理硬件能力（CUDA 是否可用不依赖 torch）
        "local_hardware": _detect_local_inference_hardware(),
    })


def _detect_local_inference_hardware() -> dict:
    """探测本地 faster-whisper 推理硬件能力。

    用 ctranslate2（faster-whisper 的真正后端）探测 CUDA，而不是 torch——
    torch 在纯用 faster-whisper + 在线转写引擎的轻量部署里可以不装，但
    ctranslate2 自带 CUDA 运行时，缺的就是上层的算子库(site-packages/nvidia)。
    """
    result = {
        "cpu_cores": os.cpu_count(),
        "cuda_available": False,
        "gpu_name": None,
        "cuda_dll_ready": False,
        "device": "cpu",
    }
    try:
        import ctranslate2
        result["cuda_available"] = ctranslate2.get_cuda_device_count() > 0
        if result["cuda_available"]:
            result["gpu_name"] = _gpu_name()
            # 算子库就绪 = nvidia/cudnn/bin 里能找到 cudnn_ops 系列 DLL
            result["cuda_dll_ready"] = _cuda_dll_ready()
    except Exception as e:
        logger.warning(f"探测本地 CUDA 能力失败: {e}")
    result["device"] = transcriber_config_manager.get_whisper_device()
    return result


def _cuda_dll_ready() -> bool:
    """检查 site-packages 里 cudnn/cublas 算子库是否就位（决定 GPU 能否真正跑）。"""
    import site
    for sp in site.getsitepackages():
        candidates = [
            Path(sp) / "nvidia" / "cudnn" / "bin",
            Path(sp) / "nvidia" / "cublas" / "bin",
        ]
        for d in candidates:
            if d.exists() and any(d.glob("cudnn_ops*.dll")):
                return True
    return False


def _gpu_name() -> Optional[str]:
    try:
        import ctranslate2
        dev = ctranslate2.get_cuda_device_count()
        if dev <= 0:
            return None
        # ctranslate2 不直接暴露名字；从 CUDA 驱动查询是可选，尽力而为
        try:
            import torch  # 若有 torch 则拿名字最方便
            if torch.cuda.is_available():
                return torch.cuda.get_device_name(0)
        except Exception as e:
            # torch 未装/加载失败：拿不到名字，退回 CUDA 设备数提示
            logger.debug(f"获取 GPU 名失败（{e}），回退设备数提示")
        # 退而求其次：返回 CUDA 设备数提示
        return f"CUDA device (x{dev})"
    except Exception:
        return None


class WhisperCustomModelRequest(BaseModel):
    name: str
    target: str  # HF repo_id（如 Systran/faster-whisper-large-v3）或本地模型目录路径


@router.get("/whisper_models")
def list_whisper_models():
    """列出内置 + 用户自定义的 whisper 模型映射。"""
    from app.transcriber.whisper_models import get_registry, BUILTIN_WHISPER_MODELS
    reg = get_registry()
    return R.success(data={"builtin": BUILTIN_WHISPER_MODELS, "custom": reg.get_custom_models()})


@router.post("/whisper_models")
def add_whisper_model(data: WhisperCustomModelRequest):
    """新增自定义 whisper 模型映射（名称 → HF repo_id 或本地路径）。"""
    from app.transcriber.whisper_models import get_registry
    try:
        custom = get_registry().add_custom_model(data.name, data.target)
    except ValueError as e:
        return R.error(msg=str(e))
    return R.success(data={"custom": custom}, msg="已添加自定义模型")


@router.delete("/whisper_models/{name}")
def delete_whisper_model(name: str):
    """删除自定义 whisper 模型映射（不会删除已下载的模型文件）。"""
    from app.transcriber.whisper_models import get_registry
    custom = get_registry().remove_custom_model(name)
    return R.success(data={"custom": custom}, msg="已删除自定义模型")


@router.post("/transcriber_config")
def update_transcriber_config(data: TranscriberConfigRequest):
    try:
        transcriber_config_manager.update_config(
            transcriber_type=data.transcriber_type,
            whisper_model_size=data.whisper_model_size,
            openai_asr_id=data.openai_asr_id,
            whisper_device=data.whisper_device,
            whisper_compute_type=data.whisper_compute_type,
            whisper_cpu_threads=data.whisper_cpu_threads,
            whisper_num_workers=data.whisper_num_workers,
        )
    except ValueError as e:
        return R.error(msg=str(e))
    return R.success(data=transcriber_config_manager.get_public_config())


# ---- OpenAI 兼容 ASR 端点管理（spec 003）----

@router.post("/openai_asr")
def add_openai_asr(data: OpenAISREndpointRequest):
    """新增一条 OpenAI 兼容 ASR 端点。"""
    try:
        endpoint_id = transcriber_config_manager.add_openai_asr(
            name=data.name,
            base_url=data.base_url,
            api_key=data.api_key,
            model=data.model,
            timeout=data.timeout if data.timeout is not None else 600,
            chunk_size_mb=data.chunk_size_mb if data.chunk_size_mb is not None else 18,
            api_style=data.api_style or "transcriptions",
            max_speech_chunks=data.max_speech_chunks if data.max_speech_chunks is not None else 120,
            # 静音三兄弟不填默认值：None 表示「跟随通用设置」，由配置层决定是否落盘
            silence_noise_db=data.silence_noise_db,
            min_silence_seconds=data.min_silence_seconds,
            max_rpm=data.max_rpm if data.max_rpm is not None else 30,
            chunk_concurrency=(
                data.chunk_concurrency if data.chunk_concurrency is not None else 4
            ),
            silence_split=data.silence_split,
        )
    except ValueError as e:
        return R.error(msg=str(e))
    return R.success(data={"id": endpoint_id}, msg="已添加")


@router.post("/openai_asr/{endpoint_id}")
def update_openai_asr(endpoint_id: str, data: OpenAISREndpointRequest):
    """更新一条端点；api_key 传打码值（含 *）时视为未改动。"""
    try:
        # 只转发请求里真正出现过的键：没出现的字段保持原值；静音三兄弟传 null
        # 表示「回到跟随通用设置」，因此不能用 None 占位补全。
        endpoint = transcriber_config_manager.update_openai_asr(
            endpoint_id,
            **data.model_dump(exclude_unset=True),
        )
    except ValueError as e:
        return R.error(msg=str(e))
    return R.success(data={"id": endpoint["id"]}, msg="已保存")


@router.delete("/openai_asr/{endpoint_id}")
def delete_openai_asr(endpoint_id: str):
    if not transcriber_config_manager.delete_openai_asr(endpoint_id):
        return R.error(msg="端点不存在")
    return R.success(msg="已删除")


@router.post("/openai_asr/{endpoint_id}/test")
def test_openai_asr(endpoint_id: str):
    """用一段 1s 静音做一次真实转写，验证地址 / 鉴权 / 模型名是否可用。"""
    import os

    endpoint = transcriber_config_manager.get_openai_asr(endpoint_id)
    if not endpoint:
        return R.error(msg="端点不存在")

    from app.transcriber.openai_asr import OpenAIASRTranscriber
    from app.utils.audio_helper import make_silent_wav

    wav_path = None
    try:
        wav_path = make_silent_wav(1.0)
        transcriber = OpenAIASRTranscriber(
            base_url=endpoint["base_url"],
            api_key=endpoint["api_key"],
            model=endpoint["model"],
            # 测试不要按真实超时傻等
            timeout=min(float(endpoint.get("timeout") or 600), 60),
            chunk_size_mb=float(endpoint.get("chunk_size_mb") or 18),
            name=endpoint.get("name") or "",
            api_style=endpoint.get("api_style") or "transcriptions",
            max_speech_chunks=endpoint.get("max_speech_chunks") or 120,
            silence_noise_db=endpoint.get("silence_noise_db") or -30.0,
            min_silence_seconds=endpoint.get("min_silence_seconds") or 0.5,
            max_rpm=endpoint.get("max_rpm") or 30,
            chunk_concurrency=endpoint.get("chunk_concurrency") or 4,
            silence_split=endpoint.get("silence_split"),
        )
        result = transcriber.transcript(file_path=wav_path)
        preview = (result.full_text or "").strip()[:50]
        message = "连接成功" + (f"，静音识别结果：{preview}" if preview else "")
        return R.success(data={"ok": True, "message": message})
    except Exception as e:  # noqa: BLE001 - 测试失败要返回可读原因，不能 500
        logger.warning(f"OpenAI ASR 测试连接失败 (id={endpoint_id}): {e}")
        return R.error(msg=f"连接失败：{e}", data={"ok": False})
    finally:
        if wav_path and os.path.exists(wav_path):
            try:
                os.remove(wav_path)
            except OSError:
                pass


# ---- 全局代理配置（作用于 LLM API + 转写 API + yt-dlp 下载）----

class ProxyConfigRequest(BaseModel):
    enabled: bool
    url: Optional[str] = None


@router.get("/proxy_config")
def get_proxy_config():
    from app.services.proxy_config_manager import ProxyConfigManager
    mgr = ProxyConfigManager()
    cfg = mgr.get_config()
    # effective 给前端展示「当前实际生效的代理」——可能来自配置，也可能来自 env 兜底
    return R.success(data={
        **cfg,
        "effective": mgr.get_proxy_url() or "",
    })


@router.post("/proxy_config")
def update_proxy_config(data: ProxyConfigRequest):
    from app.services.proxy_config_manager import ProxyConfigManager
    mgr = ProxyConfigManager()
    cfg = mgr.update_config(enabled=data.enabled, url=data.url)
    return R.success(data={
        **cfg,
        "effective": mgr.get_proxy_url() or "",
    })


# ---- Whisper 模型下载状态 & 下载触发 ----
# 下载状态（downloading / done / failed + 失败原因）统一交给 model_download_state 维护，
# 「触发下载」与「查询状态」共享同一份进程内内存态。失败原因会随状态接口透传给前端，
# 修复 issue #402 衍生问题：原先只回传 downloading/downloaded，下载失败时前端无任何提示。


def _check_whisper_model_exists(model_size: str, subdir: str = "whisper") -> bool:
    """检查指定 whisper 模型是否已下载完整到本地。

    先把模型名 resolve 成可加载标识，再按类型判定：
      - 本地路径模型 → 直接看该目录下有没有 model.bin
      - HF repo_id → 看 HF cache 布局
        <model_dir>/models--{org}--{name}/snapshots/<hash>/model.bin
        （历史 modelscope 布局 <model_dir>/whisper-{size}/model.bin 也兼容识别）
    """
    from app.transcriber.whisper_models import (
        resolve_whisper_model,
        is_local_target,
        hf_cache_dirname,
    )
    try:
        target = resolve_whisper_model(model_size)
    except Exception:
        return False
    if is_local_target(target):
        return (Path(target) / "model.bin").exists()

    model_dir = Path(get_model_dir(subdir))
    # HF cache 布局（适配任意 org/repo，不再写死 Systran）
    hf_repo_dir = model_dir / hf_cache_dirname(target) / "snapshots"
    if hf_repo_dir.exists():
        for snapshot in hf_repo_dir.iterdir():
            if (snapshot / "model.bin").exists():
                return True
    # 历史 modelscope 布局（向后兼容老用户）
    legacy = model_dir / f"whisper-{model_size}" / "model.bin"
    return legacy.exists()


#: 模型目录扫描结果的缓存 TTL（秒）。取 5s：与前端「下载中每 3s 轮询」同量级，
#: 状态不会晚两个周期才可见。
_MODELS_STATUS_TTL_SECONDS = 5.0

#: 单槽缓存 {at: 上次扫描时刻(monotonic), downloaded: {模型名: 是否已下载}}。
#:
#: **只缓存目录扫描，不缓存下载状态**：下载状态在 dl_state 里本来就是进程内内存态，
#: 每次直读零成本；把它一起缓存只会让「下载中 / 已完成」晚一个周期才可见，那就违背了
#: 本 ticket 的初衷（消除扫描开销，而不是让 UI 变迟钝）。
#:
#: 无锁：单进程 uvicorn 下最坏情况是两个线程各扫一遍，结果相同，没有正确性问题。
_MODELS_STATUS_CACHE: dict[str, Any] = {"at": 0.0, "downloaded": None}


def _invalidate_models_status_cache() -> None:
    """清掉模型目录扫描缓存。

    下载流程收尾时调用（放在 finally 里，成功失败都走），让「这个模型现在在磁盘上了」
    立刻可见，不必等 TTL 到期。
    """
    _MODELS_STATUS_CACHE["at"] = 0.0
    _MODELS_STATUS_CACHE["downloaded"] = None


def _scan_downloaded_models(model_names: list) -> dict:
    """各模型「是否已下载完整」的扫描结果，带 5s 进程内缓存。

    收益来源：_check_whisper_model_exists 对**每个**可见模型都要走一次
    Path.exists()（HF 布局还要 iterdir() 逐 snapshot 探 model.bin）。实测
    /transcriber_models_status p50 6.51ms、stat 86.0/次，而前端在「下载模型进行中」
    每 3s 轮询一次（BillNote_frontend/src/pages/SettingPage/transcriber.tsx:190-197）。
    缓存命中时这一整块扫描归零。

    BILINOTE_CACHE=off 时直通（复用 ticket 01 的判定函数），照旧只跳过「读」。
    """
    now = time.monotonic()
    cached = _MODELS_STATUS_CACHE["downloaded"]
    if (
        cached is not None
        and not task_status_cache.disabled()
        and now - _MODELS_STATUS_CACHE["at"] < _MODELS_STATUS_TTL_SECONDS
    ):
        return cached

    downloaded = {name: _check_whisper_model_exists(name, "whisper") for name in model_names}
    _MODELS_STATUS_CACHE["at"] = time.monotonic()
    _MODELS_STATUS_CACHE["downloaded"] = downloaded
    return downloaded


@router.get("/transcriber_models_status")
def get_transcriber_models_status():
    """返回所有 whisper 模型的下载状态（含用户自定义模型）。"""
    from app.transcriber.whisper_models import get_registry
    names = list(get_registry().visible_model_names())
    downloaded_map = _scan_downloaded_models(names)
    statuses = [dl_state.status_row(size, downloaded_map.get(size, False)) for size in names]

    return R.success(data={
        "whisper": statuses,
    })


class ModelDownloadRequest(BaseModel):
    model_size: str
    transcriber_type: str = "fast-whisper"  # 只有 fast-whisper 有「下载模型」概念


def _friendly_download_error(e: Exception) -> str:
    """把 HuggingFace 的网络类报错翻译成用户能照着做的提示（issue #417）。

    典型原文：'An error happened while trying to locate the file on the Hub and we
    cannot find the requested files in the local cache...' —— 本质是连不上 Hub。
    用户大概率不知道：默认走 hf-mirror.com 镜像，可配代理或改 HF_ENDPOINT。
    """
    raw = str(e)
    lowered = raw.lower()
    network_markers = (
        "locate the file on the hub",
        "couldn't connect",
        "connection error",
        "connecttimeout",
        "read timed out",
        "max retries exceeded",
        "failed to establish",
        "name or service not known",
        "temporary failure in name resolution",
    )
    if any(m in lowered for m in network_markers):
        endpoint = os.getenv("HF_ENDPOINT", "https://huggingface.co")
        return (
            f"{raw}\n"
            f"——连不上模型仓库（当前 HF_ENDPOINT={endpoint}）。可尝试："
            f"1) 在「设置」里配置可用代理；"
            f"2) 设置环境变量 HF_ENDPOINT 切换镜像（国内可用 https://hf-mirror.com）；"
            f"3) 确认容器能访问外网/镜像站后重试。"
        )
    return raw


def _do_download_whisper(model_size: str):
    """后台下载 faster-whisper 模型（支持内置 size / 自定义 repo_id / 本地路径）。

    模型名先 resolve：
      - 本地路径模型：无需下载，目录里有 model.bin 即 done，否则 failed；
      - HF repo_id：snapshot_download 到 HF cache 布局（cache_dir=model_dir），
        与加载逻辑 WhisperModel(download_root=model_dir) 完全对齐。
    """
    from huggingface_hub import snapshot_download
    from app.transcriber.whisper_models import resolve_whisper_model, is_local_target
    from app.services.proxy_config_manager import ProxyConfigManager

    try:
        dl_state.mark_downloading(model_size)
        # 让 UI 配的代理对 HuggingFace 下载也生效（issue #417：容器里代理没生效）
        proxy = ProxyConfigManager().apply_to_env()
        if proxy:
            logger.info(f"whisper 下载走代理: {proxy}")
        model_dir = get_model_dir("whisper")

        # 已经下好就不重复下
        if _check_whisper_model_exists(model_size, "whisper"):
            dl_state.mark_done(model_size)
            return

        target = resolve_whisper_model(model_size)
        if is_local_target(target):
            # 本地模型不下载，只校验 model.bin 是否就位
            ok = (Path(target) / "model.bin").exists()
            if ok:
                dl_state.mark_done(model_size)
            else:
                msg = f"本地模型路径 {target} 下没有 model.bin，无法使用"
                logger.warning(f"本地模型 {model_size}：{msg}")
                dl_state.mark_failed(model_size, msg)
            return

        logger.info(f"开始下载 whisper 模型: {model_size} ← {target}")
        # 跟 faster-whisper utils.py 用同样的 allow_patterns，避免多下无关文件；
        # 不传 local_dir 让它走 HF 默认 cache 布局（与加载逻辑对齐）
        snapshot_download(
            target,
            cache_dir=model_dir,
            allow_patterns=[
                "config.json",
                "preprocessor_config.json",
                "model.bin",
                "tokenizer.json",
                "vocabulary.*",
            ],
        )
        logger.info(f"whisper 模型下载完成: {model_size}")
        dl_state.mark_done(model_size)
    except Exception as e:
        msg = _friendly_download_error(e)
        logger.error(f"whisper 模型下载失败: {model_size}, {e}")
        dl_state.mark_failed(model_size, msg)
    finally:
        _invalidate_models_status_cache()


@router.post("/transcriber_download")
def download_transcriber_model(data: ModelDownloadRequest, background_tasks: BackgroundTasks):
    """触发后台下载指定的 whisper 模型（fast-whisper 支持内置档位 + 自定义模型）。"""
    # fast-whisper：内置档位 / 自定义 repo_id / 本地路径都允许
    from app.transcriber.whisper_models import get_registry
    if not get_registry().is_known(data.model_size):
        return R.error(msg=f"不支持的模型: {data.model_size}（请先在自定义模型中登记）")
    if dl_state.is_downloading(data.model_size):
        return R.success(msg="模型正在下载中")
    background_tasks.add_task(_do_download_whisper, data.model_size)

    return R.success(msg="模型下载已开始")


@router.get("/sys_health")
async def sys_health():
    """结构化健康状态——任何子项异常都不应让整个 endpoint 5xx。

    每个字段：'ok' | 'missing' | 'error'。
    前端 useCheckBackend 用 /sys_check 做存活判定（不依赖外部依赖），
    /sys_health 用来在设置页区分「后端没起」vs「后端起了但 ffmpeg 缺」vs「DB 写不进去」等更细的状态。
    """
    ffmpeg_status = "ok"
    try:
        ensure_ffmpeg_or_raise()
    except Exception:
        ffmpeg_status = "missing"

    db_status = "ok"
    try:
        from app.db.engine import engine
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        db_status = "error"

    # 当前转写器配置 + 模型是否已下载（用 model.bin 落盘判定，与 transcriber 加载逻辑一致）
    whisper_info: dict = {"size": None, "type": None, "downloaded": False, "checked": False}
    try:
        cfg = transcriber_config_manager.get_config()
        size = cfg["whisper_model_size"]
        ttype = cfg["transcriber_type"]
        whisper_info["size"] = size
        whisper_info["type"] = ttype
        # 只有本地引擎才有「下载」概念；在线 ASR 端点跳过
        if ttype == "fast-whisper":
            whisper_info["downloaded"] = _check_whisper_model_exists(size, "whisper")
            whisper_info["checked"] = True
    except Exception:
        pass

    return R.success(data={
        "backend": "ok",
        "ffmpeg": ffmpeg_status,
        "db": db_status,
        "whisper_model": whisper_info,
    })


@router.get("/sys_check")
async def sys_check():
    """轻量存活判定：后端进程能响应这个 endpoint 就算「起来了」，不查外部依赖。

    给桌面端 useCheckBackend / Tauri ready-probe 用。
    """
    return R.success()


@router.get("/deploy_status")
async def deploy_status():
    """返回部署监控所需的所有状态信息。

    所有子项都用 try 包起来——监控页本身不应该被任何一个子项打死。
    特别是 torch：它只在 fast-whisper 路径用得到，用在线 ASR 的
    轻量部署完全可以不装，那种情况这个 endpoint 不应该 500。
    """
    import os

    # CUDA 状态
    try:
        import torch
        cuda_available = torch.cuda.is_available()
        cuda_info = {
            "available": cuda_available,
            "torch_installed": True,
            "version": torch.version.cuda if cuda_available else None,
            "gpu_name": torch.cuda.get_device_name(0) if cuda_available else None,
        }
    except Exception:
        cuda_info = {
            "available": False,
            "torch_installed": False,
            "version": None,
            "gpu_name": None,
        }

    # Whisper 模型 / 转写器配置 + 本地下载状态
    try:
        transcriber_cfg = transcriber_config_manager.get_config()
        size = transcriber_cfg["whisper_model_size"]
        ttype = transcriber_cfg["transcriber_type"]
        if ttype == "fast-whisper":
            downloaded = _check_whisper_model_exists(size, "whisper")
        else:
            downloaded = False  # 在线引擎无下载概念
        whisper_info = {
            "model_size": size,
            "transcriber_type": ttype,
            "downloaded": downloaded,
        }
    except Exception:
        whisper_info = {"model_size": None, "transcriber_type": None, "downloaded": False}

    # FFmpeg 状态
    try:
        ensure_ffmpeg_or_raise()
        ffmpeg_ok = True
    except Exception:
        ffmpeg_ok = False

    return R.success(data={
        "backend": {"status": "running", "port": int(os.getenv("BACKEND_PORT", 8483))},
        "cuda": cuda_info,
        "whisper": whisper_info,
        "ffmpeg": {"available": ffmpeg_ok},
    })