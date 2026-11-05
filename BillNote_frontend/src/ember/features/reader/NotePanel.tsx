import { useState } from 'react'
import { Copy, Download, FileDown, FileText, ListTree } from 'lucide-react'
import toast from 'react-hot-toast'
import { downloadNotePdf } from '@/services/note'
import { readLatestMarkdown, type Task } from '@/store/taskStore'
import { useI18n } from '@/ember/i18n/context'
import { Button } from '@/ember/ui/Button'
import { EmptyState } from '@/ember/ui/EmptyState'
import { Tabs } from '@/ember/ui/Tabs'
import { MarkdownNote } from '@/ember/features/reader/MarkdownNote'
import { scrollToAnchor } from '@/ember/features/reader/anchors'
import { extractOutline } from '@/ember/features/reader/outline'
import { TranscriptView } from '@/ember/features/reader/TranscriptView'

type ReaderTab = 'note' | 'transcript'

type NotePanelProps = {
  task: Task | undefined
}

/**
 * The reading surface: the generated note and the transcript it came from,
 * tabbed rather than split — on a laptop, side-by-side panes make both halves
 * too narrow to read.
 *
 * The mind map and the RAG chat that used to sit here were removed together
 * with their backends: both were extra surfaces over the same note, and the
 * vector index the chat needed was rebuilt on every finished task.
 */
export function NotePanel({ task }: NotePanelProps) {
  const { t } = useI18n()
  const [tab, setTab] = useState<ReaderTab>('note')
  const [exportingPdf, setExportingPdf] = useState(false)
  const markdown = readLatestMarkdown(task?.markdown)

  const outline = markdown ? extractOutline(markdown) : []

  const copyMarkdown = async () => {
    try {
      await navigator.clipboard.writeText(markdown)
      toast.success(t('common.copied'))
    } catch {
      toast.error(t('common.error'))
    }
  }

  const downloadMarkdown = () => {
    const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = (task?.audioMeta?.title || task?.id || 'note').replace(/[\\/:*?"<>|]/g, '_') + '.md'
    anchor.click()
    URL.revokeObjectURL(url)
  }

  const downloadPdf = async () => {
    if (!task?.id || task.status !== 'SUCCESS') return
    setExportingPdf(true)
    try {
      const blob = await downloadNotePdf(task.id)
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = (task.audioMeta?.title || task.id || 'note').replace(/[\\/:*?"<>|]/g, '_') + '.pdf'
      anchor.click()
      // Give the browser time to start reading the download before releasing it.
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t('note.pdfFailed'))
    } finally {
      setExportingPdf(false)
    }
  }

  return (
    <div className="flex min-h-0 flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Tabs
          value={tab}
          onChange={next => setTab(next as ReaderTab)}
          items={[
            { id: 'note', label: t('note.tab.markdown') },
            { id: 'transcript', label: t('note.tab.transcript') },
          ]}
          className="border-b-0"
        />
        {tab === 'note' && markdown ? (
          <div className="flex flex-wrap items-center gap-1">
            <Button size="sm" variant="ghost" onClick={copyMarkdown}>
              <Copy className="size-3.5" />
              <span className="hidden sm:inline">{t('note.copyMarkdown')}</span>
            </Button>
            <Button size="sm" variant="ghost" onClick={downloadMarkdown}>
              <Download className="size-3.5" />
              <span className="hidden sm:inline">{t('note.download')}</span>
            </Button>
            <Button
              size="sm"
              variant="ghost"
              aria-label={t('note.downloadPdf')}
              onClick={downloadPdf}
              loading={exportingPdf}
              disabled={task?.status !== 'SUCCESS'}
            >
              <FileDown className="size-3.5" />
              <span>{t('note.downloadPdf')}</span>
            </Button>
          </div>
        ) : null}
      </div>

      {tab === 'note' ? (
        markdown ? (
          <div className="flex min-h-0 gap-6">
            {outline.length > 2 ? (
              <aside className="sticky top-4 hidden w-56 shrink-0 self-start xl:block">
                <p className="ember-overline mb-2 flex items-center gap-1.5">
                  <ListTree className="size-3" />
                  {t('note.outline')}
                </p>
                <nav className="flex flex-col gap-1 border-l border-line">
                  {outline.map(item => (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => scrollToAnchor(item.id)}
                      className="truncate border-l-2 border-transparent py-0.5 pl-3 text-left text-caption text-ink-soft transition-colors hover:border-ember hover:text-ember"
                      style={{ paddingLeft: (item.depth - 2) * 10 + 12 }}
                    >
                      {item.text}
                    </button>
                  ))}
                </nav>
              </aside>
            ) : null}
            <div className="min-w-0 flex-1">
              <MarkdownNote markdown={markdown} />
            </div>
          </div>
        ) : (
          <EmptyState icon={<FileText className="size-6" />} title={t('note.empty')} />
        )
      ) : null}

      {tab === 'transcript' ? <TranscriptView task={task} /> : null}
    </div>
  )
}
