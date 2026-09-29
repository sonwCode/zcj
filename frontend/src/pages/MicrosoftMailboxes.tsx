import { useEffect, useState, type FormEvent } from 'react'
import { Inbox, Mail, RefreshCw, Search } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { apiFetch } from '@/lib/utils'

type MailboxStats = {
  total: number
  capacity: number
  used: number
  remaining: number
  exhausted: number
}

type Message = {
  id: string
  subject?: string
  bodyPreview?: string
  body?: string
  receivedDateTime?: string
  from?: { emailAddress?: { name?: string; address?: string } } | string
  to?: string
  toRecipients?: Array<{ emailAddress?: { name?: string; address?: string } }>
}

function formatTime(value?: string) {
  if (!value) return ''
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString()
}

function fromLabel(msg: Message): string {
  const f = msg.from as any
  if (typeof f === 'string') return f
  return f?.emailAddress?.address || f?.emailAddress?.name || ''
}

export default function MicrosoftMailboxes() {
  const [stats, setStats] = useState<MailboxStats | null>(null)
  const [query, setQuery] = useState('')
  const [messages, setMessages] = useState<Message[]>([])
  const [queryEmail, setQueryEmail] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const loadStats = () => {
    apiFetch('/microsoft-mailboxes/stats')
      .then((d: MailboxStats) => setStats(d))
      .catch((e: any) => setError(e?.message || '加载统计失败'))
  }

  useEffect(() => { loadStats() }, [])

  const searchMessages = async (event: FormEvent) => {
    event.preventDefault()
    const email = query.trim()
    if (!email) return
    setLoading(true)
    setError('')
    try {
      const d = await apiFetch(`/microsoft-mailboxes/${encodeURIComponent(email)}/messages`)
      setMessages(d.messages || [])
      setQueryEmail(d.email || email)
    } catch (err: any) {
      setMessages([])
      setQueryEmail('')
      setError(err?.message || '查询邮件失败')
    } finally {
      setLoading(false)
    }
  }

  const statCards = [
    { label: '邮箱总数', value: stats?.total ?? '-', key: 'total' },
    { label: '已使用次数', value: stats?.used ?? '-', key: 'used' },
    { label: '剩余可用次数', value: stats?.remaining ?? '-', key: 'remaining' },
    { label: '已耗尽邮箱', value: stats?.exhausted ?? '-', key: 'exhausted' },
  ]

  return (
    <div className="space-y-4">
      <Card className="border border-[var(--border)] bg-[var(--bg-pane)]/40 p-5">
        <div className="flex items-center gap-2">
          <Inbox className="h-5 w-5 text-[var(--text-secondary)]" />
          <h1 className="text-xl font-semibold text-[var(--text-primary)]">微软邮箱池</h1>
        </div>
        <p className="mt-1 text-sm text-[var(--text-muted)]">统计邮箱池总用量，并可按邮箱查询其收到的信件。</p>
        <div className="mt-4 grid gap-3 sm:grid-cols-4">
          {statCards.map(card => (
            <div key={card.key} className="rounded-md border border-[var(--border)] bg-[var(--bg-pane)]/50 px-4 py-3">
              <div className="text-xs text-[var(--text-muted)]">{card.label}</div>
              <div className="mt-1 text-2xl font-semibold text-[var(--text-primary)]">{card.value}</div>
            </div>
          ))}
        </div>
        <div className="mt-3 flex items-center justify-end">
          <Button size="sm" variant="outline" onClick={loadStats}>
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" /> 刷新
          </Button>
        </div>
      </Card>

      <Card className="border border-[var(--border)] bg-[var(--bg-pane)]/40 p-5">
        <form onSubmit={searchMessages} className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <label className="flex-1">
            <span className="mb-1 block text-xs text-[var(--text-muted)]">查询邮箱信件</span>
            <input
              value={query}
              onChange={event => setQuery(event.target.value)}
              placeholder="输入父邮箱地址，如 xxx@outlook.com"
              className="w-full rounded-md border border-[var(--border)] bg-transparent px-3 py-2 text-sm text-[var(--text-primary)]"
            />
          </label>
          <Button type="submit" size="sm" disabled={loading || !query.trim()}>
            {loading ? '查询中…' : (
              <>
                <Search className="mr-1.5 h-3.5 w-3.5" /> 查询信件
              </>
            )}
          </Button>
        </form>
        {error ? <div className="mt-3 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-400">{error}</div> : null}
        {queryEmail ? (
          <div className="mt-4">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-medium text-[var(--text-primary)]">{queryEmail} 的收件箱（{messages.length} 封）</h2>
            </div>
            <div className="mt-2 divide-y divide-[var(--border)]">
              {messages.length === 0 ? (
                <p className="py-4 text-center text-sm text-[var(--text-muted)]">暂无信件</p>
              ) : messages.map((msg, idx) => (
                <div key={msg.id || idx} className="flex flex-col gap-1 py-3">
                  <div className="flex items-center gap-2">
                    <Mail className="h-3.5 w-3.5 shrink-0 text-[var(--text-muted)]" />
                    <span className="font-medium text-[var(--text-primary)]">{msg.subject || '(无主题)'}</span>
                    <span className="ml-auto shrink-0 text-xs text-[var(--text-muted)]">{formatTime(msg.receivedDateTime)}</span>
                  </div>
                  <div className="text-xs text-[var(--text-secondary)]">发件人：{fromLabel(msg) || '-'}</div>
                  {(msg.bodyPreview || msg.body) ? (
                    <div className="max-h-24 overflow-y-auto whitespace-pre-wrap break-words rounded bg-[var(--bg-pane)]/40 px-2 py-1.5 text-xs text-[var(--text-muted)]">
                      {(msg.bodyPreview || msg.body || '').slice(0, 500)}
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          </div>
        ) : null}
      </Card>
    </div>
  )
}
