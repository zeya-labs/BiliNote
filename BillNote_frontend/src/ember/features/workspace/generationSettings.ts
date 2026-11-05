/**
 * 生成参数的共享定义（spec 020）。
 *
 * 单集表单与批量面板共用同一套设置：类型、默认值、以及「UI 三态 → 接口契约」
 * 的折算都放在这里，组件文件只导出组件本身（react-refresh 的约束）。
 */
/**
 * 一次生成要用的全部参数（spec 020）。
 *
 * 单集表单和批量面板共用同一套设置：用户抱怨「批量页面调不了生成设置」，
 * 根因是这块 JSX 只长在 IntakeForm 里。抽成受控组件后两处渲染的是同一个面板，
 * 以后加一个参数也不会再漏掉批量。
 */
export type GenerationSettings = {
  quality: 'fast' | 'medium' | 'slow'
  scene: string
  detail: string
  format: string[]
  extras: string
  video_understanding: boolean
  video_interval: number
  sampling_mode: 'interval' | 'smart' | 'hybrid'
  max_frames: number
  frames_per_second: number
  frame_limit_mode: 'total' | 'per_second'
  /** 三态：global=跟随设置页的 OCR 全局开关。 */
  ocr_assist: 'global' | 'on' | 'off'
  grid_size: [number, number]
}

export const DEFAULT_GENERATION_SETTINGS: GenerationSettings = {
  quality: 'medium',
  scene: 'life',
  detail: 'detailed',
  format: [],
  extras: '',
  video_understanding: true,
  video_interval: 6,
  sampling_mode: 'smart',
  max_frames: 60,
  frames_per_second: 2,
  frame_limit_mode: 'total',
  ocr_assist: 'global',
  grid_size: [2, 2],
}

/** 把三态 ocr_assist 折成接口契约里的 true / false / null（null = 跟随全局）。 */
export const toPayloadSettings = (settings: GenerationSettings) => ({
  quality: settings.quality,
  scene: settings.scene,
  detail: settings.detail,
  format: settings.format,
  extras: settings.extras,
  video_understanding: settings.video_understanding,
  video_interval: settings.video_interval,
  sampling_mode: settings.sampling_mode,
  max_frames: settings.max_frames,
  frames_per_second: settings.frames_per_second,
  frame_limit_mode: settings.frame_limit_mode,
  grid_size: settings.grid_size,
  ocr_assist: settings.ocr_assist === 'global' ? null : settings.ocr_assist === 'on',
})
