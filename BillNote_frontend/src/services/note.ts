import request from '@/utils/request'
import toast from 'react-hot-toast'
import { translate } from '@/ember/i18n/locale'
import type { TaskDetail } from '@/utils/taskStatus'

export type IntakeItemState =
  | 'ready'
  | 'duplicate'
  | 'invalid'
  | 'unsupported'
  | 'unavailable'
  | 'limit_exceeded'

export interface IntakeItem {
  itemId: string
  clientIndex: number
  rawUrl: string
  canonicalUrl: string | null
  source: 'bilibili' | 'unknown'
  kind: 'video' | 'part' | 'collection' | 'series' | 'unsupported' | 'invalid'
  sourceId: string | null
  parentItemId: string | null
  part: number | null
  state: IntakeItemState
  error: { code: string; messageKey: string; params: Record<string, unknown> } | null
  duplicateOf: string | null
}

export interface IntakeResolveEnvelope {
  schemaVersion: number
  source: string
  summary: {
    inputCount: number
    readyCount: number
    invalidCount: number
    duplicateCount: number
    expandedCount: number
    limits: Record<string, number>
  }
  items: IntakeItem[]
}

export const resolveIntake = async (
  mode: 'single' | 'batch',
  urls: string[]
): Promise<IntakeResolveEnvelope> => {
  return await request.post(
    '/v1/intake/resolve',
    { mode, urls, contractVersion: 1 },
    { suppressToast: true }
  )
}

const API_BASE = String(import.meta.env.VITE_API_BASE_URL || '/api').replace(/\/$/, '')

// 合集展开会为每个视频查询一次分P；188 个视频的真实请求约需 42s，
// 不能使用普通 API 的 30s 全局超时，否则后端已成功完成时前端会先报网络异常。
const EXPAND_TIMEOUT_MS = 120_000

/** 归档接口出错时返回的是 `{code,msg,data}` JSON（HTTP 仍是 200），
 *  直接 `response.blob()` 会把这段 JSON 存成一个坏 zip，所以先看 Content-Type。 */
const readArchiveError = async (response: Response, fallback: string): Promise<never> => {
  const contentType = response.headers.get('Content-Type') || ''
  if (contentType.includes('application/json')) {
    let message = fallback
    try {
      const payload = await response.json()
      message = payload?.msg || message
    } catch {
      // 非 JSON 的代理错误页，保持可读兜底文案
    }
    throw new Error(message)
  }
  throw new Error(fallback)
}

export const downloadNotePdf = async (taskId: string): Promise<Blob> => {
  const base = API_BASE.endsWith('/api') ? API_BASE : `${API_BASE}/api`
  const response = await fetch(`${base}/task_pdf/${encodeURIComponent(taskId)}`)
  if (!response.ok || !(response.headers.get('Content-Type') || '').includes('application/pdf')) {
    return readArchiveError(response, translate('note.pdfFailed'))
  }
  return response.blob()
}

export const downloadNoteArchive = async (taskId: string): Promise<Blob> => {
  const base = API_BASE.endsWith('/api') ? API_BASE : `${API_BASE}/api`
  const response = await fetch(`${base}/task_archive/${encodeURIComponent(taskId)}`)
  if (!response.ok) {
    let message = '笔记归档下载失败'
    try {
      const payload = await response.json()
      message = payload?.msg || message
    } catch {
      // Keep a readable fallback for non-JSON proxy errors.
    }
    throw new Error(message)
  }
  // 后端可能返回 HTTP 200 但 body 是 JSON 错误（例如「没有找到可下载的已完成笔记」）。
  if (!(response.headers.get('Content-Type') || '').includes('application/zip')) {
    return readArchiveError(response, '笔记归档下载失败')
  }
  return response.blob()
}

export const downloadNoteArchives = async (taskIds: string[]): Promise<Blob> => {
  const base = API_BASE.endsWith('/api') ? API_BASE : `${API_BASE}/api`
  const response = await fetch(`${base}/task_archives`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ task_ids: taskIds }),
  })
  if (!response.ok) {
    let message = '批量归档下载失败'
    try {
      const payload = await response.json()
      message = payload?.msg || message
    } catch {
      // Keep a readable fallback for non-JSON proxy errors.
    }
    throw new Error(message)
  }
  // 后端可能返回 HTTP 200 但 body 是 JSON 错误（例如「没有找到可下载的已完成笔记」）。
  if (!(response.headers.get('Content-Type') || '').includes('application/zip')) {
    return readArchiveError(response, '批量归档下载失败')
  }
  return response.blob()
}

/**
 * `POST /generate_note` request body.
 *
 * Named so callers can annotate the object they build against the real contract
 * instead of letting an object literal widen (`grid_size` used to collapse to
 * `number[]` on the way to the store).
 */
export type GenerateNotePayload = {
  video_url: string
  platform: string
  quality: string
  task_id?: string
  format: Array<string>
  scene: string
  detail: string
  extras?: string
  video_understanding?: boolean
  video_interval?: number
  grid_size?: Array<number>
  sampling_mode?: 'interval' | 'smart' | 'hybrid'
  max_frames?: number
  frames_per_second?: number
  frame_limit_mode?: 'total' | 'per_second'
  // spec 008 ticket 02：本次任务是否使用 OCR 辅助。
  // 不传 / null = 沿用「设置 → OCR 辅助」的全局开关。
  ocr_assist?: boolean | null
}

export const generateNote = async (
  data: GenerateNotePayload,
): Promise<{ task_id: string } | null> => {
  try {
    console.log('generateNote', data)
    const response = await request.post('/generate_note', data)

    if (!response) {
      toast.error(translate('note.noData'))
      return null
    }
    toast.success(translate('note.submitted'))

    console.log('res', response)
    // 成功提示

    // 拦截器已把 R.success({"task_id": ...}) 解包成 data（即 { task_id }），
    // axios 静态类型仍是 AxiosResponse，在服务边界收窄一次。
    return response as unknown as { task_id: string }
  } catch (e) {
    console.error('❌ 请求出错', e)

    // 错误提示
    // toast.error('笔记生成失败，请稍后重试')

    throw e // 抛出错误以便调用方处理
  }
}

/**
 * 任务列表行。`GET /api/task_list` 返回的一条记录 —— 只带「列表需要的元信息」，
 * 不带 markdown / transcript 这些重内容（重内容由 `get_task_status` 按需拉）。
 */
export interface TaskListRow {
  task_id: string
  video_id: string
  platform: string
  /** null 表示未分组 */
  group_id: string | null
  /** 未分组时为空串 */
  group_name: string
  /** 结果文件缺失时为空串 */
  title: string
  /** 结果 JSON 存在且 markdown 非空 */
  has_note: boolean
  /** 'YYYY-MM-DD HH:MM:SS' */
  created_at: string
}

/** `GET /api/task_list` 的响应体（request 拦截器已剥掉 `{code,msg,data}` 信封）。 */
export interface TaskListPayload {
  tasks: TaskListRow[]
  total: number
}

/**
 * 拉取任务列表。前端不再做任何本地存储（用户决策 m04951），刷新页面后
 * 任务列表就靠这个方法从后端重建，所以它是「后台重载」而不是用户主动操作：
 * 失败时不弹 toast（suppressToast），交给 taskStore.loadTasks 打 console.warn。
 */
export const listTasks = async (): Promise<TaskListPayload> => {
  return await request.get('/task_list', { suppressToast: true })
}

/**
 * 真正删除一条任务记录（后端会同时删 DB 行与这个任务的产物文件）。
 * 注意：这是按 `task_id` 删；历史上按 video_id+platform 删的 `/delete_task` 已是 no-op 存根。
 * 这里不弹 toast —— 成功/失败的提示由调用方决定。
 */
export const deleteTaskRecord = async (
  task_id: string
): Promise<{ task_id: string; removed: number; files: string[] }> => {
  return await request.post('/delete_task_record', { task_id })
}

/** `POST /api/delete_task_records` 的响应体（spec 010）。 */
export interface BatchDeleteResult {
  /** 真正删掉的 task_id。 */
  deleted: string[]
  /** 没删掉的条目与原因（多数是「任务仍在执行」）。 */
  failed: { task_id: string; error: string }[]
  removed_rows: number
  files: string[]
}

/**
 * 批量删除任务记录（spec 010，前端多选删除）。
 *
 * 后端**逐条隔离**：一条失败不影响其余。调用方必须把 `failed` 如实告诉用户 ——
 * 批量删除最忌讳「看起来成功了，其实有几条没删」。
 */
export const deleteTaskRecords = async (task_ids: string[]): Promise<BatchDeleteResult> => {
  return await request.post('/delete_task_records', { task_ids })
}

export const get_task_status = async (task_id: string) => {
  try {
    // 轮询接口：失败交给 useTaskPolling 做「连续失败」判断，
    // 不在这里弹 toast（3 秒轮一次，弹一次就误导用户以为任务失败了）。
    return await request.get('/task_status/' + task_id, { suppressToast: true })
  } catch (e) {
    console.error('❌ 获取任务状态失败', e)
    throw e // 抛出错误以便调用方处理
  }
}

// ---------------- 批量导入（B 站合集 / 多P） ----------------

export interface ExpandPart {
  p: number
  title: string
  duration: number
  video_id: string
  url: string
  already_generated: boolean
}

export interface ExpandVideo {
  bvid: string
  title: string
  duration: number
  cover: string
  error?: string | null
  parts: ExpandPart[]
}

export interface ExpandResult {
  platform: string
  type: 'video' | 'collection'
  title: string
  cover: string
  owner?: { mid: string; name: string }
  videos: ExpandVideo[]
}

export interface BatchGenerateItem {
  bvid: string
  p: number
  video_id: string
  url: string
  /** 合并任务用它做小节标题（spec 018）；普通批量不需要。 */
  title?: string
}

export interface BatchGenerateResult {
  video_id: string
  task_id?: string
  status: 'queued' | 'skipped' | 'failed'
  reason?: string
}

export interface BatchGeneratePayload {
  platform: string
  items: BatchGenerateItem[]
  quality: string
  format?: Array<string>
  scene?: string
  detail?: string
  extras?: string
  screenshot?: boolean
  link?: boolean
  video_understanding?: boolean
  video_interval?: number
  grid_size?: Array<number>
  sampling_mode?: string
  /** smart 采样：单个视频最多抽多少帧（批量时每个视频各自的上限） */
  max_frames?: number
  /** smart 采样：每秒最多保留多少帧 */
  frames_per_second?: number
  /** smart 限制方式二选一：'total'=只用 max_frames；'per_second'=只用 frames_per_second */
  frame_limit_mode?: string
  /** OCR 辅助：true 强制开、false 强制关、null/缺省跟随全局设置。 */
  ocr_assist?: boolean | null
  /**
   * 批量生成时的分组名：后端会整批新建一个同名笔记组。
   * 非合并模式下必填 —— 组名为空整批会落进「未分组」（spec 009 决策 1），
   * 调用方（BatchImportPanel）已保证非空；合并模式下忽略。
   */
  group_name?: string
  /**
   * spec 018：合并任务 —— 不建组，只产生**一条**任务，把所选视频的笔记合成一篇。
   * 与普通批量的差别见 spec §4 决策 3/6。
   */
  merge?: boolean
  /** 合并笔记标题（merge=true 时生效）；为空时后端回落到 group_name，再回落到「合并笔记」。 */
  merge_title?: string
  /** spec 018 决策 5：合并后再让模型润色一次（默认关）。 */
  merge_polish?: boolean
}

/** 展开合集 / 多P 链接，返回可勾选的视频+分P 列表。 */
export const expandUrl = async (url: string): Promise<ExpandResult> => {
  return await request.post('/expand', { url }, { timeout: EXPAND_TIMEOUT_MS })
}

/**
 * 提交勾选后的批量生成请求。
 *
 * `group_id` 是后端本次整批新建的组 id（`null` = 整批都被去重/在途跳过，
 * 后端没有建组 —— spec 009 决策 2）。前端必须用它把本地任务直接落到正确的组里，
 * 否则新任务会先显示成「未分组」，要等下一次 loadTasks 才跳回正确的组。
 */
export const generateNotes = async (
  payload: BatchGeneratePayload
): Promise<{
  results: BatchGenerateResult[]
  group_id?: string | null
  /** 合并任务（spec 018）：后端只建一条任务，group_id 恒为 null。 */
  merged?: boolean
}> => {
  return await request.post('/generate_notes', payload)
}

// ---------------- 批量状态轮询（spec 009 ticket 06） ----------------

/**
 * `POST /api/task_status/batch` 返回的**轻量**状态 —— 只有进度字段，
 * 不含 markdown / transcript（重内容按需走 `get_task_status` 单任务拉取）。
 */
export interface TaskStatusLite {
  status?: string
  stage?: string | null
  stage_label?: string | null
  stageLabel?: string | null
  progress?: number | null
  stage_progress?: number | null
  stageProgress?: number | null
  message?: string | null
  detail?: TaskDetail | null
  updated_at?: string | null
  updatedAt?: string | null
  revision?: number
}

/**
 * `POST /api/task_status/batch` 的响应体（拦截器已剥掉 `{code,msg,data}` 信封）。
 * 未知 id 的值是 `null`，而不是报错。
 *
 * 后端当前返回的是 `{states: {...}}`（`backend/app/routers/note.py:1451`）。
 * 这里把 `statuses` 也声明成可选并保留，是因为该端点与前端是同批开发的：
 * 只要后端把键名改回 `statuses`（或两侧命名再漂移一次），前端不会静默拿到
 * 空对象、轮询卡死 —— 读取处两个键都认。
 */
export interface TaskStatusBatchPayload {
  states?: Record<string, TaskStatusLite | null>
  statuses?: Record<string, TaskStatusLite | null>
}

/**
 * 从批量响应里取出状态表，兼容 `states` / `statuses` 两种键名。
 * 两个都没有时返回空对象：调用方按「本轮没有状态可更新」处理，保持任务原状态。
 */
export const readTaskStatusMap = (
  payload: TaskStatusBatchPayload | null | undefined
): Record<string, TaskStatusLite | null> => payload?.states ?? payload?.statuses ?? {}

/**
 * 一次拿回多个任务的状态。批量轮询用它，替代逐条 `GET /task_status/{id}`
 * —— 200 个待办任务原本每 3 秒发 200 个串行请求（≈67 req/s），会把单进程后端打爆。
 * 失败交给 useTaskPolling 做「连续失败」判断，这里不弹 toast。
 */
export const getTaskStatusBatch = async (taskIds: string[]): Promise<TaskStatusBatchPayload> => {
  return await request.post('/task_status/batch', { task_ids: taskIds }, { suppressToast: true })
}

export type TaskReadStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'

export interface TaskView {
  taskId: string
  status: TaskReadStatus
  phase: string | null
  progress: number | null
  message: string | null
  detail: TaskDetail | null
  updatedAt: string | null
  revision: number
  error: { code: string; messageKey: string; retryable: boolean } | null
  actions: {
    retry: boolean
    cancel: boolean
    openNote: boolean
    openTranscript: boolean
    chat: boolean
    export: boolean
  }
}

export interface TaskReadModelSnapshot {
  schemaVersion: number
  snapshotRevision: number
  source: 'snapshot' | 'sse' | 'polling' | string
  tasks: TaskView[]
  groups: unknown[]
}

/** Versioned task snapshot adapter; legacy polling remains available during migration. */
export const getTaskReadModelSnapshot = async (
  taskIds: string[]
): Promise<TaskReadModelSnapshot> => {
  const query = taskIds.map(taskId => `task_ids=${encodeURIComponent(taskId)}`).join('&')
  return await request.get(`/v1/task-groups/snapshot${query ? `?${query}` : ''}`, {
    suppressToast: true,
  })
}

export const subscribeTaskReadModel = (
  taskIds: string[],
  onSnapshot: (snapshot: TaskReadModelSnapshot) => void,
  onError?: (event: Event) => void,
  onOpen?: () => void,
  since = 0,
): (() => void) => {
  const query = taskIds.map(taskId => `task_ids=${encodeURIComponent(taskId)}`).join("&")
  const source = new EventSource(`/api/v1/task-groups/events?${query}&since=${since}`)
  source.onopen = () => onOpen?.()
  source.onmessage = event => {
    try {
      onSnapshot({ ...JSON.parse(event.data), source: "sse" })
    } catch (error) {
      console.error("Invalid task event", error)
    }
  }
  source.onerror = event => onError?.(event)
  return () => source.close()
}

/** `POST /api/retry_failed_tasks` 的响应体（spec 011）。 */
export interface RetryFailedResult {
  /** 实际生效的范围。 */
  scope: 'all' | 'group'
  /** `scope === 'group'` 时的分组；`null` 表示「未分组」。 */
  group_id: string | null
  /** 范围内扫到的任务条数（不是「失败任务」条数）。 */
  candidates: number
  /** 成功重新入队的 task_id。 */
  requeued: string[]
  /** 被跳过的任务及原因（已完成 / 仍在执行 / 参数缺失 …）。 */
  skipped: { task_id: string; reason: string }[]
  /** 分组里列出来、但数据库里已不存在的 task_id。 */
  missing: string[]
}

/**
 * 批量重试失败的任务（spec 011）。
 *
 * **范围由后端算**：`scope: 'all'` 是整库，`scope: 'group'` 时 `groupId` 决定是哪个组
 * （传 `null` = 未分组）。前端只把「当前可见范围」翻译成一次请求，不自己筛一遍 ——
 * 两处各判一次必然会漂移，而漂移的表现是「点了重试但什么都没发生」。
 *
 * 只有 **FAILED** 的任务会真的重跑：SUCCESS 重跑会覆盖用户已看过的笔记，在途任务重跑
 * 会让同一个 task_id 被两个线程同时写。两者都会出现在 `skipped` 里，带上原因。
 *
 * 失败**不**静默：让调用方拿到 rejected 的 promise 并自己决定怎么提示。
 */
export const retryFailedTasks = async (
  scope: 'all' | 'group',
  groupId?: string | null
): Promise<RetryFailedResult> => {
  return await request.post('/retry_failed_tasks', {
    scope,
    group_id: scope === 'group' ? (groupId ?? null) : null,
  })
}
