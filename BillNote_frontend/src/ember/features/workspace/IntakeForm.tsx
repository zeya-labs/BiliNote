import { useEffect, useRef, useState } from 'react'
import { useForm, useWatch } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { Link2, Sparkles, SlidersHorizontal } from 'lucide-react'
import toast from 'react-hot-toast'
import { useNavigate } from 'react-router-dom'
import { expandUrl, generateNote } from '@/services/note'
import type { GenerateNotePayload } from '@/services/note'
import { localizedError } from '@/utils/request'
import { useTaskStore } from '@/store/taskStore'
import { useI18n } from '@/ember/i18n/context'
import type { MessageKey } from '@/ember/i18n/strings'
import { Button } from '@/ember/ui/Button'
import { Card } from '@/ember/ui/Card'
import { Field } from '@/ember/ui/Field'
import { Input } from '@/ember/ui/Input'
import { GenerationOptions } from './GenerationOptions'
import { DEFAULT_GENERATION_SETTINGS } from './generationSettings'
import type { GenerationSettings } from './generationSettings'

/** Users paste links without a scheme; default to https when none is present. */
const withScheme = (url: string) => (/^[a-z][a-z0-9+.-]*:\/\//i.test(url) ? url : `https://${url}`)


const formSchema = z
  .object({
    video_url: z.string().optional(),
    platform: z.string().default('bilibili'),
    quality: z.enum(['fast', 'medium', 'slow']).default('medium'),
    format: z.array(z.string()).default([]),
    scene: z.string().default('life'),
    detail: z.string().default('detailed'),
    extras: z.string().optional(),
    video_understanding: z.boolean().default(true),
    video_interval: z.coerce.number().min(1).max(30).default(6),
    sampling_mode: z.enum(['interval', 'smart', 'hybrid']).default('smart'),
    max_frames: z.coerce.number().min(5).default(60),
    frames_per_second: z.coerce.number().min(1).max(10).default(2),
    frame_limit_mode: z.enum(['total', 'per_second']).default('total'),
    ocr_assist: z.enum(['global', 'on', 'off']).default('global'),
    grid_size: z
      .tuple([z.coerce.number().min(1).max(10), z.coerce.number().min(1).max(10)])
      .default([2, 2]),
  })
  .superRefine(({ video_url }, ctx) => {
    if (!video_url) {
      ctx.addIssue({ code: 'custom', message: 'required', path: ['video_url'] })
      return
    }
    try {
      const url = new URL(withScheme(video_url))
      if (!['http:', 'https:'].includes(url.protocol)) throw new Error('protocol')
    } catch {
      ctx.addIssue({ code: 'custom', message: 'invalid', path: ['video_url'] })
    }
  })

type FormInput = z.input<typeof formSchema>
type FormValues = z.output<typeof formSchema>

/** 探测去抖：粘贴后不要每敲一个字符就打一次 /expand。 */
const PART_PROBE_DELAY_MS = 600

type Props = {
  /**
   * 单集模式探测到「这条链接其实是多P 视频」时的回调（spec 018 决策 2）。
   * 由 IntakePanel 负责切到批量面板并把链接带过去 —— 表单自己不知道批量面板
   * 怎么挂载，也不该知道。
   */
  onMultiPart?: (url: string) => void
}

/**
 * The intake form. Two visual states share one surface: a compact "paste a link"
 * resting state and an expanded options panel. The URL field stays mounted
 * across both so switching never loses what the user typed.
 */
export function IntakeForm({ onMultiPart }: Props = {}) {
  const { t } = useI18n()
  const navigate = useNavigate()
  const addPendingTask = useTaskStore(state => state.addPendingTask)
  const [showOptions, setShowOptions] = useState(false)

  const form = useForm<FormInput, unknown, FormValues>({
    resolver: zodResolver(formSchema),
    defaultValues: {
      platform: 'bilibili',
      quality: 'medium',
      scene: 'life',
      detail: 'detailed',
      video_understanding: true,
      video_interval: 6,
      sampling_mode: 'smart',
      max_frames: 60,
      frames_per_second: 2,
      frame_limit_mode: 'total',
      ocr_assist: 'global',
      grid_size: [2, 2],
      format: [],
    },
  })

  // 设置面板是受控组件（spec 020）：表单仍是唯一数据源，面板只负责把改动写回。
  const watched = useWatch({ control: form.control })
  const settings: GenerationSettings = {
    quality: (watched.quality as GenerationSettings['quality']) ?? DEFAULT_GENERATION_SETTINGS.quality,
    scene: watched.scene ?? DEFAULT_GENERATION_SETTINGS.scene,
    detail: watched.detail ?? DEFAULT_GENERATION_SETTINGS.detail,
    format: watched.format ?? DEFAULT_GENERATION_SETTINGS.format,
    extras: watched.extras ?? DEFAULT_GENERATION_SETTINGS.extras,
    video_understanding: Boolean(watched.video_understanding),
    video_interval: Number(watched.video_interval ?? DEFAULT_GENERATION_SETTINGS.video_interval),
    sampling_mode:
      (watched.sampling_mode as GenerationSettings['sampling_mode']) ??
      DEFAULT_GENERATION_SETTINGS.sampling_mode,
    max_frames: Number(watched.max_frames ?? DEFAULT_GENERATION_SETTINGS.max_frames),
    frames_per_second: Number(
      watched.frames_per_second ?? DEFAULT_GENERATION_SETTINGS.frames_per_second
    ),
    frame_limit_mode:
      (watched.frame_limit_mode as GenerationSettings['frame_limit_mode']) ??
      DEFAULT_GENERATION_SETTINGS.frame_limit_mode,
    ocr_assist:
      (watched.ocr_assist as GenerationSettings['ocr_assist']) ?? DEFAULT_GENERATION_SETTINGS.ocr_assist,
    grid_size: (watched.grid_size as [number, number] | undefined) ?? DEFAULT_GENERATION_SETTINGS.grid_size,
  }

  /** 面板只给 patch，写回表单时逐个 setValue，保持 react-hook-form 的脏值追踪。 */
  const applySettings = (patch: Partial<GenerationSettings>) => {
    for (const [key, value] of Object.entries(patch)) {
      form.setValue(key as keyof FormInput, value as never, { shouldDirty: true })
    }
  }

  // spec 018 决策 2：单集模式粘到多P 链接时**自动切到批量**（用户决策）。
  // 只在 URL 稳定 600ms 后探测一次，探测失败静默 —— 生成时后端还会再校验链接。
  const urlValue = useWatch({ control: form.control, name: 'video_url' })
  const probedUrl = useRef('')
  useEffect(() => {
    const value = (urlValue || '').trim()
    if (!onMultiPart || !value || !/BV[0-9A-Za-z]+/.test(value)) return
    if (probedUrl.current === value) return
    const timer = window.setTimeout(() => {
      probedUrl.current = value
      void expandUrl(withScheme(value))
        .then(result => {
          const parts = result.videos?.[0]?.parts ?? []
          if (parts.length > 1) onMultiPart(withScheme(value))
        })
        .catch(() => {
          // 探测失败不打扰用户：生成时后端还会再校验一次链接。
        })
    }, PART_PROBE_DELAY_MS)
    return () => window.clearTimeout(timer)
  }, [urlValue, onMultiPart])

  const errors = form.formState.errors
  const urlError =
    errors.video_url?.message === 'required'
      ? t('workspace.urlRequired')
      : errors.video_url
        ? t('workspace.urlInvalid')
        : undefined

  const onSubmit = async (values: FormValues) => {
    const gridSize: [number, number] = [values.grid_size[0], values.grid_size[1]]
    // Annotated so the object literal is checked against the API contract here, at
    // the one place it is built, instead of widening on the way to the store.
    const payload: GenerateNotePayload = {
      ...values,
      video_url: withScheme(values.video_url || ''),
      format: values.format,
      ocr_assist: values.ocr_assist === 'global' ? null : values.ocr_assist === 'on',
      grid_size: gridSize,
    }
    try {
      const data = await generateNote(payload)
      if (!data) return
      addPendingTask(data.task_id, values.platform, { ...payload, grid_size: gridSize })
      // 成功提示由数据层统一发（services/note.ts），这里再发一次会弹两条一样的。
    } catch (error) {
      const detailInfo = (
        error as { data?: { reason?: string; downloading?: boolean } } | undefined
      )?.data
      if (detailInfo?.reason === 'transcriber_model_not_ready') {
        toast.error(t('workspace.transcriberMissing'))
        if (!detailInfo.downloading) navigate('/settings/transcriber')
        return
      }
      // 兜底错误也要让用户看见：静默失败比报错更糟（reason 已知时按语言查文案）。
      toast.error(localizedError(error as Parameters<typeof localizedError>[0], 'common.error'))
    }
  }

  const label = (key: MessageKey) => t(key)

  return (
    <Card className="bg-canvas">
      <form onSubmit={form.handleSubmit(onSubmit)} className="flex flex-col">
        <div className="flex flex-col gap-3 p-4 sm:p-5">
          <Field label={label('workspace.urlLabel')} htmlFor="video-url" error={urlError} required>
            <div className="relative">
              <Link2
                aria-hidden="true"
                className="text-ink-mute pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
              />
              <Input
                id="video-url"
                autoComplete="off"
                spellCheck={false}
                className="text-body h-12 pl-9"
                placeholder={label('workspace.urlPlaceholder')}
                {...form.register('video_url')}
              />
            </div>
          </Field>

          <div className="flex items-center gap-2">
            <Button
              type="submit"
              variant="primary"
              size="lg"
              loading={form.formState.isSubmitting}
              className="min-w-0 flex-1 sm:min-w-[184px] sm:flex-none"
            >
              <Sparkles className="size-4" />
              {form.formState.isSubmitting
                ? label('workspace.submitting')
                : label('workspace.submit')}
            </Button>
            <Button
              type="button"
              size="lg"
              className="shrink-0"
              aria-expanded={showOptions}
              aria-label={label('workspace.form')}
              onClick={() => setShowOptions(current => !current)}
            >
              <SlidersHorizontal className="size-4" />
              <span className="hidden sm:inline">{label('workspace.form')}</span>
            </Button>
          </div>
        </div>

        {showOptions ? (
          <div className="ember-enter border-line flex flex-col gap-5 border-t p-4 sm:p-5">
            {/* 与批量面板渲染同一个组件（spec 020）：两处参数不会再各写一套。 */}
            <GenerationOptions value={settings} onChange={applySettings} idPrefix="single" />
          </div>
        ) : null}
      </form>
    </Card>
  )
}
