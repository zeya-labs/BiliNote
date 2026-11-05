import { useEffect, useState } from 'react'
import { Check, Download, Pencil, Plus, Trash2 } from 'lucide-react'
import toast from 'react-hot-toast'
import {
  addOpenaiAsr,
  deleteOpenaiAsr,
  downloadModel,
  getModelsStatus,
  getTranscriberConfig,
  testOpenaiAsr,
  updateOpenaiAsr,
  updateTranscriberConfig,
  type ModelsStatusResponse,
  type TranscriberConfig,
} from '@/services/transcriber'
import { getGeneralSettings } from '@/services/general'
import { useAsync } from '@/ember/lib/useAsync'
import { useI18n } from '@/ember/i18n/context'
import type { MessageKey } from '@/ember/i18n/strings'
import { Badge } from '@/ember/ui/Badge'
import { Button } from '@/ember/ui/Button'
import { Card } from '@/ember/ui/Card'
import { Dialog } from '@/ember/ui/Dialog'
import { Field } from '@/ember/ui/Field'
import { Input } from '@/ember/ui/Input'
import { Select } from '@/ember/ui/Select'
import { Switch } from '@/ember/ui/Switch'
import { Skeleton } from '@/ember/ui/Skeleton'
import { SettingRow, SettingsSection } from '@/ember/pages/settings/Section'

/** 端点行类型直接从服务层推导：同一份形状只声明一次，改接口时这里自动跟着变。 */
type AsrEndpoint = NonNullable<Awaited<ReturnType<typeof getTranscriberConfig>>["openai_asr_endpoints"]>[number]

const ENGINE_LABELS: Record<string, MessageKey> = {
  'fast-whisper': 'settings.transcriber.engine.local',
  'openai-asr': 'settings.transcriber.engine.online',
}

/** 支持「跟随通用设置」的字段：后端只把这些记进 explicit_fields。 */
const SILENCE_KEYS = ['silence_split', 'silence_noise_db', 'min_silence_seconds'] as const

const STYLES = [
  { value: 'transcriptions', label: 'transcriptions (/audio/transcriptions)' },
  { value: 'chat', label: 'chat (chat/completions + input_audio)' },
  { value: 'dashscope', label: 'DashScope Filetrans' },
]

/**
 * Transcription engine. Model downloads run in the background on the server, so
 * the status is polled while anything is downloading and stops as soon as it is not.
 *
 * Endpoints are editable in place. The list only ever returns a masked api_key, so
 * the editor starts with an empty key field: leaving it empty keeps the stored key,
 * which the backend enforces by dropping any value that still contains an asterisk.
 */
export function TranscriberSection() {
  const { t } = useI18n()
  const config = useAsync(getTranscriberConfig)
  const status = useAsync(getModelsStatus)
  const [saving, setSaving] = useState(false)
  const [editor, setEditor] = useState<AsrEndpoint | 'new' | null>(null)
  const [pendingDelete, setPendingDelete] = useState<AsrEndpoint | null>(null)

  const downloading = Boolean(status.data?.whisper.some(item => item.downloading))

  useEffect(() => {
    if (!downloading) return
    const timer = window.setInterval(() => status.reload(), 2000)
    return () => window.clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [downloading])

  const data: TranscriberConfig | undefined = config.data

  const apply = async (patch: Parameters<typeof updateTranscriberConfig>[0]) => {
    setSaving(true)
    try {
      await updateTranscriberConfig(patch)
      toast.success(t('settings.saved'))
      config.reload()
    } catch {
      toast.error(t('settings.saveFailed'))
    } finally {
      setSaving(false)
    }
  }

  const download = async (modelSize: string) => {
    try {
      await downloadModel({ model_size: modelSize, transcriber_type: data?.transcriber_type })
      toast.success(t('settings.transcriber.downloading'))
      status.reload()
    } catch {
      toast.error(t('settings.saveFailed'))
    }
  }

  const statusFor = (models: ModelsStatusResponse['whisper'] | undefined, size: string) =>
    models?.find(item => item.model_size === size)

  if (config.loading && !data) return <Skeleton className="h-72 w-full" />

  const endpoints: AsrEndpoint[] = data?.openai_asr_endpoints ?? []
  const modelStatus = statusFor(status.data?.whisper, data?.whisper_model_size ?? '')

  return (
    <SettingsSection
      title={t('settings.transcriber')}
      description={t('settings.transcriber.engine')}
    >
      <div className="flex flex-col gap-4">
        <Card className="bg-canvas px-4 sm:px-5">
          <SettingRow
            label={t('settings.transcriber.engine')}
            control={
              <Select
                value={data?.transcriber_type ?? ''}
                options={(data?.available_types ?? []).map(item => ({
                  value: item.value,
                  label: ENGINE_LABELS[item.value] ? t(ENGINE_LABELS[item.value]) : item.label,
                }))}
                onChange={event => apply({ transcriber_type: event.target.value, whisper_model_size: data?.whisper_model_size })}
                disabled={saving}
              />
            }
          />
          {data?.transcriber_type === 'fast-whisper' ? (
            <SettingRow
              label={t('settings.transcriber.modelSize')}
              hint={
                modelStatus
                  ? modelStatus.downloading
                    ? t('settings.transcriber.downloading')
                    : modelStatus.downloaded
                      ? t('settings.transcriber.downloaded')
                      : t('settings.transcriber.notDownloaded')
                  : undefined
              }
              control={
                <div className="flex items-center gap-2">
                  <Select
                    value={data?.whisper_model_size ?? ''}
                    options={(data?.whisper_model_sizes ?? []).map(size => ({ value: size, label: size }))}
                    onChange={event =>
                      apply({ transcriber_type: data?.transcriber_type ?? '', whisper_model_size: event.target.value })
                    }
                    disabled={saving}
                  />
                  {modelStatus && !modelStatus.downloaded ? (
                    <Button size="sm" variant="secondary" onClick={() => download(modelStatus.model_size)}>
                      <Download className="size-3.5" />
                    </Button>
                  ) : null}
                </div>
              }
            />
          ) : null}
        </Card>

        <div className="flex items-center justify-between gap-2">
          <span className="ember-overline">{t('settings.transcriber.endpoints')}</span>
          <Button size="sm" variant="secondary" onClick={() => setEditor('new')}>
            <Plus className="size-3.5" />
            {t('settings.transcriber.addEndpoint')}
          </Button>
        </div>

        {endpoints.length ? (
          <div className="flex flex-col gap-2">
            {endpoints.map(endpoint => {
              const active = data?.openai_asr_id === endpoint.id
              return (
                <Card key={endpoint.id} className="bg-canvas" selected={active}>
                  <div className="flex flex-wrap items-center gap-3 px-4 py-3">
                    <div className="flex min-w-0 flex-1 flex-col">
                      <span className="truncate text-small font-semibold text-ink">{endpoint.name}</span>
                      <span className="truncate text-caption text-ink-soft">
                        {endpoint.base_url} · {endpoint.model}
                      </span>
                    </div>
                    <Badge tone="neutral">{endpoint.api_style === 'dashscope' ? t('settings.transcriber.dashscope') : endpoint.api_style}</Badge>
                    {active ? (
                      <Badge tone="ember">
                        <Check className="size-3" />
                      </Badge>
                    ) : (
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() =>
                          apply({ transcriber_type: data?.transcriber_type ?? '', openai_asr_id: endpoint.id })
                        }
                      >
                        {t('common.confirm')}
                      </Button>
                    )}
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={async () => {
                        try {
                          await testOpenaiAsr(endpoint.id)
                          toast.success(t('settings.model.connected'))
                        } catch {
                          toast.error(t('settings.model.connectFailed'))
                        }
                      }}
                    >
                      {t('settings.transcriber.test')}
                    </Button>
                    <button
                      type="button"
                      aria-label={t('settings.transcriber.editEndpoint')}
                      title={t('settings.transcriber.editEndpoint')}
                      className="rounded-control p-1 text-ink-mute transition-colors duration-150 hover:bg-raised hover:text-ink"
                      onClick={() => setEditor(endpoint)}
                    >
                      <Pencil className="size-3.5" />
                    </button>
                    <button
                      type="button"
                      aria-label={t('settings.transcriber.deleteEndpoint')}
                      title={t('settings.transcriber.deleteEndpoint')}
                      className="rounded-control p-1 text-ink-mute transition-colors duration-150 hover:bg-raised hover:text-danger"
                      onClick={() => setPendingDelete(endpoint)}
                    >
                      <Trash2 className="size-3.5" />
                    </button>
                  </div>
                </Card>
              )
            })}
          </div>
        ) : (
          <p className="text-caption text-ink-mute">{t('settings.transcriber.empty')}</p>
        )}
      </div>

      <Dialog
        open={editor !== null}
        onClose={() => setEditor(null)}
        title={editor === 'new' ? t('settings.transcriber.addEndpoint') : t('settings.transcriber.editEndpoint')}
        size="md"
      >
        {editor !== null ? (
          <EndpointDialog
            endpoint={editor === 'new' ? undefined : editor}
            onDone={() => {
              setEditor(null)
              config.reload()
            }}
          />
        ) : null}
      </Dialog>

      <Dialog
        open={Boolean(pendingDelete)}
        onClose={() => setPendingDelete(null)}
        title={t('settings.transcriber.deleteEndpoint')}
        description={
          pendingDelete ? t('settings.transcriber.endpointDeleteBody', { name: pendingDelete.name }) : undefined
        }
        size="sm"
        footer={
          <>
            <Button variant="ghost" onClick={() => setPendingDelete(null)}>
              {t('common.cancel')}
            </Button>
            <Button
              variant="destructive"
              onClick={async () => {
                const target = pendingDelete
                setPendingDelete(null)
                if (!target) return
                try {
                  await deleteOpenaiAsr(target.id)
                  config.reload()
                } catch {
                  toast.error(t('settings.saveFailed'))
                }
              }}
            >
              {t('common.delete')}
            </Button>
          </>
        }
      >
        <span />
      </Dialog>
    </SettingsSection>
  )
}

function EndpointDialog({ endpoint, onDone }: { endpoint?: AsrEndpoint; onDone: () => void }) {
  const { t } = useI18n()
  const [name, setName] = useState(endpoint?.name ?? '')
  const [baseUrl, setBaseUrl] = useState(endpoint?.base_url ?? '')
  const [apiKey, setApiKey] = useState('')
  const [model, setModel] = useState(endpoint?.model ?? '')
  const [style, setStyle] = useState<AsrEndpoint['api_style']>(endpoint?.api_style ?? 'transcriptions')
  const [saving, setSaving] = useState(false)
  // 端点级事务参数。每一项都对应后端 `_normalize_endpoint` 的默认值：留空 = 用默认，
  // 所以新增端点时不填也能保存。这些值直接决定「一次请求能发多大、能并发多少」，
  // 是 413 / 429 / 静音切片失效这类问题的第一调参入口。
  const [timeout, setTimeoutSec] = useState(endpoint?.timeout ?? 600)
  const [chunkSizeMb, setChunkSizeMb] = useState(endpoint?.chunk_size_mb ?? 18)
  const [maxSpeechChunks, setMaxSpeechChunks] = useState(endpoint?.max_speech_chunks ?? 120)
  const [silenceNoiseDb, setSilenceNoiseDb] = useState(endpoint?.silence_noise_db ?? -30)
  const [minSilenceSeconds, setMinSilenceSeconds] = useState(endpoint?.min_silence_seconds ?? 0.5)
  const [maxRpm, setMaxRpm] = useState(endpoint?.max_rpm ?? 30)
  const [chunkConcurrency, setChunkConcurrency] = useState(endpoint?.chunk_concurrency ?? 4)
  const [silenceSplit, setSilenceSplit] = useState(endpoint?.silence_split ?? true)
  // 三个静音参数可以「跟随通用设置」：后端只在收到值时才把它记进 explicit_fields，
  // 传 null 等于撤掉端点级覆盖、重新跟着「设置 → 通用」走。
  const [followGlobal, setFollowGlobal] = useState(() => {
    const explicit = endpoint?.explicit_fields ?? []
    return !SILENCE_KEYS.some(key => explicit.includes(key))
  })
  // 跟随态下把全局值显示出来，否则用户只能靠猜「现在到底用多少」
  const general = useAsync(() => getGeneralSettings(), [])

  const submit = async () => {
    if (!name.trim() || !baseUrl.trim() || !model.trim()) return
    setSaving(true)
    try {
      const payload = {
        name: name.trim(),
        base_url: baseUrl.trim(),
        model: model.trim(),
        api_style: style,
        timeout: Number(timeout) || 600,
        chunk_size_mb: Number(chunkSizeMb) || 18,
        max_speech_chunks: Number(maxSpeechChunks) || 120,
        silence_noise_db: followGlobal ? null : Number(silenceNoiseDb),
        min_silence_seconds: followGlobal ? null : Number(minSilenceSeconds),
        max_rpm: Number(maxRpm) || 30,
        chunk_concurrency: Number(chunkConcurrency) || 1,
        silence_split: followGlobal ? null : silenceSplit,
        // 空 = 不改：后端会把带 * 的值当成掩码丢弃，这里连发都不发
        ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
      }
      if (endpoint) {
        await updateOpenaiAsr(endpoint.id, { ...payload, api_key: payload.api_key ?? endpoint.api_key })
      } else {
        await addOpenaiAsr({ ...payload, api_key: apiKey })
      }
      toast.success(t('settings.saved'))
      onDone()
    } catch {
      toast.error(t('settings.saveFailed'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <Field label={t('settings.transcriber.endpointName')} htmlFor="asr-name" required>
        <Input id="asr-name" value={name} onChange={event => setName(event.target.value)} />
      </Field>
      <Field label={t('settings.model.baseUrl')} htmlFor="asr-url" required>
        <Input id="asr-url" value={baseUrl} onChange={event => setBaseUrl(event.target.value)} />
      </Field>
      <Field label={t('settings.model.apiKey')} htmlFor="asr-key">
        <Input
          id="asr-key"
          type="password"
          autoComplete="off"
          value={apiKey}
          placeholder={endpoint?.api_key ?? ''}
          onChange={event => setApiKey(event.target.value)}
        />
      </Field>
      <Field label={t('settings.model.modelName')} htmlFor="asr-model" required>
        <Input id="asr-model" value={model} onChange={event => setModel(event.target.value)} />
      </Field>
      <Field label={t('settings.transcriber.style')} htmlFor="asr-style">
        <Select id="asr-style" value={style} options={STYLES.map(item => item.value === 'dashscope' ? { ...item, label: t('settings.transcriber.dashscope') } : item)} onChange={event => setStyle(event.target.value as AsrEndpoint['api_style'])} />
      </Field>

      {style === 'dashscope' ? (
        <p className="text-caption text-ink-soft">{t('settings.transcriber.dashscopeHint')}</p>
      ) : null}

      <div className="flex flex-col gap-1 border-t border-line/70 pt-4">
        <span className="ember-overline text-ink-mute">{t('settings.transcriber.limits')}</span>
        <p className="text-caption text-ink-soft">{t('settings.transcriber.limitsHint')}</p>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Field label={t('settings.transcriber.timeout')} htmlFor="asr-timeout">
          <Input
            id="asr-timeout"
            type="number"
            min={1}
            value={timeout}
            onChange={event => setTimeoutSec(Number(event.target.value))}
          />
        </Field>
        <Field label={t('settings.transcriber.chunkSize')} htmlFor="asr-chunk">
          <Input
            id="asr-chunk"
            type="number"
            min={1}
            step={1}
            value={chunkSizeMb}
            onChange={event => setChunkSizeMb(Number(event.target.value))}
          />
        </Field>
        <Field label={t('settings.transcriber.maxRpm')} htmlFor="asr-rpm">
          <Input
            id="asr-rpm"
            type="number"
            min={1}
            value={maxRpm}
            onChange={event => setMaxRpm(Number(event.target.value))}
          />
        </Field>
        <Field label={t('settings.transcriber.concurrency')} htmlFor="asr-concurrency">
          <Input
            id="asr-concurrency"
            type="number"
            min={1}
            max={32}
            value={chunkConcurrency}
            onChange={event => setChunkConcurrency(Number(event.target.value))}
          />
        </Field>
        <Field label={t('settings.transcriber.maxSpeechChunks')} htmlFor="asr-speech-chunks">
          <Input
            id="asr-speech-chunks"
            type="number"
            min={1}
            value={maxSpeechChunks}
            onChange={event => setMaxSpeechChunks(Number(event.target.value))}
          />
        </Field>
        <Field label={t('settings.transcriber.silenceNoise')} htmlFor="asr-noise">
          <Input
            id="asr-noise"
            type="number"
            step={0.5}
            disabled={followGlobal}
            value={silenceNoiseDb}
            onChange={event => setSilenceNoiseDb(Number(event.target.value))}
          />
        </Field>
        <Field label={t('settings.transcriber.minSilence')} htmlFor="asr-min-silence">
          <Input
            id="asr-min-silence"
            type="number"
            step={0.05}
            min={0.05}
            disabled={followGlobal}
            value={minSilenceSeconds}
            onChange={event => setMinSilenceSeconds(Number(event.target.value))}
          />
        </Field>
      </div>

      <SettingRow
        label={t('settings.transcriber.silenceFollowGlobal')}
        hint={t('settings.transcriber.silenceFollowGlobalHint')}
        control={
          <Switch
            checked={followGlobal}
            onChange={setFollowGlobal}
            label={t('settings.transcriber.silenceFollowGlobal')}
          />
        }
      />

      {followGlobal ? (
        <p className="text-caption text-ink-mute" data-testid="silence-global-values">
          {t('settings.transcriber.silenceGlobalNow', {
            noise: general.data?.asr_silence_noise_db ?? -30,
            silence: general.data?.asr_min_silence_seconds ?? 0.5,
            split: general.data?.asr_silence_split === false
              ? t('settings.transcriber.silenceOff')
              : t('settings.transcriber.silenceOn'),
          })}
        </p>
      ) : null}

      <SettingRow
        label={t('settings.transcriber.silenceSplit')}
        hint={t('settings.transcriber.silenceSplitHint')}
        control={
          <Switch
            checked={silenceSplit}
            onChange={setSilenceSplit}
            disabled={followGlobal}
            label={t('settings.transcriber.silenceSplit')}
          />
        }
      />

      <div className="flex justify-end">
        <Button variant="primary" onClick={submit} loading={saving} disabled={!name.trim() || !baseUrl.trim() || !model.trim()}>
          {t('common.save')}
        </Button>
      </div>
    </div>
  )
}
