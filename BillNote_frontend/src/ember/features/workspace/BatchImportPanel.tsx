import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import toast from 'react-hot-toast'
import {
  CheckSquare,
  Layers,
  Link2,
  ListChecks,
  ListOrdered,
  SlidersHorizontal,
  Square,
  X,
} from 'lucide-react'
import { Button } from '@/ember/ui/Button'
import { Card } from '@/ember/ui/Card'
import { Checkbox } from '@/ember/ui/Checkbox'
import { Chip } from '@/ember/ui/Chip'
import { Field } from '@/ember/ui/Field'
import { Input, Textarea } from '@/ember/ui/Input'
import { Spinner } from '@/ember/ui/Spinner'
import { Switch } from '@/ember/ui/Switch'
import { useI18n } from '@/ember/i18n/context'
import { buildBatchPayload } from '@/utils/batchPayload'
import { GenerationOptions } from './GenerationOptions'
import {
  DEFAULT_GENERATION_SETTINGS,
  toPayloadSettings,
} from './generationSettings'
import type { GenerationSettings } from './generationSettings'
import { parseRangeSelection } from '@/utils/range'
import {
  expandUrl,
  generateNotes,
  resolveIntake,
  type BatchGenerateItem,
  type ExpandPart,
  type ExpandResult,
} from '@/services/note'

/**
 * 批量在共享输出格式上增加目录和总结，保留单集与批量一致的截图和跳转默认值。
 */
const BATCH_DEFAULT_SETTINGS: GenerationSettings = {
  ...DEFAULT_GENERATION_SETTINGS,
  format: ['toc', ...DEFAULT_GENERATION_SETTINGS.format, 'summary'],
}
const LARGE_BATCH_THRESHOLD = 20

type Row = {
  key: string
  /** 展开后的列表序号（1 起）。页码范围按它选（spec 018 决策 7）。 */
  index: number
  bvid: string
  videoTitle: string
  videoIndex: number
  part: ExpandPart
}

type Props = {
  /** Collapse the panel (the workspace owns the open flag). */
  onClose?: () => void
  /** Called after a batch was accepted, so the workspace can reload its task list. */
  onSubmitted: () => void
  /** Optional override of the batch generation profile. */
  defaults?: Partial<GenerationSettings>
  /**
   * 单集模式探测到多P 后带过来的链接（spec 018 决策 2）：预填并自动展开，
   * 用户不用再粘一次。
   */
  initialUrl?: string
}

/**
 * Batch import: many links in, one group of tasks out — or, with "merge",
 * a single task whose note is the concatenation of every video's note (spec 018).
 *
 * Rendered inline in the workspace column rather than in a modal — a batch can
 * carry hundreds of rows, and a dialog would either cap the list or scroll
 * inside a scroll. The flow is three explicit stages so the user always knows
 * whether a link has been resolved yet:
 *   paste → resolve (expandUrl per link) → pick parts → name the group → submit.
 */
export function BatchImportPanel({ onClose, onSubmitted, defaults, initialUrl }: Props) {
  const { t } = useI18n()
  const [settings, setSettings] = useState<GenerationSettings>(() => ({
    ...BATCH_DEFAULT_SETTINGS,
    ...defaults,
  }))
  const [showOptions, setShowOptions] = useState(false)

  const [stage, setStage] = useState<'input' | 'picking'>('input')
  const [raw, setRaw] = useState(initialUrl ?? '')
  const [resolving, setResolving] = useState(false)
  const [videos, setVideos] = useState<ExpandResult[]>([])
  const [selected, setSelected] = useState<Record<string, boolean>>({})
  const [rangeText, setRangeText] = useState('')
  const [groupName, setGroupName] = useState('')
  // spec 018 决策 3：合并任务 —— 不建组，只产生一条任务。
  const [merge, setMerge] = useState(false)
  const [mergeTitle, setMergeTitle] = useState('')
  const [mergePolish, setMergePolish] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  const rows = useMemo<Row[]>(() => {
    const out: Row[] = []
    videos.forEach((video, videoIndex) => {
      video.videos?.forEach(entry => {
        entry.parts?.forEach(part => {
          out.push({
            key: `${videoIndex}:${entry.bvid}:${part.p}`,
            index: out.length + 1,
            bvid: entry.bvid,
            videoTitle: entry.title || video.title,
            videoIndex,
            part,
          })
        })
      })
    })
    return out
  }, [videos])

  const picked = rows.filter(row => selected[row.key])
  const isLargeBatch = picked.length > LARGE_BATCH_THRESHOLD
  const rangeIndexes = useMemo(
    () => parseRangeSelection(rangeText, rows.length),
    [rangeText, rows.length]
  )

  const handleResolve = useCallback(
    async (rawText?: string) => {
      const source = rawText ?? raw
      const urls = source
        .split(/[\n\s]+/)
        .map(value => value.trim())
        .filter(Boolean)
      if (urls.length === 0) {
        toast.error(t('batch.noUrls'))
        return
      }
      setResolving(true)
      try {
        const settled = await Promise.all(
          urls.map(async url => {
            try {
              return await expandUrl(url)
            } catch {
              return null
            }
          })
        )
        const ok = settled.filter((item): item is ExpandResult => Boolean(item))
        if (ok.length === 0) {
          toast.error(t('batch.resolveFailed'))
          return
        }
        if (ok.length < urls.length) {
          toast.error(t('batch.resolvePartial', { failed: urls.length - ok.length }))
        }
        setVideos(ok)
        const next: Record<string, boolean> = {}
        ok.forEach((video, videoIndex) => {
          video.videos?.forEach(entry => {
            entry.parts?.forEach(part => {
              if (!part.already_generated) next[`${videoIndex}:${entry.bvid}:${part.p}`] = true
            })
          })
        })
        setSelected(next)
        setRangeText('')
        const title = ok[0].title || ''
        setGroupName(current => current || title)
        setMergeTitle(current => current || title)
        setStage('picking')
      } finally {
        setResolving(false)
      }
    },
    [raw, t]
  )

  // 从单集模式切过来时自动展开一次；ref 挡住 StrictMode 的二次调用。
  const bootstrapped = useRef(false)
  useEffect(() => {
    if (bootstrapped.current || !initialUrl) return
    bootstrapped.current = true
    setRaw(initialUrl)
    void handleResolve(initialUrl)
  }, [initialUrl, handleResolve])

  const toggleAll = (on: boolean) => {
    const next: Record<string, boolean> = {}
    rows.forEach(row => {
      if (!row.part.already_generated) next[row.key] = on
    })
    setSelected(next)
  }

  /** 页码范围：只勾中范围内的行（spec 018 决策 7，按列表序号）。 */
  const applyRange = () => {
    const wanted = new Set(rangeIndexes)
    const next: Record<string, boolean> = {}
    rows.forEach(row => {
      if (wanted.has(row.index) && !row.part.already_generated) next[row.key] = true
    })
    setSelected(next)
  }

  const handleSubmit = async () => {
    if (picked.length === 0) {
      toast.error(t('batch.noPartsSelected'))
      return
    }
    const trimmedGroup = groupName.trim()
    const trimmedTitle = mergeTitle.trim()
    if (merge) {
      if (!trimmedTitle) {
        toast.error(t('batch.mergeTitleRequired'))
        return
      }
    } else if (!trimmedGroup) {
      toast.error(t('batch.groupRequired'))
      return
    }

    setSubmitting(true)
    try {
      // Resolve first: the backend is the only authority on which links are
      // acceptable, and it is cheaper to reject here than to explain later why
      // three of twenty tasks never appeared.
      const intake = await resolveIntake(
        'batch',
        picked.map(row => row.part.url)
      )
      const rejected = intake.items.filter(item => item.state !== 'ready')
      if (rejected.length > 0) {
        toast.error(t('batch.intakeFailed', { count: rejected.length }))
        return
      }

      const items: BatchGenerateItem[] = picked.map(row => ({
        bvid: row.bvid,
        p: row.part.p,
        video_id: row.part.video_id,
        url: row.part.url,
        // 合并任务用它做小节标题（spec 018）。
        title: row.part.title,
      }))

      // 两条路径（建组 / 合并成一条任务）由纯函数组装，单测钉住字段差异（spec 018）。
      const payload = buildBatchPayload(toPayloadSettings(settings), items, {
        merge,
        groupName: trimmedGroup,
        mergeTitle: trimmedTitle,
        mergePolish,
      })

      const { results } = await generateNotes(payload)
      const queued = results.filter(result => result.status === 'queued').length
      const skipped = results.filter(result => result.status === 'skipped').length
      if (merge) {
        toast.success(t('batch.mergeSubmitted', { count: queued }))
      } else if (queued > 0) {
        toast.success(t('batch.submitted', { queued, skipped }))
      } else {
        toast.success(t('batch.allSkipped', { skipped }))
      }
      onSubmitted()
      onClose?.()
    } catch {
      toast.error(t('batch.failed'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Card className="ember-enter bg-canvas">
      <div className="border-line flex items-center justify-between gap-3 border-b px-4 py-3 sm:px-5">
        <div className="flex min-w-0 items-center gap-2">
          <Layers className="text-ember size-4 shrink-0" aria-hidden="true" />
          <h2 className="text-subhead truncate">{t('batch.title')}</h2>
        </div>
        {onClose ? (
          <Button variant="ghost" size="sm" onClick={onClose} aria-label={t('batch.close')}>
            <X className="size-4" />
          </Button>
        ) : null}
      </div>

      <div className="flex flex-col gap-4 p-4 sm:p-5">
        {stage === 'input' ? (
          <>
            <p className="text-small text-ink-soft">{t('batch.description')}</p>
            <Field label={t('batch.urlsLabel')} htmlFor="batch-urls" hint={t('batch.urlsHint')}>
              <Textarea
                id="batch-urls"
                rows={5}
                spellCheck={false}
                autoComplete="off"
                className="text-small font-mono"
                placeholder={t('batch.urlsPlaceholder')}
                value={raw}
                onChange={event => setRaw(event.target.value)}
              />
            </Field>
            <div>
              <Button variant="primary" onClick={() => void handleResolve()} loading={resolving}>
                <Link2 className="size-4" />
                {resolving ? t('batch.resolving') : t('batch.resolve')}
              </Button>
            </div>
          </>
        ) : (
          <>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-small text-ink-soft">
                {t('batch.resolvedSummary', { videos: videos.length, parts: rows.length })}
              </p>
              <div className="flex items-center gap-2">
                <Chip onClick={() => toggleAll(true)}>
                  <CheckSquare className="size-3.5" aria-hidden="true" />
                  {t('batch.selectAll')}
                </Chip>
                <Chip onClick={() => toggleAll(false)}>
                  <Square className="size-3.5" aria-hidden="true" />
                  {t('batch.clearAll')}
                </Chip>
              </div>
            </div>

            <div className="flex flex-wrap items-end gap-2">
              <Field
                label={t('batch.rangeLabel')}
                htmlFor="batch-range"
                hint={t('batch.rangeHint')}
                className="min-w-[180px] flex-1"
              >
                <Input
                  id="batch-range"
                  className="h-10"
                  inputMode="numeric"
                  placeholder={t('batch.rangePlaceholder')}
                  value={rangeText}
                  onChange={event => setRangeText(event.target.value)}
                />
              </Field>
              <Button
                variant="secondary"
                onClick={applyRange}
                disabled={rangeIndexes.length === 0}
                className="shrink-0"
              >
                <ListOrdered className="size-4" />
                {t('batch.rangeApply')}
              </Button>
              {rangeIndexes.length > 0 ? (
                <span className="text-caption text-ink-soft pb-2">
                  {t('batch.rangeApplied', { count: rangeIndexes.length })}
                </span>
              ) : null}
            </div>

            <ul className="flex max-h-[420px] flex-col gap-3 overflow-y-auto pr-1">
              {videos.map((video, videoIndex) => (
                <li
                  key={`${videoIndex}:${video.title}`}
                  className="rounded-surface border-line border"
                >
                  <div className="border-line bg-surface flex items-center gap-2 border-b px-3 py-2">
                    <span className="text-small text-ink truncate font-semibold">
                      {video.title}
                    </span>
                    <span className="text-caption text-ink-mute ml-auto shrink-0">
                      {t('batch.videoParts', {
                        count:
                          video.videos?.reduce(
                            (sum, entry) => sum + (entry.parts?.length || 0),
                            0
                          ) || 0,
                      })}
                    </span>
                  </div>
                  <ul className="flex flex-col">
                    {video.videos?.flatMap(entry =>
                      entry.parts?.map(part => {
                        const key = `${videoIndex}:${entry.bvid}:${part.p}`
                        const index = rows.find(row => row.key === key)?.index ?? 0
                        return (
                          <li
                            key={key}
                            className="border-line flex items-start gap-3 border-b px-3 py-2 last:border-b-0"
                          >
                            <span className="text-caption text-ink-mute w-8 shrink-0 pt-0.5 text-right font-mono tabular-nums">
                              {index}
                            </span>
                            <Checkbox
                              id={key}
                              className="mt-0.5"
                              checked={Boolean(selected[key])}
                              disabled={part.already_generated}
                              label={part.title}
                              onChange={value =>
                                setSelected(current => ({ ...current, [key]: value }))
                              }
                            />
                            <label htmlFor={key} className="min-w-0 flex-1 cursor-pointer">
                              <span className="text-small text-ink block truncate">
                                P{part.p} · {part.title}
                              </span>
                              <span className="text-caption text-ink-mute mt-0.5 flex items-center gap-2">
                                <span className="tabular-nums">
                                  {formatDuration(part.duration)}
                                </span>
                                {part.already_generated && (
                                  <span className="text-ink-mute">{t('batch.generated')}</span>
                                )}
                              </span>
                            </label>
                          </li>
                        )
                      })
                    )}
                  </ul>
                </li>
              ))}
            </ul>

            {/* spec 020：批量也要能调生成设置 —— 与单集表单同一个受控面板。 */}
            <div className="flex flex-col gap-3">
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="gap-1.5 self-start px-2"
                aria-expanded={showOptions}
                aria-label={t('workspace.form')}
                onClick={() => setShowOptions(current => !current)}
              >
                <SlidersHorizontal className="size-4" />
                {t('workspace.form')}
              </Button>
              {showOptions ? (
                <div className="ember-enter rounded-surface border-line border p-4">
                  <GenerationOptions
                    value={settings}
                    onChange={patch => setSettings(current => ({ ...current, ...patch }))}
                    idPrefix="batch"
                  />
                </div>
              ) : null}
            </div>
            <div className="rounded-surface border-line bg-surface flex flex-col gap-3 border p-4">
              <div className="flex items-start justify-between gap-4">
                <div className="flex flex-col gap-0.5">
                  <span className="text-small text-ink font-semibold">{t('batch.merge')}</span>
                  <span className="text-caption text-ink-soft">{t('batch.mergeHint')}</span>
                </div>
                <Switch
                  checked={merge}
                  label={t('batch.merge')}
                  onChange={checked => setMerge(checked)}
                />
              </div>

              {merge ? (
                <div className="ember-enter border-line flex flex-col gap-3 border-t pt-3">
                  <div className="flex items-start justify-between gap-4">
                    <div className="flex flex-col gap-0.5">
                      <span className="text-small text-ink font-semibold">
                        {t('batch.mergePolish')}
                      </span>
                      <span className="text-caption text-ink-soft">
                        {t('batch.mergePolishHint')}
                      </span>
                    </div>
                    <Switch
                      checked={mergePolish}
                      label={t('batch.mergePolish')}
                      onChange={checked => setMergePolish(checked)}
                    />
                  </div>
                </div>
              ) : null}
            </div>

            {merge ? (
              <Field
                label={t('batch.mergeTitleLabel')}
                htmlFor="batch-merge-title"
                hint={t('batch.mergeTitleHint')}
                required
              >
                <Input
                  id="batch-merge-title"
                  className="h-10"
                  value={mergeTitle}
                  onChange={event => setMergeTitle(event.target.value)}
                />
              </Field>
            ) : (
              <Field
                label={t('batch.groupLabel')}
                htmlFor="batch-group"
                hint={t('batch.groupHint')}
                required
              >
                <Input
                  id="batch-group"
                  className="h-10"
                  value={groupName}
                  onChange={event => setGroupName(event.target.value)}
                />
              </Field>
            )}

            {isLargeBatch && <p className="text-caption text-warn">{t('batch.largeHint')}</p>}

            <div className="flex flex-wrap items-center gap-2">
              <Button variant="primary" onClick={handleSubmit} loading={submitting}>
                <ListChecks className="size-4" />
                {submitting
                  ? t('batch.submitting')
                  : merge
                    ? t('batch.mergeSubmit', { count: picked.length })
                    : t('batch.submit', { count: picked.length })}
              </Button>
              <Button variant="ghost" onClick={() => setStage('input')}>
                {t('batch.back')}
              </Button>
              {resolving && <Spinner className="size-4" />}
            </div>
          </>
        )}
      </div>
    </Card>
  )
}

/** mm:ss for a part duration; empty string when the backend did not report one. */
function formatDuration(seconds: number | undefined | null): string {
  if (!seconds || seconds <= 0) return ''
  const total = Math.round(seconds)
  const minutes = Math.floor(total / 60)
  const rest = total % 60
  return `${minutes}:${String(rest).padStart(2, '0')}`
}
