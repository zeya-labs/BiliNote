import request from '@/utils/request'

export type OpenAIASRStyle = 'transcriptions' | 'chat' | 'dashscope'

export interface OpenAIASREndpoint {
  id: string
  name: string
  base_url: string
  /** GET 返回的是打码值（带 *），保存时原样回传即代表不修改 */
  api_key: string
  model: string
  timeout: number
  /** 切割阈值（MB）：音频超过该大小会切成多段分别转写 */
  chunk_size_mb: number
  /** transcriptions=Whisper 风格 /audio/transcriptions；chat=chat/completions + input_audio（MiMo 等） */
  api_style: OpenAIASRStyle
  /** 按说话（静音边界）切片时最多切多少段 */
  max_speech_chunks: number
  /** silencedetect 噪声门限 dB */
  silence_noise_db: number
  /** 多长的停顿算一次「说完了」（秒） */
  min_silence_seconds: number
  /** 每分钟最多请求次数，防止触发服务端 429 */
  max_rpm: number
  /** 切片转写的并发请求数（1-32，1 即串行）；并发只填满 max_rpm 允许的额度 */
  chunk_concurrency: number
  /**
   * chat 风格是否按静音边界切片（默认 true）。
   * 关掉可省去「完整解码音频 + silencedetect」这一整轮耗时，改走按字节等长切片。
   */
  silence_split: boolean
  /**
   * 用户显式设过的静音字段（silence_split / silence_noise_db / min_silence_seconds）。
   * 不在列表里 = 该字段跟随「设置 → 通用」的全局值，改全局会立刻影响这个端点。
   */
  explicit_fields?: string[]
}

export interface OpenAIASREndpointInput {
  name: string
  base_url: string
  /** 编辑时可省略或留空：后端把带 * 的掩码值当成「不修改」 */
  api_key?: string
  model: string
  timeout?: number
  chunk_size_mb?: number
  api_style?: OpenAIASRStyle
  max_speech_chunks?: number
  /** 传 null = 撤销端点级覆盖，重新跟随「设置 → 通用」的全局值 */
  silence_noise_db?: number | null
  min_silence_seconds?: number | null
  max_rpm?: number
  chunk_concurrency?: number
  /** 传 null = 撤销端点级覆盖，重新跟随全局开关 */
  silence_split?: boolean | null
}

export interface TranscriberConfig {
  transcriber_type: string
  whisper_model_size: string
  /** 本地推理设备：auto / cpu / cuda（仅 fast-whisper 生效） */
  whisper_device?: string
  /** 计算类型：auto / float16 / int8 / int8_float16 ...（仅 fast-whisper） */
  whisper_compute_type?: string
  /** CPU 推理线程数，0 = 自动（仅 fast-whisper） */
  whisper_cpu_threads?: number
  /** faster-whisper 推理 worker 数（仅 fast-whisper） */
  whisper_num_workers?: number
  /** 本机推理硬件能力探测（仅配置读取时下发） */
  local_hardware?: {
    cpu_cores: number
    cuda_available: boolean
    gpu_name?: string | null
    cuda_dll_ready: boolean
    device: string
  }
  available_types: { value: string; label: string }[]
  whisper_model_sizes: string[]
  /** 内置模型映射：size → HF repo_id */
  whisper_builtin_models?: Record<string, string>
  /** 用户自定义模型映射：名称 → HF repo_id 或本地路径 */
  whisper_custom_models?: Record<string, string>
  /** OpenAI 兼容 ASR：当前使用的那条 */
  openai_asr_id?: string | null
  /** OpenAI 兼容 ASR：全部端点（api_key 已打码） */
  openai_asr_endpoints?: OpenAIASREndpoint[]
}

export interface ModelStatus {
  model_size: string
  downloaded: boolean
  downloading: boolean
  /** 后台下载失败（仓库 404、网络中断、本地路径缺 model.bin 等）。后端从此字段透传 */
  failed?: boolean
  /** 下载失败时的原因（仅 failed 时存在），用于前端提示 */
  error?: string
}

export interface ModelsStatusResponse {
  /** 只剩本地 faster-whisper 有「下载」概念；在线 ASR 端点无需下载 */
  whisper: ModelStatus[]
}

export const getTranscriberConfig = async (): Promise<TranscriberConfig> => {
  return await request.get('/transcriber_config')
}

export const updateTranscriberConfig = async (data: {
  transcriber_type: string
  whisper_model_size?: string
  openai_asr_id?: string | null
  whisper_device?: string
  whisper_compute_type?: string
  whisper_cpu_threads?: number
  whisper_num_workers?: number
}) => {
  return await request.post('/transcriber_config', data)
}

// ---------------- OpenAI 兼容 ASR 端点（spec 003） ----------------

export const addOpenaiAsr = async (data: OpenAIASREndpointInput): Promise<{ id: string }> => {
  return await request.post('/openai_asr', data)
}

export const updateOpenaiAsr = async (id: string, data: OpenAIASREndpointInput) => {
  return await request.post(`/openai_asr/${encodeURIComponent(id)}`, data)
}

export const deleteOpenaiAsr = async (id: string) => {
  return await request.delete(`/openai_asr/${encodeURIComponent(id)}`)
}

export const testOpenaiAsr = async (id: string) => {
  // suppressToast：失败信息由调用方用更友好的方式展示，避免全局红 toast 重复
  return await request.post(
    `/openai_asr/${encodeURIComponent(id)}/test`,
    {},
    { suppressToast: true }
  )
}

export const getModelsStatus = async (): Promise<ModelsStatusResponse> => {
  return await request.get('/transcriber_models_status')
}

export const downloadModel = async (data: { model_size: string; transcriber_type?: string }) => {
  return await request.post('/transcriber_download', data)
}

export interface WhisperModelsResponse {
  builtin: Record<string, string>
  custom: Record<string, string>
}

/** 列出内置 + 自定义 whisper 模型映射 */
export const listWhisperModels = async (): Promise<WhisperModelsResponse> => {
  return await request.get('/whisper_models')
}

/** 新增自定义模型映射（名称 → HF repo_id 或本地路径） */
export const addWhisperModel = async (data: { name: string; target: string }) => {
  return await request.post('/whisper_models', data)
}

/** 删除自定义模型映射（不会删除已下载的模型文件） */
export const deleteWhisperModel = async (name: string) => {
  return await request.delete(`/whisper_models/${encodeURIComponent(name)}`)
}
