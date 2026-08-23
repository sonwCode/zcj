import { Fragment, useEffect, useState } from 'react'
import { apiFetch } from '@/lib/utils'
import { useI18n } from '@/lib/i18n-context'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import {
  Activity,
  CircleOff,
  Edit3,
  Globe2,
  Plus,
  RefreshCw,
  Save,
  ShieldCheck,
  ToggleLeft,
  ToggleRight,
  Trash2,
  X,
} from 'lucide-react'

type ProxyItem = {
  id: number
  url: string
  region?: string
  success_count: number
  fail_count: number
  is_active: boolean
  last_checked?: string | null
}

type ProxyCheckResult = {
  ok: boolean
  proxy?: string
  provider?: string
  error_code?: string
  check_url?: string
  status_code?: number
  body?: string
  error?: string
  detail?: string
  suggestions?: string[]
}

function formatCheckResult(result: ProxyCheckResult) {
  if (result.ok) {
    const body = result.body ? `: ${result.body}` : ''
    const via = result.check_url ? ` via ${result.check_url}` : ''
    return `OK${body}${via}`
  }
  const provider = result.provider ? `${result.provider} · ` : ''
  return `失败：${provider}${result.error || '检测端未返回具体错误'}`
}

function maskProxyPassword(url: string) {
  return String(url || '').replace(/(\/\/[^:@/]+:)([^@/]+)(@)/, '$1••••$3')
}

export default function Proxies() {
  const { t } = useI18n()
  const [proxies, setProxies] = useState<ProxyItem[]>([])
  const [newProxy, setNewProxy] = useState('')
  const [region, setRegion] = useState('')
  const [checking, setChecking] = useState(false)
  const [checkingIds, setCheckingIds] = useState<Record<number, boolean>>({})
  const [checkResults, setCheckResults] = useState<Record<number, ProxyCheckResult>>({})
  const [editId, setEditId] = useState<number | null>(null)
  const [editUrl, setEditUrl] = useState('')
  const [editRegion, setEditRegion] = useState('')

  const load = () => apiFetch('/proxies').then(setProxies)

  useEffect(() => { load() }, [])

  const add = async () => {
    if (!newProxy.trim()) return
    const lines = newProxy.trim().split('\n').map(l => l.trim()).filter(Boolean)
    if (lines.length > 1) {
      await apiFetch('/proxies/bulk', {
        method: 'POST',
        body: JSON.stringify({ proxies: lines, region }),
      })
    } else {
      await apiFetch('/proxies', {
        method: 'POST',
        body: JSON.stringify({ url: lines[0], region }),
      })
    }
    setNewProxy('')
    await load()
  }

  const del = async (id: number) => {
    await apiFetch(`/proxies/${id}`, { method: 'DELETE' })
    await load()
  }

  const toggle = async (id: number) => {
    await apiFetch(`/proxies/${id}/toggle`, { method: 'PATCH' })
    await load()
  }

  const check = async () => {
    setChecking(true)
    await apiFetch('/proxies/check', { method: 'POST' })
    setTimeout(() => { load(); setChecking(false) }, 3000)
  }

  const checkOne = async (id: number) => {
    setCheckingIds((prev) => ({ ...prev, [id]: true }))
    try {
      const data = await apiFetch(`/proxies/${id}/check`, { method: 'POST' })
      setCheckResults((prev) => ({ ...prev, [id]: data.result }))
      if (data.proxy) {
        setProxies((prev) => prev.map((item) => (item.id === id ? data.proxy : item)))
      }
    } catch (error) {
      setCheckResults((prev) => ({
        ...prev,
        [id]: { ok: false, error: error instanceof Error ? error.message : String(error) },
      }))
    } finally {
      setCheckingIds((prev) => ({ ...prev, [id]: false }))
    }
  }

  const startEdit = (proxy: ProxyItem) => {
    setEditId(proxy.id)
    setEditUrl(proxy.url)
    setEditRegion(proxy.region || '')
  }

  const cancelEdit = () => {
    setEditId(null)
    setEditUrl('')
    setEditRegion('')
  }

  const saveEdit = async (id: number) => {
    if (!editUrl.trim()) return
    const updated = await apiFetch(`/proxies/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ url: editUrl, region: editRegion }),
    })
    setProxies((prev) => prev.map((item) => (item.id === id ? updated : item)))
    setCheckResults((prev) => {
      const next = { ...prev }
      delete next[id]
      return next
    })
    cancelEdit()
  }

  const activeCount = proxies.filter((item) => item.is_active).length
  const totalSuccess = proxies.reduce((sum, item) => sum + Number(item.success_count || 0), 0)
  const totalFail = proxies.reduce((sum, item) => sum + Number(item.fail_count || 0), 0)
  const metricCards = [
    { label: t('proxies.metric.count'), value: proxies.length, icon: Globe2, tone: 'text-[var(--accent)]' },
    { label: t('proxies.metric.enabled'), value: activeCount, icon: ShieldCheck, tone: 'text-[var(--tone-success)]' },
    { label: t('proxies.metric.success'), value: totalSuccess, icon: Activity, tone: 'text-[var(--accent)]' },
    { label: t('proxies.metric.fail'), value: totalFail, icon: CircleOff, tone: 'text-[var(--tone-danger)]' },
  ]

  return (
    <div className="space-y-4">
      <Card className="overflow-hidden p-2.5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <div className="text-sm font-semibold text-[var(--text-primary)]">{t('proxies.title')}</div>
            <Badge variant="default">{t('common.total')} {proxies.length}</Badge>
            <Badge variant="secondary">{t('proxies.activeBadge', { count: activeCount })}</Badge>
          </div>
          <Button variant="outline" size="sm" onClick={check} disabled={checking}>
            <RefreshCw className={`h-4 w-4 mr-1.5 ${checking ? 'animate-spin' : ''}`} />
            {t('proxies.checkAll')}
          </Button>
        </div>
      </Card>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        {metricCards.map(({ label, value, icon: Icon, tone }) => (
          <Card key={label} className="bg-transparent">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">{label}</div>
                <div className="mt-1.5 text-xl font-semibold tracking-[-0.03em] text-[var(--text-primary)]">{value}</div>
              </div>
              <div className="flex h-9 w-9 items-center justify-center rounded-lg border border-[var(--border-soft)] bg-[var(--chip-bg)]">
                <Icon className={`h-5 w-5 ${tone}`} />
              </div>
            </div>
          </Card>
        ))}
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,330px)_minmax(0,1fr)]">
        <Card className="bg-[var(--bg-pane)]/60">
          <div className="space-y-4">
            <div>
              <div className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">{t('common.add')}</div>
              <div className="mt-1 text-sm font-medium text-[var(--text-primary)]">{t('proxies.addTitle')}</div>
            </div>
            <textarea
              value={newProxy}
              onChange={e => setNewProxy(e.target.value)}
              placeholder={"host:port\nuser:pass@host:port\nhost:port:user:pass"}
              rows={8}
              className="control-surface control-surface-mono resize-none"
            />
            <input
              value={region}
              onChange={e => setRegion(e.target.value)}
              placeholder={t('proxies.regionPlaceholder')}
              className="control-surface"
            />
            <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)]/45 px-3.5 py-2.5 text-xs leading-5 text-[var(--text-secondary)]">
              支持格式：host:port、http://user:pass@host:port、user:pass@host:port、host:port:user:pass、socks5://host:port。检测会自动补全协议并使用多个 IP 探测站。
            </div>
            <Button onClick={add} className="w-full">
              <Plus className="h-4 w-4 mr-1.5" />
              {t('proxies.addToPool')}
            </Button>
          </div>
        </Card>

        <Card className="overflow-hidden p-0">
          <div className="border-b border-[var(--border)] px-4 py-3 text-sm font-medium text-[var(--text-primary)]">
            {t('proxies.list')}
          </div>
          <div className="glass-table-wrap">
            <table className="w-full min-w-[920px] text-sm">
              <thead>
                <tr className="border-b border-[var(--border)] text-[var(--text-muted)]">
                  <th className="px-4 py-2.5 text-left">{t('proxies.address')}</th>
                  <th className="px-4 py-2.5 text-left">{t('proxies.region')}</th>
                  <th className="px-4 py-2.5 text-left">{t('proxies.successFailure')}</th>
                  <th className="px-4 py-2.5 text-left">{t('common.status')}</th>
                  <th className="px-4 py-2.5 text-left">{t('common.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {proxies.length === 0 && (
                  <tr>
                    <td colSpan={5} className="px-4 py-8">
                      <div className="empty-state-panel">{t('proxies.empty')}</div>
                    </td>
                  </tr>
                )}
                {proxies.map(p => {
                  const isEditing = editId === p.id
                  const rowChecking = Boolean(checkingIds[p.id])
                  const result = checkResults[p.id]
                  return (
                    <Fragment key={p.id}>
                      <tr className="border-b border-[var(--border)]/40 hover:bg-[var(--bg-hover)]/70">
                        <td className="px-4 py-2.5 font-mono text-xs text-[var(--text-secondary)]">
                          {isEditing ? (
                            <input
                              value={editUrl}
                              onChange={(event) => setEditUrl(event.target.value)}
                              className="control-surface control-surface-mono h-9"
                            />
                          ) : (
                            <span title="完整认证信息可通过编辑查看">{maskProxyPassword(p.url)}</span>
                          )}
                        </td>
                        <td className="px-4 py-2.5 text-[var(--text-muted)]">
                          {isEditing ? (
                            <input
                              value={editRegion}
                              onChange={(event) => setEditRegion(event.target.value)}
                              className="control-surface h-9 w-24"
                              placeholder="BR"
                            />
                          ) : (p.region || '-')}
                        </td>
                        <td className="px-4 py-2.5">
                          <span className="text-[var(--tone-success)]">{p.success_count}</span>
                          <span className="text-[var(--text-muted)]"> / </span>
                          <span className="text-[var(--tone-danger)]">{p.fail_count}</span>
                        </td>
                        <td className="px-4 py-2.5">
                          <Badge variant={p.is_active ? 'success' : 'danger'}>
                            {p.is_active ? t('common.active') : t('common.disabled')}
                          </Badge>
                        </td>
                        <td className="px-4 py-2.5">
                          <div className="flex flex-wrap items-center gap-2">
                            {isEditing ? (
                              <>
                                <button onClick={() => saveEdit(p.id)} className="table-action-btn">
                                  <Save className="mr-1.5 h-4 w-4" />
                                  保存
                                </button>
                                <button onClick={cancelEdit} className="table-action-btn">
                                  <X className="mr-1.5 h-4 w-4" />
                                  取消
                                </button>
                              </>
                            ) : (
                              <>
                                <button onClick={() => checkOne(p.id)} className="table-action-btn" disabled={rowChecking}>
                                  <RefreshCw className={`mr-1.5 h-4 w-4 ${rowChecking ? 'animate-spin' : ''}`} />
                                  检测
                                </button>
                                <button onClick={() => startEdit(p)} className="table-action-btn">
                                  <Edit3 className="mr-1.5 h-4 w-4" />
                                  编辑
                                </button>
                                <button onClick={() => toggle(p.id)} className="table-action-btn">
                                  {p.is_active ? <ToggleRight className="mr-1.5 h-4 w-4" /> : <ToggleLeft className="mr-1.5 h-4 w-4" />}
                                  {p.is_active ? t('proxies.disable') : t('common.enabled')}
                                </button>
                                <button onClick={() => del(p.id)} className="table-action-btn table-action-btn-danger">
                                  <Trash2 className="mr-1.5 h-4 w-4" />
                                  {t('common.delete')}
                                </button>
                              </>
                            )}
                          </div>
                        </td>
                      </tr>
                      {result && (
                        <tr className="border-b border-[var(--border)]/40 bg-[var(--bg-pane)]/55">
                          <td colSpan={5} className={`px-4 py-2 text-xs leading-5 ${result.ok ? 'text-[var(--tone-success)]' : 'text-[var(--tone-danger)]'}`}>
                            <div className="whitespace-pre-wrap break-all">{formatCheckResult(result)}</div>
                            {!result.ok && Array.isArray(result.suggestions) && result.suggestions.length > 0 && (
                              <ul className="mt-1.5 list-disc space-y-0.5 pl-5 text-[var(--text-muted)]">
                                {result.suggestions.map((suggestion) => <li key={suggestion}>{suggestion}</li>)}
                              </ul>
                            )}
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
    </div>
  )
}
