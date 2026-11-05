import json
import os
import uuid
from pathlib import Path
from typing import Optional, Dict, Any, List

# OpenAI 兼容 ASR 端点的默认参数
DEFAULT_ASR_TIMEOUT = 600          # 秒，长音频给足时间
DEFAULT_ASR_CHUNK_MB = 18          # 超过该大小就切割成多段分别转写
DEFAULT_SPEECH_MAX_CHUNKS = 120    # 按说话切片时最多切多少段
DEFAULT_SILENCE_NOISE_DB = -30.0   # silencedetect 噪声门限（dB）
DEFAULT_MIN_SILENCE_SECONDS = 0.5  # 多长的停顿算一次「说完了」
DEFAULT_ASR_MAX_RPM = 30           # 每分钟最多请求次数（防 429）
DEFAULT_ASR_CHUNK_CONCURRENCY = 4  # 切片转写的并发请求数（1-32，1 即串行）
ASR_CHUNK_CONCURRENCY_MAX = 32
DEFAULT_ASR_SILENCE_SPLIT = True   # chat 风格是否按静音边界切片（关掉改等长切割）

def _global_limits():
    """延迟 import 运行时限额门面。

    本模块是纯配置读写（不 import 任何 app.* 包），而 runtime_limits 会间接触发
    数据库初始化；把它拖进函数体，既避免导入环，也让「只读配置文件」的场景
    （比如命令行工具）不必建库。
    """
    from app.services import runtime_limits

    return runtime_limits


# 允许通过 update_openai_asr 修改的字段
_ASR_EDITABLE_FIELDS = (
    "name", "base_url", "api_key", "model", "timeout", "chunk_size_mb", "api_style",
    "max_speech_chunks", "silence_noise_db", "min_silence_seconds", "max_rpm",
    "chunk_concurrency", "silence_split",
)

# 这三个字段支持「跟随通用设置」：没被显式指定时**不落盘**，读取时实时回落到
# 「设置 → 通用」里的全局静音参数。以前它们会在保存时被默认值填满并持久化，
# 于是第一次保存就把当时的全局值冻结成端点值，之后改全局永远不生效。
_SILENCE_FIELDS = ("silence_split", "silence_noise_db", "min_silence_seconds")

# 接口风格：transcriptions=Whisper 风格 /audio/transcriptions；chat=chat/completions + input_audio
ASR_API_STYLES = ("transcriptions", "chat", "dashscope")


def _as_bool(value, default: bool) -> bool:
    """把前端传来的布尔/字符串统一成 bool。

    前端开关可能以 ``"true"`` / ``"false"`` 的形式回来，直接 ``bool("false")``
    会得到 True，所以这里显式认识字符串形式。
    """
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _mask_key(key: Optional[str]) -> str:
    """API Key 打码，规则与 ProviderService.mask_key 保持一致。"""
    if not key:
        return ""
    key = str(key)
    if len(key) < 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


class TranscriberConfigManager:
    """管理转写器配置，存储在 JSON 文件中，支持前端动态修改。"""

    def __init__(self, filepath: str = "config/transcriber.json"):
        self.path = Path(filepath)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ---------------- 底层读写 ----------------

    def _read(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _write(self, data: Dict[str, Any]):
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ---------------- OpenAI 兼容 ASR 端点 ----------------

    @staticmethod
    def _normalize_endpoint(raw: Dict[str, Any]) -> Dict[str, Any]:
        """补齐缺省字段，保证前端/转写器拿到结构一致的 dict。

        timeout / chunk_size_mb 只在「缺失」时补默认值——0 或非法值要留给
        `_validate_endpoint` 报错，不能悄悄被 `or` 吞成默认值。
        """
        raw = raw or {}
        explicit = {
            str(name)
            for name in (raw.get("explicit_fields") or [])
            if str(name) in _SILENCE_FIELDS
        }
        timeout = raw.get("timeout")
        if timeout is None or timeout == "":
            timeout = DEFAULT_ASR_TIMEOUT
        # 兼容旧配置：以前叫 max_upload_mb（压缩阈值），现在叫 chunk_size_mb（切割阈值）
        chunk_size_mb = raw.get("chunk_size_mb", raw.get("max_upload_mb"))
        if chunk_size_mb is None or chunk_size_mb == "":
            chunk_size_mb = DEFAULT_ASR_CHUNK_MB
        return {
            "id": str(raw.get("id") or ""),
            "name": str(raw.get("name") or ""),
            "base_url": str(raw.get("base_url") or ""),
            "api_key": str(raw.get("api_key") or ""),
            "model": str(raw.get("model") or ""),
            "timeout": timeout,
            "chunk_size_mb": chunk_size_mb,
            "api_style": str(raw.get("api_style") or "transcriptions"),
            "max_speech_chunks": _as_int(
                raw.get("max_speech_chunks"), DEFAULT_SPEECH_MAX_CHUNKS
            ),
            # 端点没显式配置时回落到「设置 → 通用」里的全局静音参数：用户在通用页
            # 调一次，所有未单独覆盖的端点一起生效（端点级配置永远优先）。
            "silence_noise_db": _as_float(
                raw.get("silence_noise_db") if "silence_noise_db" in explicit else None,
                _global_limits().asr_silence_noise_db(),
            ),
            "min_silence_seconds": _as_float(
                raw.get("min_silence_seconds") if "min_silence_seconds" in explicit else None,
                _global_limits().asr_min_silence_seconds(),
            ),
            "max_rpm": _as_int(raw.get("max_rpm"), DEFAULT_ASR_MAX_RPM),
            # 并发数按范围收敛，避免旧配置/手改配置把并发开到荒谬的值
            "chunk_concurrency": max(
                1,
                min(
                    ASR_CHUNK_CONCURRENCY_MAX,
                    _as_int(raw.get("chunk_concurrency"), DEFAULT_ASR_CHUNK_CONCURRENCY),
                ),
            ),
            # chat 风格（MiMo）默认按静音边界切片；关掉后走「按字节等长切片」，
            # 省掉整个音频解码 + silencedetect 的耗时。
            "silence_split": _as_bool(
                raw.get("silence_split") if "silence_split" in explicit else None,
                _global_limits().asr_silence_split(),
            ),
            # 哪些静音字段是用户显式设过的（前端据此显示「跟随通用设置」状态）
            "explicit_fields": sorted(explicit),
        }

    @staticmethod
    def _persistable(endpoint: Dict[str, Any]) -> Dict[str, Any]:
        """写入磁盘前的形态：未显式指定的静音字段不落盘。

        「跟随全局」必须在文件里表现为「键不存在」——存了就会在下次读取时被
        当成端点显式值，全局改动从此不再生效。
        """
        explicit = set(endpoint.get("explicit_fields") or []) & set(_SILENCE_FIELDS)
        clean = {k: v for k, v in endpoint.items() if k not in _SILENCE_FIELDS}
        clean["explicit_fields"] = sorted(explicit)
        for name in _SILENCE_FIELDS:
            if name in explicit:
                clean[name] = endpoint[name]
        return clean

    def _endpoints(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        raw = data.get("openai_asr_endpoints") or []
        if not isinstance(raw, list):
            return []
        return [self._normalize_endpoint(e) for e in raw if isinstance(e, dict)]

    @staticmethod
    def _validate_endpoint(endpoint: Dict[str, Any]):
        for field, label in (
            ("name", "名称"),
            ("base_url", "服务地址"),
            ("api_key", "API Key"),
            ("model", "模型名"),
        ):
            if not str(endpoint.get(field) or "").strip():
                raise ValueError(f"{label}不能为空")
        try:
            timeout = int(endpoint.get("timeout"))
        except (TypeError, ValueError):
            raise ValueError("超时时间必须为正整数")
        if timeout <= 0:
            raise ValueError("超时时间必须大于 0")

        try:
            chunk_size_mb = float(endpoint.get("chunk_size_mb"))
        except (TypeError, ValueError):
            raise ValueError("切割阈值必须为正数")
        if chunk_size_mb <= 0:
            raise ValueError("切割阈值必须大于 0")

        if str(endpoint.get("api_style") or "") not in ASR_API_STYLES:
            raise ValueError("接口类型只能是 transcriptions、chat 或 dashscope")

        try:
            if int(endpoint.get("max_speech_chunks")) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("最多切片数必须为正整数")
        try:
            if float(endpoint.get("min_silence_seconds")) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("最短静音时长必须大于 0")
        try:
            if int(endpoint.get("max_rpm")) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("每分钟请求数必须为正整数")
        try:
            if int(endpoint.get("chunk_concurrency")) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("切片并发数必须为正整数")

    def list_openai_asr(self) -> List[Dict[str, Any]]:
        return self._endpoints(self._read())

    def get_openai_asr(self, endpoint_id: str) -> Optional[Dict[str, Any]]:
        for endpoint in self.list_openai_asr():
            if endpoint["id"] == endpoint_id:
                return endpoint
        return None

    def get_active_openai_asr(self) -> Optional[Dict[str, Any]]:
        endpoint_id = self.get_config().get("openai_asr_id")
        if not endpoint_id:
            return None
        return self.get_openai_asr(endpoint_id)

    def add_openai_asr(
        self,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = DEFAULT_ASR_TIMEOUT,
        chunk_size_mb: float = DEFAULT_ASR_CHUNK_MB,
        api_style: str = "transcriptions",
        max_speech_chunks: int = DEFAULT_SPEECH_MAX_CHUNKS,
        silence_noise_db: Optional[float] = None,
        min_silence_seconds: Optional[float] = None,
        max_rpm: int = DEFAULT_ASR_MAX_RPM,
        chunk_concurrency: int = DEFAULT_ASR_CHUNK_CONCURRENCY,
        silence_split: Optional[bool] = None,
    ) -> str:
        # 静音三兄弟默认 None = 「跟随通用设置」：不传就不落盘，读取时回落全局值
        payload: Dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "name": name,
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "timeout": timeout,
            "chunk_size_mb": chunk_size_mb,
            "api_style": api_style,
            "max_speech_chunks": max_speech_chunks,
            "max_rpm": max_rpm,
            "chunk_concurrency": chunk_concurrency,
        }
        explicit: List[str] = []
        if silence_noise_db is not None:
            payload["silence_noise_db"] = silence_noise_db
            explicit.append("silence_noise_db")
        if min_silence_seconds is not None:
            payload["min_silence_seconds"] = min_silence_seconds
            explicit.append("min_silence_seconds")
        if silence_split is not None:
            payload["silence_split"] = silence_split
            explicit.append("silence_split")
        payload["explicit_fields"] = explicit
        endpoint = self._normalize_endpoint(payload)
        self._validate_endpoint(endpoint)

        data = self._read()
        endpoints = self._endpoints(data)
        endpoints.append(endpoint)
        data["openai_asr_endpoints"] = [self._persistable(e) for e in endpoints]
        # 第一条自动成为当前使用，省得用户还要再点一次「使用」
        if not data.get("openai_asr_id"):
            data["openai_asr_id"] = endpoint["id"]
        self._write(data)
        return endpoint["id"]

    def update_openai_asr(self, endpoint_id: str, **fields) -> Dict[str, Any]:
        data = self._read()
        endpoints = self._endpoints(data)
        target = next((e for e in endpoints if e["id"] == endpoint_id), None)
        if target is None:
            raise ValueError("端点不存在")

        for key in _ASR_EDITABLE_FIELDS:
            if key not in fields:
                continue
            value = fields[key]
            if value is None:
                continue
            # 前端回显的是打码后的 key，带 * 视为未改动，不能覆盖真实值
            if key == "api_key" and "*" in str(value):
                continue
            target[key] = value

        # 静音三兄弟：传 null 表示「回到跟随通用设置」（从文件里摘掉），
        # 传值表示端点级显式覆盖。其余字段传 null 仍然等于「未改动」。
        explicit = set(target.get("explicit_fields") or [])
        for name in _SILENCE_FIELDS:
            if name not in fields:
                continue
            if fields[name] is None:
                explicit.discard(name)
                target.pop(name, None)
            else:
                explicit.add(name)
        target["explicit_fields"] = sorted(explicit)

        target = self._normalize_endpoint(target)
        self._validate_endpoint(target)

        data["openai_asr_endpoints"] = [
            self._persistable(target if e["id"] == endpoint_id else e)
            for e in endpoints
        ]
        self._write(data)
        return target

    def delete_openai_asr(self, endpoint_id: str) -> bool:
        data = self._read()
        endpoints = self._endpoints(data)
        remaining = [e for e in endpoints if e["id"] != endpoint_id]
        if len(remaining) == len(endpoints):
            return False
        data["openai_asr_endpoints"] = [self._persistable(e) for e in remaining]
        # 删掉的正好是当前使用的 → 清空选择，让用户重新选
        if data.get("openai_asr_id") == endpoint_id:
            data.pop("openai_asr_id", None)
        self._write(data)
        return True

    # ---------------- 顶层配置 ----------------

    def get_config(self) -> Dict[str, Any]:
        """获取当前转写器配置，fallback 到环境变量默认值。

        whisper 默认 size 从 'medium' (~1.5GB) 改为 'tiny' (~75MB)：
        新装用户没主动设置时不应该被首次下载卡住。想要更高精度可在「音频转写配置」
        页主动切换。
        """
        data = self._read()
        return {
            "transcriber_type": data.get(
                "transcriber_type",
                os.getenv("TRANSCRIBER_TYPE", "fast-whisper"),
            ),
            "whisper_model_size": data.get(
                "whisper_model_size",
                os.getenv("WHISPER_MODEL_SIZE", "tiny"),
            ),
            # 本地转写性能相关配置（仅 fast-whisper 生效）。
            # whisper_device: auto=自动探测（有 CUDA 用 GPU，否则 CPU）；cpu/cuda 强制。
            # whisper_compute_type: auto=按设备选（CUDA→float16，CPU→int8）；也可显式指定。
            # whisper_cpu_threads: CPU 推理线程数，0=自动（用物理核心数）。GPU 下忽略。
            # whisper_num_workers: faster-whisper 推理 worker 数，>1 时多任务可真正并行。
            "whisper_device": data.get(
                "whisper_device", os.getenv("WHISPER_DEVICE", "auto")
            ),
            "whisper_compute_type": data.get(
                "whisper_compute_type", os.getenv("WHISPER_COMPUTE_TYPE", "auto")
            ),
            "whisper_cpu_threads": _as_int(
                data.get("whisper_cpu_threads"),
                int(os.getenv("WHISPER_CPU_THREADS", "0")),
            ),
            "whisper_num_workers": _as_int(
                data.get("whisper_num_workers"),
                int(os.getenv("WHISPER_NUM_WORKERS", "1")),
            ),
            "openai_asr_id": data.get("openai_asr_id") or None,
            "openai_asr_endpoints": self._endpoints(data),
        }

    def get_public_config(self) -> Dict[str, Any]:
        """给前端的版本：api_key 打码，避免明文回传。"""
        config = self.get_config()
        config["openai_asr_endpoints"] = [
            {**endpoint, "api_key": _mask_key(endpoint.get("api_key"))}
            for endpoint in config["openai_asr_endpoints"]
        ]
        return config

    def update_config(
        self,
        transcriber_type: str,
        whisper_model_size: Optional[str] = None,
        openai_asr_id: Optional[str] = None,
        whisper_device: Optional[str] = None,
        whisper_compute_type: Optional[str] = None,
        whisper_cpu_threads: Optional[int] = None,
        whisper_num_workers: Optional[int] = None,
    ) -> Dict[str, Any]:
        """更新转写器配置并持久化。

        whisper_* 性能参数只在「非 None」时写入，避免调用方漏传把用户改好的值重置。
        """
        data = self._read()
        data["transcriber_type"] = transcriber_type
        if whisper_model_size is not None:
            data["whisper_model_size"] = whisper_model_size
        if openai_asr_id is not None:
            data["openai_asr_id"] = openai_asr_id
        if whisper_device is not None:
            if whisper_device not in ("auto", "cpu", "cuda"):
                raise ValueError("本地转写设备只能是 auto / cpu / cuda")
            data["whisper_device"] = whisper_device
        if whisper_compute_type is not None:
            data["whisper_compute_type"] = whisper_compute_type
        if whisper_cpu_threads is not None:
            data["whisper_cpu_threads"] = max(0, int(whisper_cpu_threads))
        if whisper_num_workers is not None:
            data["whisper_num_workers"] = max(1, int(whisper_num_workers))
        self._write(data)
        return self.get_config()

    def get_transcriber_type(self) -> str:
        return self.get_config()["transcriber_type"]

    def get_whisper_model_size(self) -> str:
        return self.get_config()["whisper_model_size"]

    def get_whisper_device(self) -> str:
        return self.get_config()["whisper_device"]

    def get_whisper_compute_type(self) -> str:
        return self.get_config()["whisper_compute_type"]

    def get_whisper_cpu_threads(self) -> int:
        return self.get_config()["whisper_cpu_threads"]

    def get_whisper_num_workers(self) -> int:
        return self.get_config()["whisper_num_workers"]

    def is_model_ready(self) -> Dict[str, Any]:
        """当前转写器是否就绪可用。

        返回 {ready, transcriber_type, model_size, downloading, reason}：
          - openai-asr：需要至少配置并选中一条端点
          - fast-whisper：检查 whisper-{size}/model.bin 落盘
        给 /generate_note 入口做「开始视频前先确认模型下载好」的门禁用。
        """
        cfg = self.get_config()
        ttype = cfg["transcriber_type"]
        size = cfg["whisper_model_size"]
        result = {
            "ready": True,
            "transcriber_type": ttype,
            "model_size": size,
            "downloading": False,
            "reason": "",
        }

        if ttype == "openai-asr":
            if not cfg.get("openai_asr_endpoints"):
                result["ready"] = False
                result["reason"] = "尚未配置 OpenAI 兼容 ASR，请在「设置 → 音频转写配置」添加"
            elif not self.get_active_openai_asr():
                result["ready"] = False
                result["reason"] = "尚未选择要使用的 OpenAI 兼容 ASR，请在「设置 → 音频转写配置」选择"
            return result

        if ttype != "fast-whisper":
            return result  # 在线端点无需本地模型

        # 延迟 import 避免与 routers.config 的循环依赖；只取纯函数，不触发路由副作用
        try:
            from app.routers.config import (
                _check_whisper_model_exists,
                _downloading,
            )
        except Exception as e:
            # 拿不到检查函数时保守放行，不要把用户卡死
            result["reason"] = f"无法检查模型状态: {e}"
            return result

        if ttype == "fast-whisper":
            downloaded = _check_whisper_model_exists(size, "whisper")
            downloading = _downloading.get(size) == "downloading"

        result["downloading"] = downloading
        if downloaded:
            return result
        result["ready"] = False
        result["reason"] = (
            f"转写模型 {ttype} / {size} 尚未下载就绪"
            + ("，正在下载中，请稍候" if downloading else "，请先在「设置 → 音频转写配置」页下载")
        )
        return result
