import { memo, useEffect, useState, useRef, useCallback } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate, useParams } from 'react-router-dom'
import { getPlatforms } from '@/lib/app-data'
import { apiDownload, apiFetch, triggerBrowserDownload } from '@/lib/utils'
import { formatDateTime, translateAccountStatus } from '@/lib/i18n'
import { useI18n } from '@/lib/i18n-context'
import { TaskLogPanel } from '@/components/tasks/TaskLogPanel'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { getTaskStatusText, TASK_STATUS_VARIANTS } from '@/lib/tasks'
import { RefreshCw, Copy, ExternalLink, Download, Upload, Plus, X, Trash2, Zap, Loader2, ShieldCheck, Users, Ban, AlertTriangle, Gauge } from 'lucide-react'

const STATUS_VARIANT: Record<string, any> = {
  registered: 'default', trial: 'success', subscribed: 'success',
  expired: 'warning', invalid: 'danger',
  free: 'secondary', eligible: 'secondary', valid: 'success', unknown: 'secondary',
}

const platformActionsCache = new Map<string, any[]>()
const platformActionsPromiseCache = new Map<string, Promise<any[]>>()

const BROWSER_MODE_OPTIONS = [
  { value: 'camoufox_headed', label: 'Camoufox Headed' },
  { value: 'camoufox_headless', label: 'Camoufox Headless' },
  { value: 'bitbrowser_headed', label: 'BitBrowser Headed' },
  { value: 'bitbrowser_hidden', label: 'BitBrowser Hidden' },
  { value: 'bitbrowser_headless', label: 'BitBrowser Headless' },
]

const ACCOUNT_TOOL_BUTTON_CLASS = 'h-8 shrink-0 whitespace-nowrap bg-transparent'
function getAccountOverview(acc: any) {
  return acc?.overview || {}
}

function getDisplaySummary(acc: any) {
  return acc?.display_summary && typeof acc.display_summary === 'object' ? acc.display_summary : {}
}

function getVerificationMailbox(acc: any) {
  const providerResources = Array.isArray(acc?.provider_resources) ? acc.provider_resources : []
  const normalized = providerResources.find((item: any) => item?.resource_type === 'mailbox')
  if (normalized) {
    return {
      provider: normalized.provider_name,
      email: normalized.handle || normalized.display_name,
      account_id: normalized.resource_identifier,
    }
  }
  return null
}

function getLifecycleStatus(acc: any) {
  return getDisplaySummary(acc)?.status?.lifecycle || acc?.lifecycle_status || 'registered'
}

function getDisplayStatus(acc: any) {
  return getDisplaySummary(acc)?.status?.display || acc?.display_status || acc?.plan_state || getLifecycleStatus(acc)
}

function getPlanState(acc: any) {
  return getDisplaySummary(acc)?.status?.plan_state || acc?.plan_state || acc?.overview?.plan_state || 'unknown'
}

function getValidityStatus(acc: any) {
  return getDisplaySummary(acc)?.status?.validity || acc?.validity_status || acc?.overview?.validity_status || 'unknown'
}

function getValidityReason(acc: any) {
  const overview = getAccountOverview(acc)
  return String(overview?.validity_reason || overview?.check_error || '').trim()
}

function getSub2SyncInfo(acc: any) {
  const state = getAccountOverview(acc)?.legacy_extra?.sub2api_sync
  if (!state || typeof state !== 'object') return null
  const status = String(state.status || '').trim().toLowerCase()
  const labels: Record<string, string> = {
    credentials_pending: '等待 Codex 凭据',
    identity_pending: '等待 Agent Identity',
    registry_pending: 'Agent Registry 等待重试',
    registry_ineligible: 'Agent Registry 未开放',
    imported_active: 'Sub2 可调度',
    imported_cooling: 'Sub2 额度冷却',
    imported_unschedulable: 'Sub2 不可调度',
    imported_unverified: 'Sub2 待验证',
    sync_pending: 'Sub2 等待重试',
    sync_failed: 'Sub2 同步失败',
    invalid: 'Sub2 凭据失效',
    deleted: 'Sub2 已清理',
    active: 'Sub2 已导入',
    ineligible: 'Agent Registry 未开放',
  }
  const tone = status === 'imported_active'
    ? 'success'
    : status === 'imported_cooling' || status.endsWith('_pending') || status === 'registry_pending'
      ? 'warning'
      : ['invalid', 'sync_failed', 'registry_ineligible', 'ineligible'].includes(status)
        ? 'danger'
        : 'secondary'
  return {
    status,
    label: labels[status] || `Sub2 ${status || '未知'}`,
    tone,
    title: String(state.last_error || state.remote_error_message || state.next_retry_at || ''),
  }
}

function parseAccountTime(value: unknown) {
  if (!value) return null
  const parsed = new Date(String(value)).getTime()
  return Number.isFinite(parsed) ? parsed : null
}

function formatSurvivalDuration(durationMs: number) {
  const totalMinutes = Math.max(Math.floor(durationMs / 60000), 0)
  if (totalMinutes < 1) return '不足 1 分钟'
  if (totalMinutes < 60) return `${totalMinutes} 分钟`
  const totalHours = Math.floor(totalMinutes / 60)
  const minutes = totalMinutes % 60
  if (totalHours < 24) return `${totalHours} 小时${minutes ? ` ${minutes} 分钟` : ''}`
  const days = Math.floor(totalHours / 24)
  const hours = totalHours % 24
  return `${days} 天${hours ? ` ${hours} 小时` : ''}`
}

function getSurvivalInfo(acc: any, nowMs: number) {
  const startedAt = parseAccountTime(acc?.created_at)
  if (startedAt === null) return { state: 'unknown', label: '时间未知', duration: '-', title: '' }

  const overview = getAccountOverview(acc)
  const status = getDisplaySummary(acc)?.status || {}
  const validity = String(getValidityStatus(acc) || '').toLowerCase()
  const lifecycle = String(getLifecycleStatus(acc) || '').toLowerCase()
  const invalid = validity === 'invalid' || ['invalid', 'disabled', 'banned', 'deactivated'].includes(lifecycle)
  const checkedAt = parseAccountTime(
    overview?.invalid_detected_at
      || overview?.deactivated_at
      || status?.checked_at
      || overview?.checked_at
      || acc?.updated_at,
  )
  const endedAt = invalid && checkedAt !== null ? checkedAt : nowMs
  const duration = formatSurvivalDuration(Math.max(endedAt - startedAt, 0))
  const state = invalid ? 'invalid' : validity === 'valid' ? 'valid' : 'unknown'
  const label = invalid ? '存活时长' : validity === 'valid' ? '已存活' : '注册至今'
  return {
    state,
    label,
    duration,
    title: `${new Date(startedAt).toLocaleString()} - ${invalid ? new Date(endedAt).toLocaleString() : '现在'}`,
  }
}

function isDisabledAccount(acc: any) {
  const lifecycle = String(getLifecycleStatus(acc) || '').toLowerCase()
  const display = String(getDisplayStatus(acc) || '').toLowerCase()
  return ['disabled', 'banned', 'expired', 'suspended'].some(flag =>
    lifecycle.includes(flag) || display.includes(flag)
  )
}

function isQuotaExhaustedAccount(acc: any) {
  const overview = getAccountOverview(acc)
  const planState = String(getPlanState(acc) || '').toLowerCase()
  const display = String(getDisplayStatus(acc) || '').toLowerCase()
  const rawRemaining = overview?.remaining_credits
  const hasRemainingValue = rawRemaining !== undefined && rawRemaining !== null && rawRemaining !== ''
  return (
    planState.includes('exhaust') ||
    planState.includes('quota') ||
    display.includes('exhaust') ||
    display.includes('额度耗尽') ||
    (hasRemainingValue && Number(rawRemaining) === 0)
  )
}

function getCompactStatusMeta(acc: any) {
  const summary = getDisplaySummary(acc)
  const primaryMetrics = Array.isArray(summary?.primary_metrics) ? summary.primary_metrics : []
  if (primaryMetrics.length > 0) {
    return primaryMetrics.slice(0, 2).map((item: any) => {
      const sub = item?.sub ? ` · ${item.sub}` : ''
      return `${item?.label || ''}:${item?.value || '-'}${sub}`
    }).join(' / ')
  }
  const overview = getAccountOverview(acc)
  const parts = [
    `生命周期:${getLifecycleStatus(acc)}`,
    `套餐:${getPlanState(acc)}`,
    `有效:${getValidityStatus(acc)}`,
  ]
  const remainingCredits = overview?.remaining_credits
  const usageTotal = overview?.usage_total
  if (remainingCredits || usageTotal) {
    parts.push(`额度:${remainingCredits || '-'} / 已用:${usageTotal || '-'}`)
  }
  return parts.join(' / ')
}

function getPrimaryMetrics(acc: any) {
  const metrics = getDisplaySummary(acc)?.primary_metrics
  return Array.isArray(metrics) ? metrics : []
}

function getSecondaryMetrics(acc: any) {
  const metrics = getDisplaySummary(acc)?.secondary_metrics
  return Array.isArray(metrics) ? metrics : []
}

function getDisplayWarnings(acc: any) {
  const warnings = getDisplaySummary(acc)?.warnings
  return Array.isArray(warnings) ? warnings : []
}

function getDisplayBadges(acc: any) {
  const badges = getDisplaySummary(acc)?.badges
  return Array.isArray(badges) ? badges : []
}

function getDisplaySections(acc: any) {
  const sections = getDisplaySummary(acc)?.sections
  return Array.isArray(sections) ? sections : []
}

function getProviderAccounts(acc: any) {
  return Array.isArray(acc?.provider_accounts) ? acc.provider_accounts : []
}

function getCredentials(acc: any) {
  return Array.isArray(acc?.credentials) ? acc.credentials : []
}

function getCashierUrl(acc: any) {
  const overview = getAccountOverview(acc)
  return overview?.cashier_url || acc?.cashier_url || ''
}

function getPrimaryToken(acc: any) {
  if (acc?.primary_token) return acc.primary_token
  const credential = getCredentials(acc).find((item: any) => item?.scope === 'platform' && item?.credential_type === 'token' && item?.value)
  return credential?.value || ''
}

function escapeCsvField(value: unknown) {
  const text = value == null ? '' : String(value)
  if (!/[",\n\r]/.test(text)) return text
  return `"${text.replace(/"/g, '""')}"`
}

async function loadPlatformActions(platform: string, options?: { force?: boolean }) {
  const key = String(platform || '').trim()
  if (!key) return []
  const force = Boolean(options?.force)
  if (!force && platformActionsCache.has(key)) {
    return platformActionsCache.get(key) || []
  }
  if (!force && platformActionsPromiseCache.has(key)) {
    return platformActionsPromiseCache.get(key) || []
  }
  const pending = apiFetch(`/actions/${key}`)
    .then((data) => {
      const actions = Array.isArray(data?.actions) ? data.actions : []
      platformActionsCache.set(key, actions)
      platformActionsPromiseCache.delete(key)
      return actions
    })
    .catch((error) => {
      platformActionsPromiseCache.delete(key)
      throw error
    })
  platformActionsPromiseCache.set(key, pending)
  return pending
}

function buildActionParamDraft(action: any, acc: any) {
  const params = Array.isArray(action?.params) ? action.params : []
  const emailPrefix = String(acc?.email || '').split('@')[0] || 'Development'
  const draft: Record<string, string> = {}
  params.forEach((param: any) => {
    if (action?.id === 'create_api_key' && param?.key === 'name') {
      draft[param.key] = `${emailPrefix}Development`
      return
    }
    if (Array.isArray(param?.options) && param.options.length > 0) {
      const firstOption = param.options[0]
      draft[param?.key || ''] = String(
        firstOption && typeof firstOption === 'object'
          ? firstOption.value ?? ''
          : firstOption ?? '',
      )
      return
    }
    draft[param?.key || ''] = ''
  })
  return draft
}

function Sub2StatCard({
  icon: Icon,
  title,
  value,
  subtitle,
  tone,
}: {
  icon: any
  title: string
  value: number | string
  subtitle: string
  tone: 'cyan' | 'green' | 'amber' | 'red' | 'violet'
}) {
  const tones = {
    cyan: 'bg-cyan-50 text-cyan-600',
    green: 'bg-emerald-50 text-emerald-600',
    amber: 'bg-amber-50 text-amber-600',
    red: 'bg-rose-50 text-rose-600',
    violet: 'bg-violet-50 text-violet-600',
  }
  return (
    <div className="group flex min-h-[88px] items-center gap-4 rounded-xl border border-slate-200 bg-white px-5 py-4 shadow-sm transition-all duration-200 hover:-translate-y-0.5 hover:border-slate-300 hover:shadow-md">
      <div className={`flex h-12 w-12 shrink-0 items-center justify-center rounded-lg ${tones[tone]}`}>
        <Icon className="h-5 w-5" />
      </div>
      <div className="min-w-0">
        <div className="text-xs font-medium text-slate-500">{title}</div>
        <div className="mt-1 text-2xl font-bold leading-none tracking-tight text-slate-950">{value}</div>
        <div className="mt-2 truncate text-xs text-slate-500">{subtitle}</div>
      </div>
    </div>
  )
}

// ── 注册弹框 ────────────────────────────────────────────────
// ── 新增账号弹框 ─────────────────────────────────────────
function AddModal({ platform, onClose, onDone }: { platform: string; onClose: () => void; onDone: () => void }) {
  const [form, setForm] = useState({ email: '', password: '', lifecycle_status: 'registered', primary_token: '', cashier_url: '' })
  const [saving, setSaving] = useState(false)
  const set = (k: string, v: string) => setForm(f => ({ ...f, [k]: v }))

  const save = async () => {
    setSaving(true)
    try {
      await apiFetch('/accounts', {
        method: 'POST',
        body: JSON.stringify({ ...form, platform }),
      })
      onDone()
    } finally { setSaving(false) }
  }

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-sm"
           onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)]">
          <h2 className="text-base font-semibold text-[var(--text-primary)]">手动新增账号</h2>
          <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"><X className="h-4 w-4" /></button>
        </div>
        <div className="px-6 py-4 space-y-3">
          {[['email','邮箱','text'],['password','密码','text'],['primary_token','主凭证','text'],['cashier_url','试用链接','text']].map(([k,l,t]) => (
            <div key={k}>
              <label className="text-xs text-[var(--text-muted)] block mb-1">{l}</label>
              <input type={t} value={(form as any)[k]} onChange={e => set(k, e.target.value)}
                className="control-surface" />
            </div>
          ))}
          <div>
            <label className="text-xs text-[var(--text-muted)] block mb-1">生命周期状态</label>
            <select value={form.lifecycle_status} onChange={e => set('lifecycle_status', e.target.value)}
              className="control-surface appearance-none">
              <option value="registered">已注册</option>
              <option value="trial">试用中</option>
              <option value="subscribed">已订阅</option>
            </select>
          </div>
        </div>
        <div className="flex gap-3 px-6 py-4 border-t border-[var(--border)]">
          <Button onClick={save} disabled={saving} className="flex-1">{saving ? '保存中...' : '保存'}</Button>
          <Button variant="outline" onClick={onClose} className="flex-1">取消</Button>
        </div>
      </div>
    </div>
  )
}

function formatResultValue(value: any) {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'boolean') return value ? '是' : '否'
  return String(value)
}

function ResultStat({ label, value }: { label: string; value: any }) {
  return (
    <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2">
      <div className="text-[11px] uppercase tracking-[0.16em] text-[var(--text-muted)]">{label}</div>
      <div className="mt-1 text-sm font-medium text-[var(--text-primary)] break-all">{formatResultValue(value)}</div>
    </div>
  )
}

function metricToneClass(tone?: string) {
  if (tone === 'good') return 'border-emerald-500/25 bg-emerald-500/10 text-[var(--tone-success)]'
  if (tone === 'warning') return 'border-amber-500/30 bg-amber-500/10 text-[var(--tone-warning)]'
  if (tone === 'danger') return 'border-red-500/25 bg-red-500/10 text-[var(--tone-danger)]'
  return 'border-[var(--border)] bg-[var(--bg-hover)] text-[var(--text-primary)]'
}

function metricAccentClass(tone?: string) {
  if (tone === 'good') return 'from-emerald-400/70 to-cyan-300/50'
  if (tone === 'warning') return 'from-amber-300/80 to-orange-300/50'
  if (tone === 'danger') return 'from-red-400/80 to-rose-300/50'
  return 'from-[var(--accent)]/80 to-[var(--accent-strong)]/45'
}

function DisplayMetricCard({ metric, compact = false }: { metric: any; compact?: boolean }) {
  return (
    <div className={`group relative overflow-hidden rounded-lg border px-3.5 py-3 ${metricToneClass(metric?.tone)}`}>
      <div className={`pointer-events-none absolute inset-y-0 left-0 w-1 bg-gradient-to-b ${metricAccentClass(metric?.tone)}`} />
      <div className="relative flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[10px] uppercase tracking-[0.18em] opacity-65">{metric?.label || '-'}</div>
          {metric?.sub ? <div className="mt-1 truncate text-[11px] opacity-65">{metric.sub}</div> : null}
        </div>
        <div className={`${compact ? 'text-sm' : 'text-lg'} shrink-0 font-semibold tracking-[-0.03em]`}>{formatResultValue(metric?.value)}</div>
      </div>
      {typeof metric?.percent === 'number' ? (
        <div className="relative mt-3 h-1.5 overflow-hidden rounded-full bg-black/25">
          <div className={`h-full rounded-full bg-gradient-to-r ${metricAccentClass(metric?.tone)}`} style={{ width: `${Math.max(0, Math.min(100, metric.percent))}%` }} />
        </div>
      ) : null}
    </div>
  )
}

function DisplayWarnings({ warnings }: { warnings: any[] }) {
  if (!warnings.length) return null
  return (
    <div className="space-y-2">
      {warnings.map((item: any, index: number) => (
        <div key={`${item?.key || 'warning'}-${index}`} className={`rounded-xl border px-3 py-2 text-xs ${metricToneClass(item?.tone || 'warning')}`}>
          {item?.message || '-'}
        </div>
      ))}
    </div>
  )
}

function DisplaySections({ sections }: { sections: any[] }) {
  if (!sections.length) return null
  return (
    <div className="space-y-3">
      {sections.map((section: any) => (
        <div key={section?.key || section?.title} className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] p-3">
          <div className="text-xs font-semibold text-[var(--text-primary)]">{section?.title || '明细'}</div>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            {(Array.isArray(section?.items) ? section.items : []).map((item: any, index: number) => (
              <div key={`${item?.title || 'item'}-${index}`} className="rounded-lg border border-[var(--border)] bg-black/20 p-3">
                <div className="text-xs font-semibold text-[var(--text-primary)]">{item?.title || '-'}</div>
                <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-[var(--text-secondary)]">
                  {(Array.isArray(item?.metrics) ? item.metrics : []).map((metric: any) => (
                    <div key={metric?.key || metric?.label}>
                      <span className="text-[var(--text-muted)]">{metric?.label || '-'}: </span>
                      <span>{formatResultValue(metric?.value)}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

function ActionResultHighlights({ payload }: { payload: any }) {
  if (!payload || typeof payload !== 'object') return null

  const stats: Array<{ label: string; value: any }> = []
  if (payload.code) stats.push({ label: '邮箱验证码', value: payload.code })
  if (payload.code && payload.email) stats.push({ label: '验证码邮箱', value: payload.email })
  if ('valid' in payload) stats.push({ label: '账号有效', value: payload.valid })
  if (payload.membership_type) stats.push({ label: '套餐', value: payload.membership_type })
  if (payload.plan) stats.push({ label: '套餐', value: payload.plan })
  if (payload.plan_id) stats.push({ label: 'Plan ID', value: payload.plan_id })
  if (typeof payload.has_valid_payment_method === 'boolean') stats.push({ label: '已绑卡', value: payload.has_valid_payment_method })
  if ('trial_eligible' in payload) stats.push({ label: '可试用', value: payload.trial_eligible })
  if (payload.trial_length_days) stats.push({ label: '试用天数', value: payload.trial_length_days })
  if (payload.remaining_credits) stats.push({ label: '剩余额度', value: payload.remaining_credits })
  if (payload.usage_total) stats.push({ label: '已用额度', value: payload.usage_total })
  if (payload.plan_credits) stats.push({ label: '总额度', value: payload.plan_credits })
  if (payload.usage_summary?.plan_title) stats.push({ label: 'Kiro 套餐', value: payload.usage_summary.plan_title })
  if ('days_until_reset' in (payload.usage_summary || {})) stats.push({ label: '重置倒计时', value: payload.usage_summary?.days_until_reset })
  if (payload.usage_summary?.next_reset_at) stats.push({ label: '下次重置', value: payload.usage_summary.next_reset_at })
  if ('available' in (payload.portal_session || {})) stats.push({ label: 'Portal 可用', value: payload.portal_session?.available })
  if (payload.desktop_app_state?.app_name) stats.push({ label: '桌面应用', value: payload.desktop_app_state?.app_name })
  if ('running' in (payload.desktop_app_state || {})) stats.push({ label: '桌面已打开', value: payload.desktop_app_state?.running })
  if ('ready' in (payload.desktop_app_state || {})) stats.push({ label: '桌面就绪', value: payload.desktop_app_state?.ready })
  if (payload.key_prefix) stats.push({ label: 'API Key 前缀', value: payload.key_prefix })
  if (payload.key_prefix && payload.name) stats.push({ label: 'Key 名称', value: payload.name })
  if (payload.key_prefix && payload.id) stats.push({ label: 'Key ID', value: payload.id })

  const cursorModels = payload.usage_summary?.models && typeof payload.usage_summary.models === 'object'
    ? Object.entries(payload.usage_summary.models)
    : []
  const kiroBreakdowns = Array.isArray(payload.usage_summary?.breakdowns)
    ? payload.usage_summary.breakdowns
    : []
  const kiroPlans = Array.isArray(payload.usage_summary?.plans)
    ? payload.usage_summary.plans
    : []

  if (stats.length === 0 && cursorModels.length === 0 && kiroBreakdowns.length === 0 && kiroPlans.length === 0 && !payload.quota_note) {
    return null
  }

  return (
    <div className="space-y-4 mb-4">
      {stats.length > 0 && (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {stats.map(item => <ResultStat key={item.label} label={item.label} value={item.value} />)}
        </div>
      )}

      {cursorModels.length > 0 && (
        <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] p-4">
          <div className="text-sm font-semibold text-[var(--text-primary)]">Cursor Usage</div>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            {cursorModels.map(([model, info]: [string, any]) => (
              <div key={model} className="rounded-lg border border-[var(--border)] bg-black/20 p-3">
                <div className="text-xs font-semibold text-[var(--text-primary)]">{model}</div>
                <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-[var(--text-secondary)]">
                  <div>请求数: {formatResultValue(info?.num_requests)}</div>
                  <div>总请求: {formatResultValue(info?.num_requests_total)}</div>
                  <div>Token: {formatResultValue(info?.num_tokens)}</div>
                  <div>剩余请求: {formatResultValue(info?.remaining_requests)}</div>
                  <div>请求上限: {formatResultValue(info?.max_request_usage)}</div>
                  <div>Token 上限: {formatResultValue(info?.max_token_usage)}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {kiroBreakdowns.length > 0 && (
        <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] p-4">
          <div className="text-sm font-semibold text-[var(--text-primary)]">Kiro Usage</div>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            {kiroBreakdowns.map((item: any, index: number) => (
              <div key={`${item.resource_type || item.display_name}-${index}`} className="rounded-lg border border-[var(--border)] bg-black/20 p-3">
                <div className="text-xs font-semibold text-[var(--text-primary)]">{item.display_name || item.resource_type}</div>
                <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-[var(--text-secondary)]">
                  <div>已用: {formatResultValue(item.current_usage)}</div>
                  <div>上限: {formatResultValue(item.usage_limit)}</div>
                  <div>剩余: {formatResultValue(item.remaining_usage)}</div>
                  <div>单位: {formatResultValue(item.unit)}</div>
                  <div>试用状态: {formatResultValue(item.trial_status)}</div>
                  <div>试用到期: {formatResultValue(item.trial_expiry)}</div>
                  <div>试用上限: {formatResultValue(item.trial_usage_limit)}</div>
                  <div>试用剩余: {formatResultValue(item.trial_remaining_usage)}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {kiroPlans.length > 0 && (
        <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] p-4">
          <div className="text-sm font-semibold text-[var(--text-primary)]">Kiro Plans</div>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            {kiroPlans.map((plan: any) => (
              <div key={plan.name} className="rounded-lg border border-[var(--border)] bg-black/20 p-3">
                <div className="flex items-center justify-between gap-3">
                  <div className="text-xs font-semibold text-[var(--text-primary)]">{plan.title || plan.name}</div>
                  <div className="text-xs text-emerald-400">{formatResultValue(plan.amount)} {plan.currency || ''}</div>
                </div>
                <div className="mt-1 text-[11px] text-[var(--text-muted)]">{plan.billing_interval || '-'}</div>
                {Array.isArray(plan.features) && plan.features.length > 0 && (
                  <div className="mt-2 text-xs text-[var(--text-secondary)] break-words">
                    {plan.features.join(' · ')}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {payload.quota_note && (
        <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-xs text-amber-200">
          {payload.quota_note}
        </div>
      )}
    </div>
  )
}

function ActionResultModal({
  title,
  payload,
  onClose,
}: {
  title: string
  payload: any
  onClose: () => void
}) {
  const content = typeof payload === 'string' ? payload : JSON.stringify(payload, null, 2)

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="dialog-panel dialog-panel-lg"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)]">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">{title}</h2>
            <p className="text-xs text-[var(--text-muted)] mt-0.5">操作结果</p>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => navigator.clipboard.writeText(content)}>
              <Copy className="h-4 w-4 mr-1" />
              复制
            </Button>
            <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]">
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>
        <div className="px-6 py-4">
          <ActionResultHighlights payload={payload} />
          <pre className="bg-[var(--bg-hover)] border border-[var(--border)] rounded-xl p-4 text-xs text-[var(--text-secondary)] whitespace-pre-wrap break-all overflow-auto max-h-[65vh]">
            {content}
          </pre>
        </div>
      </div>
    </div>
  )
}

function ActionTaskModal({
  title,
  taskId,
  taskStatus,
  onClose,
  onDone,
}: {
  title: string
  taskId: string
  taskStatus: string | null
  onClose: () => void
  onDone: (status: string) => void
}) {
  const { t, language } = useI18n()
  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="dialog-panel flex w-[min(960px,calc(100vw-32px))] max-w-none flex-col overflow-hidden"
        onClick={e => e.stopPropagation()}
        style={{ maxHeight: '90vh' }}
      >
        <div className="relative overflow-hidden border-b border-[var(--border)] px-6 py-5">
          <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_12%_0%,rgba(9,182,162,0.18),transparent_34%),linear-gradient(90deg,rgba(255,255,255,0.04),transparent)]" />
          <div className="relative flex items-start justify-between gap-4">
            <div className="min-w-0">
              <div className="mb-2 inline-flex rounded-full border border-[var(--border)] bg-[var(--chip-bg)] px-3 py-1 text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">
                Platform Action
              </div>
              <h2 className="truncate text-lg font-semibold text-[var(--text-primary)]">{title}</h2>
              <p className="mt-1 text-xs text-[var(--text-muted)]">任务状态、错误摘要与实时日志集中展示</p>
            </div>
            <div className="flex items-center gap-2">
              {taskStatus ? (
                <Badge variant={TASK_STATUS_VARIANTS[taskStatus] || 'secondary'}>
                  {getTaskStatusText(taskStatus, language)}
                </Badge>
              ) : null}
              <button onClick={onClose} className="rounded-full border border-[var(--border)] bg-[var(--bg-hover)] p-2 text-[var(--text-muted)] hover:text-[var(--text-primary)]">
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>
        </div>
        <div className="flex-1 overflow-y-auto px-6 py-5">
          <TaskLogPanel taskId={taskId} onDone={onDone} />
        </div>
        <div className="flex items-center justify-between border-t border-[var(--border)] px-6 py-3 text-xs text-[var(--text-muted)]">
          <span>{t('taskHistory.taskId')}: {taskId}</span>
          <Button variant="outline" size="sm" onClick={onClose}>
            {t('common.close')}
          </Button>
        </div>
      </div>
    </div>
  )
}

function ActionParamsModal({
  action,
  initialValues,
  submitting,
  onClose,
  onSubmit,
}: {
  action: any
  initialValues: Record<string, string>
  submitting: boolean
  onClose: () => void
  onSubmit: (params: Record<string, string>) => void
}) {
  const [form, setForm] = useState<Record<string, string>>(initialValues)

  useEffect(() => {
    setForm(initialValues)
  }, [action?.id, initialValues])

  const params = Array.isArray(action?.params) ? action.params : []
  const visibleParams = params.filter((param: any) => {
    if (action?.id !== 'extract_payment_link') return true
    const method = form.payment_method || 'ideal'
    if (['checkout_proxies', 'promotion_proxies', 'provider_proxies'].includes(param.key)) {
      return method === 'upi'
    }
    if (param.key === 'blik_code') return method === 'blik'
    return true
  })

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="dialog-panel dialog-panel-md flex max-h-[90vh] flex-col"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)]">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">{action?.label || '动作参数'}</h2>
            <p className="text-xs text-[var(--text-muted)] mt-0.5">填写执行该动作所需的参数</p>
          </div>
          <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-6 py-4">
          {visibleParams.map((param: any) => {
            const value = form[param.key] ?? ''
            if (Array.isArray(param.options) && param.options.length > 0) {
              return (
                <label key={param.key} className="block">
                  <div className="mb-1 text-xs text-[var(--text-muted)]">{param.label || param.key}</div>
                  <select
                    value={value}
                    onChange={e => setForm(current => ({ ...current, [param.key]: e.target.value }))}
                    className="w-full rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2 text-sm outline-none focus:border-[var(--text-accent)]"
                  >
                    {param.options.map((option: any, index: number) => {
                      const optionValue = String(
                        option && typeof option === 'object' ? option.value ?? '' : option ?? '',
                      )
                      const optionLabel = String(
                        option && typeof option === 'object' ? option.label ?? optionValue : optionValue,
                      )
                      return <option key={`${optionValue}-${index}`} value={optionValue}>{optionLabel}</option>
                    })}
                  </select>
                </label>
              )
            }
            if (param.type === 'textarea') {
              return (
                <label key={param.key} className="block">
                  <div className="mb-1 text-xs text-[var(--text-muted)]">{param.label || param.key}</div>
                  <textarea
                    value={value}
                    onChange={e => setForm(current => ({ ...current, [param.key]: e.target.value }))}
                    rows={3}
                    className="w-full rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2 text-sm outline-none focus:border-[var(--text-accent)]"
                  />
                </label>
              )
            }
            return (
              <label key={param.key} className="block">
                <div className="mb-1 text-xs text-[var(--text-muted)]">{param.label || param.key}</div>
                <input
                  type={param.type === 'number' ? 'number' : 'text'}
                  value={value}
                  onChange={e => setForm(current => ({ ...current, [param.key]: e.target.value }))}
                  className="w-full rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2 text-sm outline-none focus:border-[var(--text-accent)]"
                />
              </label>
            )
          })}
        </div>
        <div className="px-6 py-4 border-t border-[var(--border)] flex gap-3">
          <Button onClick={() => onSubmit(form)} disabled={submitting} className="flex-1">
            {submitting ? '执行中...' : '执行'}
          </Button>
          <Button variant="outline" onClick={onClose} disabled={submitting} className="flex-1">取消</Button>
        </div>
      </div>
    </div>
  )
}

const SurvivalCell = memo(function SurvivalCell({
  acc,
  onChanged,
}: {
  acc: any
  onChanged: () => void
}) {
  const [taskId, setTaskId] = useState('')
  const [error, setError] = useState('')
  const [nowMs, setNowMs] = useState(() => Date.now())
  const info = getSurvivalInfo(acc, nowMs)

  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), 30_000)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    if (!taskId) return
    let active = true
    let timer: number | undefined
    const poll = async () => {
      try {
        const task = await apiFetch(`/tasks/${taskId}`)
        if (!active) return
        const status = String(task?.status || '')
        if (!['succeeded', 'failed', 'cancelled', 'interrupted'].includes(status)) {
          timer = window.setTimeout(poll, 1000)
          return
        }
        setTaskId('')
        if (status !== 'succeeded') {
          setError(String(task?.error || '检测失败'))
          return
        }
        setError('')
        onChanged()
      } catch (pollError: any) {
        if (!active) return
        setTaskId('')
        setError(pollError?.message || '检测失败')
      }
    }
    poll()
    return () => {
      active = false
      if (timer) window.clearTimeout(timer)
    }
  }, [taskId, onChanged])

  const check = async () => {
    setError('')
    try {
      const task = await apiFetch(`/accounts/${acc.id}/check`, { method: 'POST' })
      setTaskId(String(task?.task_id || task?.id || ''))
    } catch (checkError: any) {
      setError(checkError?.message || '检测失败')
    }
  }

  const tone = info.state === 'invalid'
    ? 'text-red-400'
    : info.state === 'valid'
      ? 'text-emerald-400'
      : 'text-amber-400'

  return (
    <div className="flex min-w-0 flex-col items-start gap-1" title={error || info.title}>
      <span className={`text-xs font-medium ${tone}`}>{info.label}</span>
      <span className="whitespace-nowrap text-[11px] text-[var(--text-muted)]">{info.duration}</span>
      <button
        type="button"
        onClick={check}
        disabled={Boolean(taskId)}
        className="inline-flex h-6 items-center gap-1 text-[11px] text-[var(--text-secondary)] hover:text-[var(--text-primary)] disabled:opacity-50"
      >
        <RefreshCw className={`h-3 w-3 ${taskId ? 'animate-spin' : ''}`} />
        {taskId ? '检测中' : error ? '重试' : '检测'}
      </button>
    </div>
  )
})

function AutoRefreshIndicator({
  busy,
  intervalSeconds,
  nextAt,
  lastAt,
}: {
  busy: boolean
  intervalSeconds: number
  nextAt: number
  lastAt: number | null
}) {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (intervalSeconds <= 0) return
    const timer = window.setInterval(() => setNow(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [intervalSeconds])

  const secondsUntilRefresh = intervalSeconds > 0
    ? Math.max(0, Math.ceil((nextAt - now) / 1000))
    : 0
  const progress = intervalSeconds > 0
    ? Math.min(100, Math.max(0, 100 - ((nextAt - now) / (intervalSeconds * 1000)) * 100))
    : 0
  const lastLabel = lastAt == null
    ? '等待首次同步'
    : (now - lastAt < 3_000
      ? '刚刚已同步'
      : `上次同步 ${Math.max(1, Math.floor((now - lastAt) / 1000))} 秒前`)
  const enabled = intervalSeconds > 0

  return (
    <div
      className="hidden min-w-[188px] items-center gap-2 rounded-lg border border-[var(--border)] bg-[var(--bg-pane)]/60 px-2 py-1 sm:flex"
      title="这里只同步列表中的服务端状态；单行“检测”按钮才会主动检查账号"
      role="status"
      aria-live="polite"
      aria-label={busy
        ? '正在同步账号状态'
        : enabled
          ? `自动刷新已开启，下次同步 ${secondsUntilRefresh} 秒`
          : '自动刷新已关闭'}
    >
      <span
        className={`h-2 w-2 shrink-0 rounded-full transition-colors ${busy
          ? 'bg-sky-400 motion-safe:animate-pulse'
          : enabled
            ? 'bg-emerald-400'
            : 'bg-slate-500'}`}
        aria-hidden="true"
      />
      <div className="min-w-0 flex-1 leading-none">
        <div className="flex items-center gap-1.5 text-[11px] font-medium text-[var(--text-secondary)]">
          <span>{busy ? '正在同步' : enabled ? '自动刷新' : '手动刷新'}</span>
          {enabled ? <span className="text-[var(--text-muted)]">· {intervalSeconds} 秒</span> : null}
        </div>
        <div className="mt-1 truncate text-[10px] text-[var(--text-muted)]">
          {busy
            ? '正在更新账号状态…'
            : enabled
              ? `${lastLabel} · 下次 ${secondsUntilRefresh} 秒`
              : '列表仅在操作完成或手动点击时更新'}
        </div>
        <div className="mt-1 h-0.5 overflow-hidden rounded-full bg-[var(--border)]" aria-hidden="true">
          <div
            className="h-full rounded-full bg-emerald-400 transition-[width] duration-300 ease-out"
            style={{ width: `${busy ? 100 : progress}%` }}
          />
        </div>
      </div>
    </div>
  )
}

function MailboxCodeCell({
  acc,
  onResult,
}: {
  acc: any
  onResult: (title: string, payload: any) => void
}) {
  const mailbox = getVerificationMailbox(acc)
  const [taskId, setTaskId] = useState<string | null>(null)
  const [lastCode, setLastCode] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (!taskId) return
    let active = true
    let timer: number | undefined

    const poll = async () => {
      try {
        const task = await apiFetch(`/tasks/${taskId}`)
        if (!active) return
        const status = String(task?.status || '')
        if (!['succeeded', 'failed', 'cancelled', 'interrupted'].includes(status)) {
          timer = window.setTimeout(poll, 1000)
          return
        }
        setTaskId(null)
        if (status !== 'succeeded') {
          setError(task?.error || '接码失败')
          return
        }
        const data = task?.data ?? task?.result?.data ?? {}
        const code = String(data?.code || '').trim()
        if (!code) {
          setError('任务完成但没有返回验证码')
          return
        }
        setLastCode(code)
        setError('')
        try {
          await navigator.clipboard.writeText(code)
        } catch {
          // Clipboard permission is optional; the code remains visible.
        }
        onResult(`${acc.email} · 邮箱接码`, data)
      } catch (pollError: any) {
        if (!active) return
        setTaskId(null)
        setError(pollError?.message || '读取接码任务失败')
      }
    }

    poll()
    return () => {
      active = false
      if (timer) window.clearTimeout(timer)
    }
  }, [taskId, acc.email, onResult])

  if (!mailbox?.email) {
    return <span className="text-xs text-[var(--text-muted)]/40">-</span>
  }

  const start = async () => {
    setError('')
    setLastCode('')
    try {
      const response = await apiFetch(
        `/actions/${acc.platform}/${acc.id}/receive_email_code`,
        { method: 'POST', body: JSON.stringify({ params: {} }) },
      )
      if (response?.sync) {
        if (!response.ok) throw new Error(response.error || '接码失败')
        const code = String(response.data?.code || '').trim()
        setLastCode(code)
        if (code) await navigator.clipboard.writeText(code).catch(() => undefined)
        onResult(`${acc.email} · 邮箱接码`, response.data)
        return
      }
      setTaskId(String(response?.task_id || ''))
    } catch (startError: any) {
      setError(startError?.message || '创建接码任务失败')
    }
  }

  const cancel = async () => {
    if (!taskId) return
    try {
      await apiFetch(`/tasks/${taskId}/cancel`, { method: 'POST' })
    } catch (cancelError: any) {
      setError(cancelError?.message || '停止失败')
    }
  }

  if (taskId) {
    return (
      <button
        type="button"
        onClick={cancel}
        className="inline-flex h-7 min-w-[72px] items-center justify-center gap-1 rounded-md border border-amber-400/30 bg-amber-400/10 px-2 text-xs text-amber-300"
        title="停止等待邮箱验证码"
      >
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        停止
      </button>
    )
  }

  if (lastCode) {
    return (
      <button
        type="button"
        onClick={() => navigator.clipboard.writeText(lastCode).catch(() => undefined)}
        className="inline-flex h-7 min-w-[86px] items-center justify-center gap-1.5 rounded-md border border-emerald-400/30 bg-emerald-400/10 px-2 font-mono text-xs font-semibold text-emerald-300"
        title="复制验证码"
      >
        {lastCode}
        <Copy className="h-3 w-3" />
      </button>
    )
  }

  return (
    <button
      type="button"
      onClick={start}
      className="table-action-btn min-w-[72px]"
      title={error || `从 ${mailbox.email} 读取最新验证码`}
    >
      {error ? '重试' : '接码'}
    </button>
  )
}

// ── 行操作菜单 ─────────────────────────────────────────────
function ActionMenu({
  acc,
  onDetail,
  onDelete,
  onResult,
  onChanged,
}: {
  acc: any
  onDetail: () => void
  onDelete: () => void
  onResult: (title: string, payload: any) => void
  onChanged: () => void
}) {
  const { language } = useI18n()
  const [open, setOpen] = useState(false)
  const [actions, setActions] = useState<any[]>([])
  const [actionsLoaded, setActionsLoaded] = useState(false)
  const [running, setRunning] = useState<string | null>(null)
  const [toast, setToast] = useState<{ type: 'success' | 'error'; text: string } | null>(null)
  const [actionTask, setActionTask] = useState<{ taskId: string; title: string } | null>(null)
  const [actionTaskStatus, setActionTaskStatus] = useState<string | null>(null)
  const [pendingAction, setPendingAction] = useState<{ action: any; params: Record<string, string> } | null>(null)
  const [menuPosition, setMenuPosition] = useState({ top: 0, left: 0, maxHeight: 320 })
  const menuRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const visibleActions = actions.filter(action => (
    action?.id !== 'bind_email' || String(acc?.email || '').startsWith('phone:')
  ))

  const runAction = (action: any, params: Record<string, any>) => {
    setRunning(action.id)
    setActionTaskStatus(null)
    apiFetch(`/actions/${acc.platform}/${acc.id}/${action.id}`, { method: 'POST', body: JSON.stringify({ params }) })
      .then(resp => {
        if (resp?.sync) {
          setRunning(null)
          if (!resp.ok) {
            setToast({ type: 'error', text: resp.error || 'Operation failed' })
            return
          }
          onChanged()
          if (resp.data?.url || resp.data?.checkout_url || resp.data?.cashier_url) {
            const actionUrl = resp.data?.url || resp.data?.checkout_url || resp.data?.cashier_url
            window.open(actionUrl, '_blank')
            try {
              navigator.clipboard.writeText(actionUrl)
            } catch {
              // Ignore clipboard errors
            }
          }
          onResult(action.label, resp.data)
          return
        }
        setActionTask({
          taskId: resp.task_id,
          title: `${acc.email} · ${action.label}`,
        })
      })
      .catch(() => {
        setRunning(null)
        setToast({ type: 'error', text: 'Request failed' })
      })
  }

  const updateMenuPosition = useCallback(() => {
    const trigger = triggerRef.current
    if (!trigger) return

    const rect = trigger.getBoundingClientRect()
    const viewportPadding = 12
    const menuWidth = 220
    const estimatedHeight = Math.min(320, Math.max(menuRef.current?.offsetHeight || 200, 160))

    let left = rect.right - menuWidth
    if (left < viewportPadding) left = viewportPadding
    if (left + menuWidth > window.innerWidth - viewportPadding) {
      left = Math.max(viewportPadding, window.innerWidth - menuWidth - viewportPadding)
    }

    let top = rect.bottom + 8
    if (top + estimatedHeight > window.innerHeight - viewportPadding) {
      top = Math.max(viewportPadding, rect.top - estimatedHeight - 8)
    }

    setMenuPosition({
      top: Math.round(top),
      left: Math.round(left),
      maxHeight: Math.max(160, window.innerHeight - viewportPadding * 2),
    })
  }, [])

  useEffect(() => {
    if (toast) { const t = setTimeout(() => setToast(null), 4000); return () => clearTimeout(t) }
  }, [toast])
  useEffect(() => {
    if (!open) return
    let active = true
    setActionsLoaded(false)
    loadPlatformActions(acc.platform)
      .then((items) => {
        if (active) {
          setActions(items)
          setActionsLoaded(true)
          window.requestAnimationFrame(updateMenuPosition)
        }
      })
      .catch(() => {
        if (active) {
          setActions([])
          setActionsLoaded(true)
        }
      })
    updateMenuPosition()
    const handler = (e: MouseEvent) => {
      const target = e.target as Node
      if (menuRef.current?.contains(target) || triggerRef.current?.contains(target)) return
      setOpen(false)
    }
    const reposition = () => updateMenuPosition()
    document.addEventListener('mousedown', handler)
    window.addEventListener('resize', reposition)
    window.addEventListener('scroll', reposition, true)
    return () => {
      active = false
      document.removeEventListener('mousedown', handler)
      window.removeEventListener('resize', reposition)
      window.removeEventListener('scroll', reposition, true)
    }
  }, [open, acc.platform, updateMenuPosition])

  const handleActionDone = async (status: string) => {
    if (!actionTask) return
    setActionTaskStatus(status)
    setRunning(null)
    try {
      const task = await apiFetch(`/tasks/${actionTask.taskId}`)
      const data = task?.data ?? task?.result?.data
      if (status !== 'succeeded') {
        setToast({ type: 'error', text: task?.error || getTaskStatusText(status, language) })
        return
      }
      onChanged()
      const actionUrl = data?.url || data?.checkout_url || data?.cashier_url
      if (actionUrl) {
        window.open(actionUrl, '_blank')
        try {
          await navigator.clipboard.writeText(actionUrl)
        } catch {
          // ignore clipboard failures
        }
      }
      if (data && typeof data === 'object') {
        if (actionUrl) {
          setToast({ type: 'success', text: data.message || '支付链接已在新标签打开，链接已复制' })
          return
        }
        const detailKeys = Object.keys(data).filter(key => !['message', 'url', 'checkout_url', 'cashier_url'].includes(key))
        if (detailKeys.length > 0) {
          onResult(actionTask.title, data)
        }
        setToast({ type: 'success', text: data.message || '操作成功' })
        return
      }
      setToast({ type: 'success', text: typeof data === 'string' && data ? data : '操作成功' })
    } catch (error: any) {
      setToast({ type: 'error', text: error?.message || '读取任务结果失败' })
    }
  }

  return (
    <div className="relative flex min-w-[136px] items-center justify-end gap-1.5 whitespace-nowrap">
      {toast && (
        <div
          className="fixed top-5 right-5 z-[9999] flex items-center gap-2.5 rounded-xl border px-4 py-3 text-[13px] font-medium shadow-lg  cursor-pointer transition-all"
          style={{
            background: toast.type === 'success' ? 'rgba(16,185,129,0.12)' : 'rgba(239,68,68,0.12)',
            borderColor: toast.type === 'success' ? 'rgba(16,185,129,0.25)' : 'rgba(239,68,68,0.25)',
            color: toast.type === 'success' ? '#6ee7b7' : '#fca5a5',
          }}
          onClick={() => setToast(null)}
        >
          <span className="text-base">{toast.type === 'success' ? '✓' : '✗'}</span>
          <span>{toast.text}</span>
        </div>
      )}
      {actionTask && (
        <ActionTaskModal
          title={actionTask.title}
          taskId={actionTask.taskId}
          taskStatus={actionTaskStatus}
          onClose={() => {
            setActionTask(null)
            setActionTaskStatus(null)
          }}
          onDone={handleActionDone}
        />
      )}
      {pendingAction && (
        <ActionParamsModal
          action={pendingAction.action}
          initialValues={pendingAction.params}
          submitting={running === pendingAction.action?.id}
          onClose={() => {
            if (!running) setPendingAction(null)
          }}
          onSubmit={(params) => {
            const action = pendingAction.action
            setPendingAction(null)
            runAction(action, params)
          }}
        />
      )}
      <button onClick={onDetail} className="table-action-btn">详情</button>
      <div className="relative">
        <button ref={triggerRef} onClick={() => setOpen(o => !o)}
          className="table-action-btn">更多 ▾</button>
        {open && typeof document !== 'undefined' && createPortal(
            <div
              ref={menuRef}
              className="fixed z-[9999] w-[220px] overflow-y-auto rounded-2xl border border-[var(--border)] bg-[var(--bg-card)]/96 py-1.5 shadow-[var(--shadow-soft)] "
              style={{ top: menuPosition.top, left: menuPosition.left, maxHeight: menuPosition.maxHeight }}
            >
              {visibleActions.length === 0 && (
                <div className="px-3 py-2 text-xs text-[var(--text-muted)]">
                  {actionsLoaded ? '暂无额外操作' : '正在加载操作…'}
                </div>
              )}
              {visibleActions.map(a => (
                <button key={a.id}
                  onClick={() => {
                    setOpen(false)
                    if (Array.isArray(a.params) && a.params.length > 0) {
                      setPendingAction({
                        action: a,
                        params: buildActionParamDraft(a, acc),
                      })
                      return
                    }
                    runAction(a, {})
                  }}
                  disabled={!!running}
                  className="w-full px-3 py-2 text-left text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)] disabled:opacity-50">
                  {running === a.id ? '执行中...' : a.label}
                </button>
              ))}
              <div className="my-1 border-t border-[var(--border)]/70" />
              <button
                onClick={() => {
                  setOpen(false)
                  if (confirm(`确认删除 ${acc.email}？`)) {
                    apiFetch(`/accounts/${acc.id}`, { method: 'DELETE' }).then(onDelete)
                  }
                }}
                className="w-full px-3 py-2 text-left text-xs text-[#f0b0b0] transition-colors hover:bg-[rgba(239,68,68,0.08)] hover:text-[#ffd5d5]"
              >
                删除
              </button>
            </div>,
            document.body,
        )}
      </div>
    </div>
  )
}

// ── 账号详情弹框 ───────────────────────────────────────────
function DetailModal({ acc, onClose, onSave }: { acc: any; onClose: () => void; onSave: () => void }) {
  const [form, setForm] = useState({
    lifecycle_status: getLifecycleStatus(acc),
    primary_token: getPrimaryToken(acc),
    cashier_url: getCashierUrl(acc),
  })
  const [saving, setSaving] = useState(false)
  const overview = getAccountOverview(acc)
  const verificationMailbox = getVerificationMailbox(acc)
  const providerAccounts = getProviderAccounts(acc)
  const credentials = getCredentials(acc)
  const primaryMetrics = getPrimaryMetrics(acc)
  const secondaryMetrics = getSecondaryMetrics(acc)
  const warnings = getDisplayWarnings(acc)
  const displayBadges = getDisplayBadges(acc)
  const displaySections = getDisplaySections(acc)
  const copyText = (text: string) => navigator.clipboard.writeText(text)
  const platformCredentials = credentials.filter((item: any) => item.scope === 'platform')

  const save = async () => {
    setSaving(true)
    try {
      await apiFetch(`/accounts/${acc.id}`, { method: 'PATCH', body: JSON.stringify(form) })
      onSave()
    } finally { setSaving(false) }
  }

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-sm flex flex-col" style={{maxHeight:'90vh'}} onClick={e => e.stopPropagation()}>
        {/* ── Sticky Header ── */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)] shrink-0">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">账号详情</h2>
            <p className="text-xs text-[var(--text-muted)] mt-0.5">{acc.email}</p>
          </div>
          <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"><X className="h-4 w-4" /></button>
        </div>
        {/* ── Scrollable Content ── */}
        <div className="px-6 py-4 space-y-3 flex-1 overflow-y-auto min-h-0">
          <div className="relative overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--accent-soft)] p-4 shadow-[var(--shadow-soft)]">
            <div className="pointer-events-none absolute -right-16 -top-20 h-44 w-44 rounded-full bg-[var(--accent-soft)] blur-3xl" />
            <div className="relative flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="text-xs uppercase tracking-[0.18em] text-[var(--text-muted)]">核心状态</div>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <Badge variant={STATUS_VARIANT[getDisplayStatus(acc)] || 'secondary'}>{getDisplayStatus(acc)}</Badge>
                  <span className="text-lg font-semibold tracking-[-0.03em] text-[var(--text-primary)]">{acc.plan_name || overview.plan_name || overview.plan || getPlanState(acc)}</span>
                </div>
              </div>
              <div className="grid grid-cols-2 gap-2 text-right text-[11px] text-[var(--text-muted)] sm:grid-cols-3">
                <div className="rounded-xl border border-[var(--border-soft)] bg-black/10 px-2.5 py-2">
                  <div className="uppercase tracking-[0.12em]">生命周期</div>
                  <div className="mt-1 text-[var(--text-primary)]">{getLifecycleStatus(acc)}</div>
                </div>
                <div className="rounded-xl border border-[var(--border-soft)] bg-black/10 px-2.5 py-2">
                  <div className="uppercase tracking-[0.12em]">有效性</div>
                  <div className="mt-1 text-[var(--text-primary)]">{getValidityStatus(acc)}</div>
                </div>
                <div className="rounded-xl border border-[var(--border-soft)] bg-black/10 px-2.5 py-2">
                  <div className="uppercase tracking-[0.12em]">套餐状态</div>
                  <div className="mt-1 text-[var(--text-primary)]">{getPlanState(acc)}</div>
                </div>
              </div>
            </div>
            {secondaryMetrics.length > 0 && (
              <div className="relative mt-4 grid gap-2 sm:grid-cols-2">
                {secondaryMetrics.slice(0, 4).map((metric: any) => (
                  <DisplayMetricCard key={metric.key || metric.label} metric={metric} compact />
                ))}
              </div>
            )}
          </div>

          {primaryMetrics.length > 0 && (
            <div className="grid gap-3 sm:grid-cols-2">
              {primaryMetrics.map((metric: any) => (
                <DisplayMetricCard key={metric.key || metric.label} metric={metric} />
              ))}
            </div>
          )}

          <DisplayWarnings warnings={warnings} />
          <DisplaySections sections={displaySections} />

          {(displayBadges.length > 0 || verificationMailbox?.email) && (
            <div className="space-y-2">
              {displayBadges.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {displayBadges.map((badge: any, index: number) => (
                    <span key={`${badge?.label || 'badge'}-${index}`} className="rounded-full border border-[var(--border)] bg-[var(--bg-hover)] px-2 py-0.5 text-[11px] text-[var(--text-secondary)]">
                      {badge?.label}
                    </span>
                  ))}
                </div>
              )}
              {verificationMailbox?.email && (
                <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2 text-xs text-[var(--text-secondary)]">
                  验证码邮箱: {verificationMailbox.email} · {verificationMailbox.provider || '-'} · ID {verificationMailbox.account_id || '-'}
                </div>
              )}
            </div>
          )}
          {(() => {
            const wsStatuses = (overview && overview.workspace_statuses) || (acc.workspace_statuses) || {}
            const ids = Object.keys(wsStatuses)
            if (ids.length === 0) return null
            const statusLabel: Record<string,string> = { export_ok:'已导出', export_failed:'导出失败', export_skipped:'跳过导出', session_stale:'Session 非 workspace', accept_ok:'已接受', accept_failed:'接受失败', accept_skipped:'跳过接受', request_ok:'已请求', request_failed:'请求失败', pending:'未处理' }
            const statusColor: Record<string,string> = { export_ok:'text-emerald-500', export_failed:'text-amber-500', export_skipped:'text-gray-400', session_stale:'text-orange-500', accept_ok:'text-sky-500', accept_failed:'text-orange-500', accept_skipped:'text-gray-400', request_ok:'text-blue-500', request_failed:'text-red-500', pending:'text-gray-400' }
            return (
              <div className="space-y-2">
                <label className="text-xs text-[var(--text-muted)] block">Workspace 状态 ({ids.length})</label>
                <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] overflow-hidden">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="border-b border-[var(--border)] text-[var(--text-muted)]">
                        <th className="px-3 py-1.5 text-left font-medium">Workspace ID</th>
                        <th className="px-3 py-1.5 text-left font-medium">状态</th>
                        <th className="px-3 py-1.5 text-left font-medium">凭证</th>
                        <th className="px-3 py-1.5 text-left font-medium">错误</th>
                        <th className="px-3 py-1.5 text-center font-medium">导出</th>
                      </tr>
                    </thead>
                    <tbody>
                      {ids.map(id => {
                        const s = wsStatuses[id] || {}
                        const status = (s.status || 'pending') as string
                        const label = statusLabel[status] || status
                        const cls = statusColor[status] || 'text-gray-400'
                        const creds = (s.credentials) || {}
                        const credKeys = Object.keys(creds)
                        const hasCreds = credKeys.length > 0
                        const exportJson = () => {
                          const entry = { workspace_id: id, email: acc.email, ...creds, exported_at: s.updated_at || '' }
                          const blob = new Blob([JSON.stringify(entry, null, 2)], {type:'application/json'})
                          const url = URL.createObjectURL(blob)
                          const a = document.createElement('a')
                          a.href = url; a.download = (acc.email || 'ws').replace(/[@.]/g,'_') + '_' + id.slice(0,8) + '.json'
                          a.click(); URL.revokeObjectURL(url)
                        }
                        return (
                          <tr key={id} className="border-b border-[var(--border)]/30 last:border-b-0">
                            <td className="px-3 py-1.5 font-mono text-[var(--text-secondary)] truncate max-w-[140px]" title={id}>{id.length > 16 ? id.slice(0, 16) + '…' : id}</td>
                            <td className={`px-3 py-1.5 font-medium ${cls}`}>{label}</td>
                            <td className="px-3 py-1.5 text-[var(--text-muted)] font-mono">{hasCreds ? credKeys.join(', ') : '无'}</td>
                            <td className="px-3 py-1.5 text-[var(--text-muted)]/70 truncate max-w-[120px]" title={s.error || ''}>{s.error || '-'}</td>
                            <td className="px-3 py-1.5 text-center">
                              {hasCreds ? (
                                <button onClick={(e) => { e.stopPropagation(); exportJson() }} className="text-[10px] px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-500 hover:bg-emerald-500/20 transition-colors" title="导出此 workspace 的 CPA JSON">导出</button>
                              ) : (
                                <span className="text-[var(--text-muted)]/40 text-[10px]">-</span>
                              )}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
                {/* 批量导出所有 workspace 凭证 */}
                {(() => {
                  const exportable = ids
                    .map(id => ({id, s: wsStatuses[id] || {}}))
                    .filter(({s}) => Object.keys(s.credentials || {}).length > 0)
                  if (exportable.length < 2) return null
                  const exportAll = () => {
                    const all = exportable.map(({id, s}) => ({workspace_id:id, email:acc.email, ...(s.credentials||{}), exported_at:s.updated_at||''}))
                    const blob = new Blob([JSON.stringify(all, null, 2)], {type:'application/json'})
                    const url = URL.createObjectURL(blob)
                    const a = document.createElement('a')
                    a.href=url; a.download=(acc.email||'workspaces').replace(/[@.]/g,'_')+'_all_workspaces.json'
                    a.click(); URL.revokeObjectURL(url)
                  }
                  return (
                    <div className="mt-1 flex justify-end">
                      <button onClick={exportAll} className="text-[10px] px-2 py-0.5 rounded bg-sky-500/10 text-sky-500 hover:bg-sky-500/20 transition-colors">
                        批量导出 ({exportable.length} 个)
                      </button>
                    </div>
                  )
                })()}
              </div>
            )
          })()}
          {providerAccounts.length > 0 && (
            <div className="space-y-2">
              <label className="text-xs text-[var(--text-muted)] block">Provider Accounts</label>
              {providerAccounts.map((item: any, index: number) => (
                <div key={`${item.provider_name || 'provider'}-${item.login_identifier || index}`} className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] p-3">
                  <div className="text-xs font-semibold text-[var(--text-primary)]">
                    {item.provider_name || item.provider_type || 'provider'}
                  </div>
                  <div className="mt-1 text-xs text-[var(--text-secondary)] break-all">
                    登录标识: {item.login_identifier || '-'}
                  </div>
                  {item.credentials && Object.keys(item.credentials).length > 0 && (
                    <div className="mt-2 grid gap-2">
                      {Object.entries(item.credentials).map(([key, value]: [string, any]) => (
                        <div key={key}>
                          <div className="text-[11px] text-[var(--text-muted)]">{key}</div>
                          <div className="flex items-start gap-1">
                            <div className="flex-1 rounded-md border border-[var(--border)] bg-black/20 px-2 py-1.5 text-xs font-mono text-[var(--text-secondary)] break-all max-h-40 overflow-y-auto">
                              {String(value || '-')}
                            </div>
                            {value ? (
                              <button onClick={() => copyText(String(value))} className="mt-1 shrink-0 text-[var(--text-muted)] hover:text-[var(--text-secondary)]">
                                <Copy className="h-3 w-3" />
                              </button>
                            ) : null}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
          {platformCredentials.length > 0 && (
            <div className="space-y-2">
              <label className="text-xs text-[var(--text-muted)] block">Platform Credentials</label>
              {platformCredentials.map((item: any) => (
                <div key={`${item.scope}-${item.key}`} className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] p-3">
                  <div className="text-[11px] text-[var(--text-muted)]">{item.key}</div>
                  <div className="mt-1 flex items-start gap-1">
                    <div className="flex-1 rounded-md border border-[var(--border)] bg-black/20 px-2 py-1.5 text-xs font-mono text-[var(--text-secondary)] break-all max-h-40 overflow-y-auto">
                      {item.value}
                    </div>
                    <button onClick={() => copyText(String(item.value || ''))} className="mt-1 shrink-0 text-[var(--text-muted)] hover:text-[var(--text-secondary)]">
                      <Copy className="h-3 w-3" />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
          <div>
            <label className="text-xs text-[var(--text-muted)] block mb-1">生命周期状态</label>
            <select value={form.lifecycle_status} onChange={e => setForm(f => ({ ...f, lifecycle_status: e.target.value }))}
              className="control-surface appearance-none">
              {['registered','trial','subscribed','expired','invalid'].map(s => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          <div>
            <label className="text-xs text-[var(--text-muted)] block mb-1">主凭证</label>
            <textarea value={form.primary_token} onChange={e => setForm(f => ({ ...f, primary_token: e.target.value }))}
              rows={2} className="control-surface control-surface-mono resize-none" />
          </div>
          <div>
            <label className="text-xs text-[var(--text-muted)] block mb-1">试用链接</label>
            <textarea value={form.cashier_url} onChange={e => setForm(f => ({ ...f, cashier_url: e.target.value }))}
              rows={2} className="control-surface control-surface-mono resize-none" />
          </div>
        </div>
        {/* ── Sticky Footer ── */}
        <div className="flex gap-3 px-6 py-4 border-t border-[var(--border)] shrink-0">
          <Button onClick={save} disabled={saving} className="flex-1">{saving ? '保存中...' : '保存'}</Button>
          <Button variant="outline" onClick={onClose} className="flex-1">取消</Button>
        </div>
      </div>
    </div>
  )
}

// ── 导入弹框 ────────────────────────────────────────────────
function ImportModal({ platform, onClose, onDone }: { platform: string; onClose: () => void; onDone: () => void }) {
  const [text, setText] = useState('')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<string | null>(null)
  const submit = async () => {
    setLoading(true)
    try {
      const lines = text.trim().split('\n').filter(Boolean)
      const res = await apiFetch('/accounts/import', { method: 'POST', body: JSON.stringify({ platform, lines }) })
      setResult(`导入成功 ${res.created} 个`); onDone()
    } catch (e: any) { setResult(`失败: ${e.message}`) } finally { setLoading(false) }
  }
  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-sm p-6" onClick={e => e.stopPropagation()}>
        <h2 className="text-base font-semibold text-[var(--text-primary)] mb-2">批量导入</h2>
        <p className="text-xs text-[var(--text-muted)] mb-3">每行格式: <code className="bg-[var(--bg-hover)] px-1 rounded">email password [cashier_url]</code></p>
        <textarea value={text} onChange={e => setText(e.target.value)} rows={8}
          className="control-surface control-surface-mono resize-none mb-3" />
        {result && <p className="text-sm text-emerald-400 mb-3">{result}</p>}
        <div className="flex gap-2">
          <Button onClick={submit} disabled={loading} className="flex-1">{loading ? '导入中...' : '导入'}</Button>
          <Button variant="outline" onClick={onClose} className="flex-1">取消</Button>
        </div>
      </div>
    </div>
  )
}

function ExportMenu({
  platform,
  total,
  statusFilter,
  searchFilter,
  selectedIds,
  onExported,
}: {
  platform: string
  total: number
  statusFilter: string
  searchFilter: string
  selectedIds: number[]
  onExported?: () => void
}) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState<string | null>(null)
  const [exportLimit, setExportLimit] = useState('')
  const [deleteAfterExport, setDeleteAfterExport] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const menuWidth = 260
  const [menuPosition, setMenuPosition] = useState({ top: 0, left: 0 })
  const hasSelection = selectedIds.length > 0

  useEffect(() => {
    if (!open) return

    const updatePosition = () => {
      const rect = buttonRef.current?.getBoundingClientRect()
      if (!rect) return
      const viewportPadding = 12
      const left = Math.min(
        Math.max(viewportPadding, rect.right - menuWidth),
        Math.max(viewportPadding, window.innerWidth - menuWidth - viewportPadding),
      )
      setMenuPosition({ top: rect.bottom + 8, left })
    }

    const handler = (e: MouseEvent) => {
      const target = e.target as Node
      const clickedMenu = menuRef.current?.contains(target)
      const clickedButton = buttonRef.current?.contains(target)
      if (!clickedMenu && !clickedButton) setOpen(false)
    }

    updatePosition()
    document.addEventListener('mousedown', handler)
    window.addEventListener('resize', updatePosition)
    window.addEventListener('scroll', updatePosition, true)
    return () => {
      document.removeEventListener('mousedown', handler)
      window.removeEventListener('resize', updatePosition)
      window.removeEventListener('scroll', updatePosition, true)
    }
  }, [open, menuWidth])

  const doExport = async (format: string) => {
    const limit = Math.max(0, Number.parseInt(exportLimit, 10) || 0)
    if (deleteAfterExport) {
      const scopeText = limit > 0 ? `${limit} 个导出单位` : (hasSelection ? `${selectedIds.length} 个已选账号` : '当前筛选结果')
      if (!window.confirm(`确认导出 ${scopeText} 后从本地删除？\n一个账号有多个 Workspace/JSON 时，只会删除本次实际导出的部分。`)) return
    }
    setLoading(format)
    try {
      const { blob, filename } = await apiDownload(`/accounts/export/${format}`, {
        method: 'POST',
        body: JSON.stringify({
          platform,
          ids: hasSelection ? selectedIds : [],
          select_all: !hasSelection,
          status_filter: !hasSelection ? statusFilter || null : null,
          search_filter: !hasSelection ? searchFilter || null : null,
          limit,
          delete_after_export: deleteAfterExport,
        }),
      })
      triggerBrowserDownload(blob, filename)
      setOpen(false)
      if (deleteAfterExport) onExported?.()
    } catch (e: any) {
      window.alert(e?.message || '导出失败')
    } finally {
      setLoading(null)
    }
  }

  const options = [
    ...(platform === 'chatgpt' ? [{ key: 'sub2api-agent-identity', label: '导出 Agent Identity (Sub2Api)' }] : []),
    { key: 'json', label: '导出 JSON' },
    { key: 'csv', label: '导出 CSV' },
    { key: 'any2api', label: '导出 Any2Api' },
    { key: 'sub2api', label: '导出 Sub2Api' },
    { key: 'cpa', label: '导出 CPA' },
    { key: 'cockpit', label: '导出 Cockpit' },
    { key: 'compact-auto', label: 'Compact Auto' },
    ...(platform === 'kiro' ? [{ key: 'kiro-go', label: '导出 Kiro-Go' }] : []),
  ]

  return (
    <div className="relative">
      <Button
        ref={buttonRef}
        variant="outline"
        size="sm"
        onClick={() => setOpen(v => !v)}
        disabled={total === 0 || !!loading}
        className={ACCOUNT_TOOL_BUTTON_CLASS}
      >
        <Download className="h-4 w-4 mr-1 shrink-0" />
        {loading ? '导出中...' : hasSelection ? `导出已选(${selectedIds.length})` : '导出'}
      </Button>
      {open && typeof document !== 'undefined' && createPortal(
        <div
          ref={menuRef}
          className="fixed z-[9999] w-[260px] overflow-y-auto rounded-xl border border-[var(--border)] bg-[var(--bg-card)] py-2 shadow-2xl ring-1 ring-black/5 backdrop-blur"
          style={{ top: menuPosition.top, left: menuPosition.left, maxHeight: `calc(100vh - ${menuPosition.top + 12}px)` }}
        >
          <div className="px-3 pb-2 text-[11px] text-[var(--text-muted)]">
            {hasSelection ? `导出 ${selectedIds.length} 个已选账号` : '导出当前筛选结果'}
            <div className="mt-2 grid grid-cols-[1fr_76px] items-center gap-2">
              <span>导出数量</span>
              <input
                value={exportLimit}
                onChange={e => setExportLimit(e.target.value.replace(/[^\d]/g, ''))}
                placeholder="全部"
                className="h-7 rounded-md border border-[var(--border)] bg-[var(--bg-base)] px-2 text-right text-xs text-[var(--text-primary)] outline-none focus:border-[var(--accent)]"
              />
            </div>
            <label className="mt-2 flex cursor-pointer items-center gap-2 rounded-md px-1 py-1 text-[11px] text-red-500 hover:bg-red-500/10">
              <input
                type="checkbox"
                checked={deleteAfterExport}
                onChange={e => setDeleteAfterExport(e.target.checked)}
                className="h-3.5 w-3.5"
              />
              <span>导出后从本地删除</span>
            </label>
            <div className="mt-1 text-[10px] leading-4 text-[var(--text-muted)]/80">
              一个账号如果有多个 Workspace/JSON，会按实际导出单位拆开计算。
            </div>
          </div>
          {options.map(option => (
            <button
              key={option.key}
              onClick={() => doExport(option.key)}
              className="w-full px-3 py-1.5 text-left text-xs text-[var(--text-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
            >
              {option.label}
            </button>
          ))}
        </div>,
        document.body,
      )}
    </div>
  )
}

// ── Main ────────────────────────────────────────────────────
export default function Accounts() {
  const { t, language } = useI18n()
  const { platform } = useParams<{ platform: string }>()
  const navigate = useNavigate()
  const [tab, setTab] = useState(platform || '')
  useEffect(() => {
    if (platform) {
      setTab(platform)
      setPage(1)
    }
  }, [platform])

  const [accounts, setAccounts] = useState<any[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(30)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [filterStatus, setFilterStatus] = useState('')
  const [detail, setDetail] = useState<any | null>(null)
  const [showImport, setShowImport] = useState(false)
  const [showAdd, setShowAdd] = useState(false)
  const [platformsMap, setPlatformsMap] = useState<Record<string, any>>({})
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [actionResult, setActionResult] = useState<{ title: string; payload: any } | null>(null)
  const [bulkDeleting, setBulkDeleting] = useState(false)
  const [batchRefreshing, setBatchRefreshing] = useState(false)
  const [batchTask, setBatchTask] = useState<{ taskId: string; title: string } | null>(null)
  const [batchTaskStatus, setBatchTaskStatus] = useState<string | null>(null)
  const [browserMode, setBrowserMode] = useState('camoufox_headless')
  const [actionConcurrency, setActionConcurrency] = useState(1)
  const [oauthTaskId, setOauthTaskId] = useState('')
  const [oauthBusy, setOauthBusy] = useState(false)
  const [oauthConfirmOpen, setOauthConfirmOpen] = useState(false)
  const [getRtTaskId, setGetRtTaskId] = useState('')
  const [getRtBusy, setGetRtBusy] = useState(false)
  const [getRtConfirmOpen, setGetRtConfirmOpen] = useState(false)
  const [getRtBypassTaskId, setGetRtBypassTaskId] = useState('')
  const [getRtBypassBusy, setGetRtBypassBusy] = useState(false)
  const [getRtBypassConfirmOpen, setGetRtBypassConfirmOpen] = useState(false)
  const [getRtSmsProvider, setGetRtSmsProvider] = useState('')
  const [getRtSmspoolKey, setGetRtSmspoolKey] = useState('')
  const [getRtSmspoolMaxPrice, setGetRtSmspoolMaxPrice] = useState('0.13')
  const [getRtSmsapiPhone, setGetRtSmsapiPhone] = useState('')
  const [getRtSmsapiUrl, setGetRtSmsapiUrl] = useState('')
  const [getRtRecordHar, setGetRtRecordHar] = useState(false)
  const [getRtPhoneReuseCount, setGetRtPhoneReuseCount] = useState(3)
  const [autoRefreshBusy, setAutoRefreshBusy] = useState(false)
  const [nextAutoRefreshAt, setNextAutoRefreshAt] = useState(() => Date.now() + 30_000)
  const [lastAutoRefreshAt, setLastAutoRefreshAt] = useState<number | null>(null)
  const [survivalRefreshSeconds, setSurvivalRefreshSeconds] = useState(() => {
    if (typeof window === 'undefined') return 30
    try {
      const value = Number(window.localStorage.getItem('accounts_refresh_interval_seconds'))
      return [0, 15, 30, 60, 120].includes(value) ? value : 30
    } catch {
      return 30
    }
  })
  const accountLoadAbortRef = useRef<AbortController | null>(null)
  const autoRefreshInFlightRef = useRef(false)

  useEffect(() => {
    getPlatforms().then((list: any[]) => {
      const map: Record<string, any> = {}
      list.forEach(p => { map[p.name] = p })
      setPlatformsMap(map)
      if (!platform && !tab && list[0]?.name) {
        setTab(list[0].name)
      }
    }).catch(() => {})
  }, [platform, tab])

  useEffect(() => {
    const timer = setTimeout(() => {
      setPage(1)
      setDebouncedSearch(search)
    }, 400)
    return () => clearTimeout(timer)
  }, [search])

  useEffect(() => {
    setSelectedIds(new Set())
  }, [tab, filterStatus, debouncedSearch])

  const load = useCallback(async () => {
    accountLoadAbortRef.current?.abort()
    const controller = new AbortController()
    accountLoadAbortRef.current = controller
    setLoading(true)
    try {
      const params = new URLSearchParams({ platform: tab, page: String(page), page_size: String(pageSize) })
      if (debouncedSearch) params.set('email', debouncedSearch)
      if (filterStatus) params.set('status', filterStatus)
      const data = await apiFetch(`/accounts?${params}`, { signal: controller.signal })
      setAccounts(data.items); setTotal(data.total)
    } catch (error) {
      if ((error as Error)?.name !== 'AbortError') setError(String(error || '加载账号失败'))
    } finally {
      if (accountLoadAbortRef.current === controller) {
        accountLoadAbortRef.current = null
        setLoading(false)
      }
    }
  }, [tab, page, pageSize, debouncedSearch, filterStatus])

  useEffect(() => { void load() }, [load])
  useEffect(() => () => accountLoadAbortRef.current?.abort(), [])
  useEffect(() => {
    try {
      window.localStorage.setItem('accounts_refresh_interval_seconds', String(survivalRefreshSeconds))
    } catch {
      // Ignore browsers that block localStorage.
    }
  }, [survivalRefreshSeconds])
  useEffect(() => {
    setNextAutoRefreshAt(
      survivalRefreshSeconds > 0
        ? Date.now() + survivalRefreshSeconds * 1000
        : 0,
    )
  }, [survivalRefreshSeconds])
  useEffect(() => {
    if (survivalRefreshSeconds <= 0) return
    const timer = window.setInterval(() => {
      if (
        autoRefreshInFlightRef.current
        || document.visibilityState !== 'visible'
        || detail
        || showImport
        || showAdd
        || batchTask
      ) return
      const startedAt = Date.now()
      setNextAutoRefreshAt(startedAt + survivalRefreshSeconds * 1000)
      autoRefreshInFlightRef.current = true
      setAutoRefreshBusy(true)
      void load().finally(() => {
        autoRefreshInFlightRef.current = false
        setAutoRefreshBusy(false)
        setLastAutoRefreshAt(Date.now())
      })
    }, survivalRefreshSeconds * 1000)
    return () => window.clearInterval(timer)
  }, [batchTask, detail, load, showAdd, showImport, survivalRefreshSeconds])
  const totalPages = Math.max(Math.ceil(total / pageSize), 1)

  useEffect(() => {
    setSelectedIds(prev => {
      const visible = new Set(accounts.map(acc => acc.id))
      return new Set([...prev].filter(id => visible.has(id)))
    })
  }, [accounts])

  
  const exportCsv = () => {
    const header = 'email,password,display_status,lifecycle_status,plan_state,validity_status,cashier_url,created_at'
    const rowsSource = selectedIds.size > 0 ? accounts.filter(a => selectedIds.has(a.id)) : accounts
    const rows = rowsSource.map(a => [
      a.email,
      a.password,
      getDisplayStatus(a),
      getLifecycleStatus(a),
      getPlanState(a),
      getValidityStatus(a),
      getCashierUrl(a),
      a.created_at,
    ].map(escapeCsvField).join(','))
    const blob = new Blob([[header, ...rows].join('\n')], { type: 'text/csv' })
    triggerBrowserDownload(blob, `${tab}_accounts.csv`)
  }

  const pageIds = accounts.map(acc => acc.id)
  const allSelectedOnPage = pageIds.length > 0 && pageIds.every(id => selectedIds.has(id))
  const selectedCount = selectedIds.size

  const toggleOne = (id: number) => {
    setSelectedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const togglePage = () => {
    setSelectedIds(prev => {
      const next = new Set(prev)
      if (allSelectedOnPage) pageIds.forEach(id => next.delete(id))
      else pageIds.forEach(id => next.add(id))
      return next
    })
  }

  const copy = (text: string) => {
    if (navigator.clipboard) { navigator.clipboard.writeText(text) }
    else { const el = document.createElement('textarea'); el.value = text; document.body.appendChild(el); el.select(); document.execCommand('copy'); document.body.removeChild(el) }
  }
  const emailApiLine = (email: string) =>
    email

  const startCodexOAuth = async () => {
    setError('')
    const ids = [...selectedIds].map(Number)
    if (ids.length === 0) {
      setError('请选择至少 1 个账户进行 Codex OAuth')
      return
    }
    setOauthBusy(true)
    try {
      const data = await apiFetch('/tasks/codex-oauth', {
        method: 'POST',
        body: JSON.stringify({
          platform: 'chatgpt',
          ids,
          browser_mode: browserMode,
          concurrency: Math.max(Number(actionConcurrency || 1), 1),
        }),
      })
      setOauthTaskId(String(data?.task_id || data?.id || ''))
    } catch (exc: any) {
      setError(exc?.message || t('login.requestFailed'))
    } finally {
      setOauthBusy(false)
    }
  }

  const handleOAuthTaskDone = useCallback(async () => {
    setOauthBusy(false)
    setSelectedIds(new Set())
    await load()
  }, [load])

  // ── 获取rt（refresh_token）──
  const startGetRt = async () => {
    setError('')
    const ids = [...selectedIds].map(Number)
    if (ids.length === 0) {
      setError('请选择至少 1 个账户获取 refresh_token')
      return
    }
    setGetRtBusy(true)
    try {
      const data = await apiFetch('/tasks/get-rt', {
        method: 'POST',
        body: JSON.stringify({
          platform: 'chatgpt',
          ids,
          browser_mode: browserMode,
          concurrency: Math.max(Number(actionConcurrency || 1), 1),
          record_har: getRtRecordHar ? 'true' : '',
          sms_provider: getRtSmsProvider,
          smspool_api_key: getRtSmspoolKey.trim(),
          smspool_max_price: getRtSmspoolMaxPrice.trim() || '0.13',
          smsapi_phone: getRtSmsapiPhone.trim(),
          smsapi_url: getRtSmsapiUrl.trim(),
          phone_reuse_count: Math.max(Number(getRtPhoneReuseCount || 3), 3),
        }),
      })
      setGetRtTaskId(String(data?.task_id || data?.id || ''))
    } catch (exc: any) {
      setError(exc?.message || t('login.requestFailed'))
    } finally {
      setGetRtBusy(false)
    }
  }

  const handleGetRtTaskDone = useCallback(async () => {
    setGetRtBusy(false)
    setSelectedIds(new Set())
    await load()
  }, [load])

  // ── 获取rt(绕过手机号) ──
  const startGetRtBypass = async () => {
    setError('')
    const ids = [...selectedIds].map(Number)
    if (ids.length === 0) {
      setError('请选择至少 1 个账户')
      return
    }
    setGetRtBypassBusy(true)
    try {
      const data = await apiFetch('/tasks/get-rt-bypass', {
        method: 'POST',
        body: JSON.stringify({
          platform: 'chatgpt',
          ids,
          browser_mode: browserMode,
          concurrency: Math.max(Number(actionConcurrency || 1), 1),
        }),
      })
      setGetRtBypassTaskId(String(data?.task_id || data?.id || ''))
    } catch (exc: any) {
      setError(exc?.message || t('login.requestFailed'))
    } finally {
      setGetRtBypassBusy(false)
    }
  }

  const handleGetRtBypassTaskDone = useCallback(async () => {
    setGetRtBypassBusy(false)
    setSelectedIds(new Set())
    await load()
  }, [load])

  const currentPlatformMeta = platformsMap[tab]
  const platformLabel = currentPlatformMeta?.display_name || tab
  const visibleTrial = accounts.filter(acc => getPlanState(acc) === 'trial').length
  const visibleSubscribed = accounts.filter(acc => getPlanState(acc) === 'subscribed').length
  const visibleInvalid = accounts.filter(acc => getValidityStatus(acc) === 'invalid' || getLifecycleStatus(acc) === 'invalid').length
  const linkedCashier = accounts.filter(acc => Boolean(getCashierUrl(acc))).length
  const disabledAccounts = accounts.filter(isDisabledAccount).length
  const enabledAccounts = accounts.filter(acc => !isDisabledAccount(acc) && getValidityStatus(acc) !== 'invalid').length
  const quotaExhausted = accounts.filter(isQuotaExhaustedAccount).length
  const sub2Stats = [
    { title: '账号总数', value: total || accounts.length, subtitle: '全部 auth file', icon: Users, tone: 'cyan' as const },
    { title: '启用中', value: enabledAccounts, subtitle: '可参与调度', icon: ShieldCheck, tone: 'green' as const },
    { title: '已禁用', value: disabledAccounts, subtitle: '停用账号', icon: Ban, tone: 'amber' as const },
    { title: '401报错', value: visibleInvalid, subtitle: 'HTTP 401', icon: AlertTriangle, tone: 'red' as const },
    { title: '额度耗尽', value: quotaExhausted, subtitle: '临时降级', icon: Gauge, tone: 'violet' as const },
  ]

  return (
    <div className="flex h-full min-h-0 flex-col gap-4 overflow-hidden">
      {detail && <DetailModal acc={detail} onClose={() => setDetail(null)} onSave={() => { setDetail(null); load() }} />}
      {showImport && <ImportModal platform={tab} onClose={() => setShowImport(false)} onDone={() => { setShowImport(false); load() }} />}
      {showAdd && <AddModal platform={tab} onClose={() => setShowAdd(false)} onDone={() => { setShowAdd(false); load() }} />}
      {actionResult && <ActionResultModal title={actionResult.title} payload={actionResult.payload} onClose={() => setActionResult(null)} />}
      {batchTask && (
        <ActionTaskModal
          title={batchTask.title}
          taskId={batchTask.taskId}
          taskStatus={batchTaskStatus}
          onClose={() => {
            setBatchTask(null)
            setBatchTaskStatus(null)
            setBatchRefreshing(false)
            load()
          }}
          onDone={(status) => {
            setBatchTaskStatus(status)
            setBatchRefreshing(false)
            load()
          }}
        />
      )}
      {oauthTaskId && (
        createPortal(
          <div className="dialog-backdrop" onClick={() => setOauthTaskId('')}>
            <div
              className="dialog-panel flex max-h-[82vh] flex-col"
              onClick={event => event.stopPropagation()}
            >
              <div className="flex items-center justify-between border-b border-[var(--border)] px-6 py-4">
                <div>
                  <h2 className="text-base font-semibold text-[var(--text-primary)]">
                    Codex OAuth
                  </h2>
                  <div className="mt-1 text-xs text-[var(--text-muted)]">
                    Task logs are shown here.
                  </div>
                </div>
                <button
                  onClick={() => setOauthTaskId('')}
                  className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
              <div className="min-h-0 flex-1 px-6 py-4">
                <div className="h-[420px] min-h-0 rounded border border-[var(--border)] p-3">
                  <TaskLogPanel taskId={oauthTaskId} onDone={handleOAuthTaskDone} />
                </div>
              </div>
              <div className="flex justify-end gap-2 border-t border-[var(--border)] px-6 py-3">
                <Button variant="outline" size="sm" onClick={() => setOauthTaskId('')}>
                  {t('common.close')}
                </Button>
              </div>
            </div>
          </div>,
          document.body,
        )
      )}
      {oauthConfirmOpen && (
        createPortal(
          <div
            className="dialog-backdrop"
            onClick={() => !oauthBusy && setOauthConfirmOpen(false)}
          >
            <div
              className="dialog-panel flex max-h-[82vh] flex-col"
              onClick={event => event.stopPropagation()}
            >
              <div className="flex items-center justify-between border-b border-[var(--border)] px-6 py-4">
                <div>
                  <h2 className="text-base font-semibold text-[var(--text-primary)]">
                    Codex OAuth
                  </h2>
                  <div className="mt-1 text-xs text-[var(--text-muted)]">
                    Selected {selectedIds.size} account(s). Choose browser mode and concurrency.
                  </div>
                </div>
                <button
                  onClick={() => !oauthBusy && setOauthConfirmOpen(false)}
                  className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
              <div className="space-y-4 px-6 py-4">
                <div>
                  <label className="mb-1 block text-xs text-[var(--text-muted)]">
                    Browser mode
                  </label>
                  <select
                    value={browserMode}
                    onChange={event => setBrowserMode(event.target.value)}
                    className="control-surface control-surface-compact w-full"
                  >
                    {BROWSER_MODE_OPTIONS.map(option => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="mb-1 block text-xs text-[var(--text-muted)]">
                    Concurrency
                  </label>
                  <input
                    type="number"
                    min={1}
                    value={actionConcurrency}
                    onChange={event =>
                      setActionConcurrency(Math.max(Number(event.target.value || 1), 1))
                    }
                    className="control-surface control-surface-compact w-full text-center"
                  />
                </div>
              </div>
              <div className="flex justify-end gap-2 border-t border-[var(--border)] px-6 py-3">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setOauthConfirmOpen(false)}
                  disabled={oauthBusy}
                >
                  {t('common.close')}
                </Button>
                <Button
                  size="sm"
                  onClick={async () => {
                    setOauthConfirmOpen(false)
                    await startCodexOAuth()
                  }}
                  disabled={oauthBusy || selectedIds.size === 0}
                >
                  {oauthBusy ? (
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  ) : (
                    <ShieldCheck className="mr-2 h-4 w-4" />
                  )}
                  Start
                </Button>
              </div>
            </div>
          </div>,
          document.body,
        )
      )}
      {getRtConfirmOpen && (
        createPortal(
          <div
            className="dialog-backdrop"
            onClick={() => !getRtBusy && setGetRtConfirmOpen(false)}
          >
            <div
              className="dialog-panel flex max-h-[82vh] flex-col"
              onClick={event => event.stopPropagation()}
            >
              <div className="flex items-center justify-between border-b border-[var(--border)] px-6 py-4">
                <div>
                  <h2 className="text-base font-semibold text-[var(--text-primary)]">
                    获取rt（refresh_token）
                  </h2>
                  <div className="mt-1 text-xs text-[var(--text-muted)]">
                    已选 {selectedIds.size} 个账户。使用浏览器 OAuth + 手机验证跳过获取 refresh_token。
                  </div>
                </div>
                <button
                  onClick={() => !getRtBusy && setGetRtConfirmOpen(false)}
                  className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
              <div className="space-y-4 px-6 py-4">
                <div>
                  <label className="mb-1 block text-xs text-[var(--text-muted)]">
                    Browser mode
                  </label>
                  <select
                    value={browserMode}
                    onChange={event => setBrowserMode(event.target.value)}
                    className="control-surface control-surface-compact w-full"
                  >
                    {BROWSER_MODE_OPTIONS.map(option => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="mb-1 block text-xs text-[var(--text-muted)]">
                    Concurrency
                  </label>
                  <input
                    type="number"
                    min={1}
                    value={actionConcurrency}
                    onChange={event =>
                      setActionConcurrency(Math.max(Number(event.target.value || 1), 1))
                    }
                    className="control-surface control-surface-compact w-full text-center"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-xs text-[var(--text-muted)]">
                    Phone reuse count
                  </label>
                  <input
                    type="number"
                    min={3}
                    value={getRtPhoneReuseCount}
                    onChange={event =>
                      setGetRtPhoneReuseCount(Math.max(Number(event.target.value || 3), 3))
                    }
                    className="control-surface control-surface-compact w-full text-center"
                  />
                  <div className="mt-1 text-[11px] text-[var(--text-muted)]">
                    One phone is reused for at least 3 successful accounts, then the task switches to a new phone.
                  </div>
                </div>
                <label className="flex items-start gap-2 rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-4 py-3 cursor-pointer hover:border-[var(--accent)]/60">
                  <input
                    type="checkbox"
                    checked={getRtRecordHar}
                    onChange={event => setGetRtRecordHar(event.target.checked)}
                    className="mt-0.5 h-4 w-4 cursor-pointer accent-[var(--accent)]"
                  />
                  <div className="flex-1 text-xs text-[var(--text-secondary)]">
                    <div className="text-sm font-medium text-[var(--text-primary)]">
                      Capture Camoufox HAR
                    </div>
                    <div className="mt-0.5">
                      Saves the OAuth browser network log to tools/captures. Use camoufox_headed or camoufox_headless.
                    </div>
                    {getRtRecordHar && !browserMode.startsWith('camoufox_') ? (
                      <div className="mt-2 text-[11px] text-amber-400">
                        Current browser mode does not support HAR recording. Switch to Camoufox to write a HAR file.
                      </div>
                    ) : null}
                  </div>
                </label>
                {/* ── 手机号接码（可选）── */}
                <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-4 py-3 space-y-3">
                  <div className="text-sm font-medium text-[var(--text-primary)]">手机号接码（可选，跳过则遇到 add_phone 会失败）</div>
                  <div>
                    <label className="mb-1 block text-xs text-[var(--text-muted)]">接码渠道</label>
                    <select
                      value={getRtSmsProvider}
                      onChange={e => setGetRtSmsProvider(e.target.value)}
                      className="control-surface control-surface-compact w-full"
                    >
                      <option value="">(不启用)</option>
                      <option value="smspool">SMSPool</option>
                      <option value="smsapi">SmsApi（自有固定号）</option>
                    </select>
                  </div>
                  {getRtSmsProvider === 'smspool' && (
                    <>
                      <div>
                        <label className="mb-1 block text-xs text-[var(--text-muted)]">
                          SMSPool API Key（留空用内置默认 key）
                        </label>
                        <input
                          type="text"
                          value={getRtSmspoolKey}
                          onChange={e => setGetRtSmspoolKey(e.target.value)}
                          placeholder="SMSPool API key"
                          className="control-surface control-surface-compact w-full"
                        />
                      </div>
                      <div>
                        <label className="mb-1 block text-xs text-[var(--text-muted)]">
                          价格上限 USD（默认 0.13）
                        </label>
                        <input
                          type="text"
                          value={getRtSmspoolMaxPrice}
                          onChange={e => setGetRtSmspoolMaxPrice(e.target.value.replace(/[^0-9.]/g, ''))}
                          placeholder="0.13"
                          className="control-surface control-surface-compact w-full text-center font-mono"
                        />
                      </div>
                      <div className="text-[11px] text-[var(--text-muted)]">
                        租美国号（country=1），OpenAI/ChatGPT service=671
                      </div>
                    </>
                  )}
                  {getRtSmsProvider === 'smsapi' && (
                    <>
                      <div>
                        <label className="mb-1 block text-xs text-[var(--text-muted)]">
                          手机号 + 查询 URL（支持 +1XXXXXXXX----URL 格式）
                        </label>
                        <textarea
                          value={getRtSmsapiPhone}
                          onChange={e => setGetRtSmsapiPhone(e.target.value)}
                          rows={3}
                          placeholder={"+12025550101----https://relay.example.com/api/sms/recordText?key=RELAY_KEY_1\n+12025550102----https://relay.example.com/api/sms/recordText?key=RELAY_KEY_2"}
                          className="control-surface control-surface-compact w-full resize-none font-mono text-xs"
                        />
                      </div>
                      <div>
                        <label className="mb-1 block text-xs text-[var(--text-muted)]">
                          查询 URL（与手机号分开填写时用；上面的手机号字段已含 ----URL 则无需填写）
                        </label>
                        <input
                          type="text"
                          value={getRtSmsapiUrl}
                          onChange={e => setGetRtSmsapiUrl(e.target.value)}
                          placeholder="https://relay.example.com/api/sms/recordText?key=RELAY_KEY"
                          className="control-surface control-surface-compact w-full"
                        />
                      </div>
                    </>
                  )}
                  <div className="text-[11px] text-[var(--text-muted)]">
                    浏览器填表时自动去除 +1 区号，使用本地号码格式。
                  </div>
                </div>
              </div>
              <div className="flex justify-end gap-2 border-t border-[var(--border)] px-6 py-3">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setGetRtConfirmOpen(false)}
                  disabled={getRtBusy}
                >
                  {t('common.close')}
                </Button>
                <Button
                  size="sm"
                  onClick={async () => {
                    setGetRtConfirmOpen(false)
                    await startGetRt()
                  }}
                  disabled={getRtBusy || selectedIds.size === 0}
                >
                  {getRtBusy ? (
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  ) : (
                    <Zap className="mr-2 h-4 w-4" />
                  )}
                  开始获取
                </Button>
              </div>
            </div>
          </div>,
          document.body,
        )
      )}
      {getRtTaskId && (
        createPortal(
          <div className="dialog-backdrop" onClick={() => setGetRtTaskId('')}>
            <div
              className="dialog-panel flex max-h-[82vh] flex-col"
              onClick={event => event.stopPropagation()}
            >
              <div className="flex items-center justify-between border-b border-[var(--border)] px-6 py-4">
                <div>
                  <h2 className="text-base font-semibold text-[var(--text-primary)]">
                    获取rt
                  </h2>
                  <div className="mt-1 text-xs text-[var(--text-muted)]">
                    Task logs are shown here.
                  </div>
                </div>
                <button
                  onClick={() => setGetRtTaskId('')}
                  className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
              <div className="min-h-0 flex-1 px-6 py-4">
                <div className="h-[420px] min-h-0 rounded border border-[var(--border)] p-3">
                  <TaskLogPanel taskId={getRtTaskId} onDone={handleGetRtTaskDone} />
                </div>
              </div>
              <div className="flex justify-end gap-2 border-t border-[var(--border)] px-6 py-3">
                <Button variant="outline" size="sm" onClick={() => setGetRtTaskId('')}>
                  {t('common.close')}
                </Button>
              </div>
            </div>
          </div>,
          document.body,
        )
      )}
      {/* ── 获取rt(绕过) 确认弹窗 ── */}
      {getRtBypassConfirmOpen && (
        createPortal(
          <div className="dialog-backdrop" onClick={() => !getRtBypassBusy && setGetRtBypassConfirmOpen(false)}>
            <div className="dialog-panel flex max-h-[82vh] flex-col" onClick={event => event.stopPropagation()}>
              <div className="flex items-center justify-between border-b border-[var(--border)] px-6 py-4">
                <div>
                  <h2 className="text-base font-semibold text-[var(--text-primary)]">获取rt（绕过手机号）</h2>
                  <div className="mt-1 text-xs text-[var(--text-muted)]">
                    已选 {selectedIds.size} 个账户。拦截 session/select 跳过手机验证。
                  </div>
                </div>
                <button onClick={() => !getRtBypassBusy && setGetRtBypassConfirmOpen(false)} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"><X className="h-4 w-4" /></button>
              </div>
              <div className="space-y-4 px-6 py-4">
                <div>
                  <label className="mb-1 block text-xs text-[var(--text-muted)]">Browser mode</label>
                  <select value={browserMode} onChange={event => setBrowserMode(event.target.value)} className="control-surface control-surface-compact w-full">
                    {BROWSER_MODE_OPTIONS.map(option => (<option key={option.value} value={option.value}>{option.label}</option>))}
                  </select>
                </div>
                <div>
                  <label className="mb-1 block text-xs text-[var(--text-muted)]">Concurrency</label>
                  <input type="number" min={1} value={actionConcurrency}
                    onChange={event => setActionConcurrency(Math.max(Number(event.target.value || 1), 1))}
                    className="control-surface control-surface-compact w-full text-center" />
                </div>
                <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)] px-4 py-3 text-xs text-[var(--text-secondary)]">
                  拦截 POST session/select 响应，将 phone_otp_* 替换为 consent 类型，浏览器直接跳授权同意页。
                </div>
              </div>
              <div className="flex justify-end gap-2 border-t border-[var(--border)] px-6 py-3">
                <Button variant="outline" size="sm" onClick={() => setGetRtBypassConfirmOpen(false)} disabled={getRtBypassBusy}>{t('common.close')}</Button>
                <Button size="sm" onClick={async () => { setGetRtBypassConfirmOpen(false); await startGetRtBypass() }} disabled={getRtBypassBusy || selectedIds.size === 0}>
                  {getRtBypassBusy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <ShieldCheck className="mr-2 h-4 w-4" />}
                  开始
                </Button>
              </div>
            </div>
          </div>,
          document.body,
        )
      )}
      {/* ── 获取rt(绕过) 任务日志 ── */}
      {getRtBypassTaskId && (
        createPortal(
          <div className="dialog-backdrop" onClick={() => setGetRtBypassTaskId('')}>
            <div className="dialog-panel flex max-h-[82vh] flex-col" onClick={event => event.stopPropagation()}>
              <div className="flex items-center justify-between border-b border-[var(--border)] px-6 py-4">
                <div><h2 className="text-base font-semibold text-[var(--text-primary)]">获取rt(绕过)</h2><div className="mt-1 text-xs text-[var(--text-muted)]">Task logs</div></div>
                <button onClick={() => setGetRtBypassTaskId('')} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"><X className="h-4 w-4" /></button>
              </div>
              <div className="min-h-0 flex-1 px-6 py-4">
                <div className="h-[420px] min-h-0 rounded border border-[var(--border)] p-3">
                  <TaskLogPanel taskId={getRtBypassTaskId} onDone={handleGetRtBypassTaskDone} />
                </div>
              </div>
              <div className="flex justify-end gap-2 border-t border-[var(--border)] px-6 py-3">
                <Button variant="outline" size="sm" onClick={() => setGetRtBypassTaskId('')}>{t('common.close')}</Button>
              </div>
            </div>
          </div>,
          document.body,
        )
      )}
      {error && (
        <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      )}

      <div className="shrink-0 rounded-2xl border border-slate-200 bg-[#f3f7fa] p-3 shadow-sm">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
          {sub2Stats.map(item => (
            <Sub2StatCard
              key={item.title}
              icon={item.icon}
              title={item.title}
              value={item.value}
              subtitle={item.subtitle}
              tone={item.tone}
            />
          ))}
        </div>
      </div>

      <Card className="shrink-0 bg-[var(--bg-pane)]/40 border border-[var(--border)] shadow-sm">
        <div className="flex flex-col gap-3 px-5 py-4 border-b border-[var(--border)]/50 lg:flex-row lg:items-start lg:justify-between">
          <div className="flex min-w-0 shrink-0 flex-wrap items-center gap-3">
            <h1 className="shrink-0 text-lg font-semibold tracking-tight text-[var(--text-primary)]">
              {platformLabel}
            </h1>
            <div className="hidden h-4 w-[1px] bg-[var(--border)] sm:block"></div>
            <div className="flex min-w-0 flex-wrap items-center gap-1.5 text-xs">
              <span className="shrink-0 text-[var(--text-muted)]">{t('accounts.count', { count: total })}</span>
              {visibleTrial > 0 && <span className="flex items-center rounded-full bg-emerald-500/10 px-2 py-0.5 font-medium text-emerald-500 ring-1 ring-inset ring-emerald-500/20">{t('accounts.trial', { count: visibleTrial })}</span>}
              {visibleSubscribed > 0 && <span className="flex items-center rounded-full bg-blue-500/10 px-2 py-0.5 font-medium text-blue-500 ring-1 ring-inset ring-blue-500/20">{t('accounts.subscribed', { count: visibleSubscribed })}</span>}
              {linkedCashier > 0 && <span className="flex items-center rounded-full bg-amber-500/10 px-2 py-0.5 font-medium text-amber-500 ring-1 ring-inset ring-amber-500/20">{t('accounts.linked', { count: linkedCashier })}</span>}
              {visibleInvalid > 0 && <span className="flex items-center rounded-full bg-red-500/10 px-2 py-0.5 font-medium text-red-500 ring-1 ring-inset ring-red-500/20">{t('accounts.invalid', { count: visibleInvalid })}</span>}
              {selectedCount > 0 && <span className="flex items-center rounded-full bg-[var(--text-primary)]/10 px-2 py-0.5 font-medium text-[var(--text-primary)] ring-1 ring-inset ring-[var(--text-primary)]/20">{t('accounts.selected', { count: selectedCount })}</span>}
            </div>
          </div>
          <div className="grid w-full gap-2 xl:w-auto xl:min-w-[660px] xl:grid-cols-[1fr_1fr_1.35fr]">
            <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)]/70 p-2">
              <div className="mb-1 px-1 text-[10px] font-semibold uppercase tracking-[0.18em] text-[var(--text-muted)]">账号录入</div>
              <div className="flex flex-wrap gap-2">
                <Button
                  size="sm"
                  onClick={() => navigate(`/register?platform=${encodeURIComponent(tab)}&from=accounts`)}
                  className="h-8 flex-1 shrink-0 whitespace-nowrap shadow-sm"
                >
                  <Plus className="mr-1.5 h-3.5 w-3.5 shrink-0" />
                  {t('accounts.autoRegister')}
                </Button>
                <Button size="sm" variant="outline" onClick={() => setShowAdd(true)} className="h-8 flex-1 shrink-0 whitespace-nowrap bg-transparent">
                  <Plus className="mr-1.5 h-3.5 w-3.5 shrink-0" />
                  {t('accounts.manualAdd')}
                </Button>
              </div>
            </div>

            <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)]/70 p-2">
              <div className="mb-1 px-1 text-[10px] font-semibold uppercase tracking-[0.18em] text-[var(--text-muted)]">导入与导出</div>
              <div className="flex flex-wrap gap-2">
                <Button size="sm" variant="outline" onClick={() => setShowImport(true)} className="h-8 flex-1 shrink-0 whitespace-nowrap bg-transparent">
                  <Upload className="mr-1.5 h-3.5 w-3.5 shrink-0" />
                  {t('accounts.import')}
                </Button>
                {tab === 'chatgpt' ? (
                  <ExportMenu
                    platform={tab}
                    total={total}
                    statusFilter={filterStatus}
                    searchFilter={debouncedSearch}
                    selectedIds={[...selectedIds]}
                    onExported={() => {
                      setSelectedIds(new Set())
                      load()
                    }}
                  />
                ) : (
                  <Button size="sm" variant="outline" onClick={exportCsv} disabled={accounts.length === 0} className="h-8 flex-1 shrink-0 whitespace-nowrap bg-transparent">
                    <Download className="mr-1.5 h-3.5 w-3.5 shrink-0" />
                    {t('accounts.export')}
                  </Button>
                )}
              </div>
            </div>

            <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)]/70 p-2">
              <div className="mb-1 flex items-center justify-between gap-2 px-1">
                <span className="text-[10px] font-semibold uppercase tracking-[0.18em] text-[var(--text-muted)]">凭证操作</span>
                <button
                  type="button"
                  onClick={() => { window.location.href = '/history' }}
                  className="text-[11px] text-[var(--text-muted)] hover:text-[var(--accent)]"
                >
                  任务日志
                </button>
              </div>
              <div className="flex flex-wrap gap-2">
                {tab === 'chatgpt' && (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      setError('')
                      if (selectedIds.size === 0) {
                        setError('请至少选择 1 个账号执行 Codex OAuth')
                        return
                      }
                      setOauthConfirmOpen(true)
                    }}
                    disabled={oauthBusy || selectedCount === 0}
                    className="h-8 flex-1 shrink-0 whitespace-nowrap bg-transparent"
                  >
                    {oauthBusy ? (
                      <Loader2 className="mr-1.5 h-3.5 w-3.5 shrink-0 animate-spin" />
                    ) : (
                      <ShieldCheck className="mr-1.5 h-3.5 w-3.5 shrink-0" />
                    )}
                    Codex OAuth
                  </Button>
                )}
                {tab === 'chatgpt' && (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      setError('')
                      if (selectedIds.size === 0) {
                        setError('请至少选择 1 个账号获取 refresh_token')
                        return
                      }
                      setGetRtConfirmOpen(true)
                    }}
                    disabled={getRtBusy || selectedCount === 0}
                    className="h-8 flex-1 shrink-0 whitespace-nowrap bg-transparent"
                  >
                    {getRtBusy ? (
                      <Loader2 className="mr-1.5 h-3.5 w-3.5 shrink-0 animate-spin" />
                    ) : (
                      <Zap className="mr-1.5 h-3.5 w-3.5 shrink-0" />
                    )}
                    获取 RT
                  </Button>
                )}
                {tab === 'chatgpt' && (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      setError('')
                      if (selectedIds.size === 0) {
                        setError('请至少选择 1 个账号')
                        return
                      }
                      setGetRtBypassConfirmOpen(true)
                    }}
                    disabled={getRtBypassBusy || selectedCount === 0}
                    className="h-8 flex-1 shrink-0 whitespace-nowrap bg-transparent"
                  >
                    {getRtBypassBusy ? (
                      <Loader2 className="mr-1.5 h-3.5 w-3.5 shrink-0 animate-spin" />
                    ) : (
                      <ShieldCheck className="mr-1.5 h-3.5 w-3.5 shrink-0" />
                    )}
                    获取 RT（绕过）
                  </Button>
                )}
              </div>
            </div>
          </div>
        </div>
        
        {/* Search & Filter Toolbar */}
        <div className="flex items-center justify-between gap-4 px-5 py-2.5 bg-[var(--bg-pane)]/20">
          <div className="flex flex-1 items-center gap-3">
            <div className="relative flex-1 max-w-sm">
              <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-2.5 text-[var(--text-muted)]">
                <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="8"></circle><path d="m21 21-4.3-4.3"></path></svg>
              </div>
              <input
                type="text"
                placeholder={t('accounts.searchPlaceholder')}
                value={search}
                onChange={e => setSearch(e.target.value)}
                className="w-full rounded-md border border-[var(--border)] bg-transparent py-1.5 pl-8 pr-3 text-sm text-[var(--text-primary)] transition-colors placeholder:text-[var(--text-muted)] focus:border-[var(--text-primary)] focus:outline-none focus:ring-1 focus:ring-[var(--text-primary)]"
              />
            </div>
            <select
              value={filterStatus}
              onChange={e => {
                setPage(1)
                setFilterStatus(e.target.value)
              }}
              className="rounded-md border border-[var(--border)] bg-transparent py-1.5 pl-3 pr-8 text-sm text-[var(--text-primary)] transition-colors focus:border-[var(--text-primary)] focus:outline-none focus:ring-1 focus:ring-[var(--text-primary)] appearance-none"
              style={{ backgroundImage: `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E")`, backgroundPosition: 'right 8px center', backgroundRepeat: 'no-repeat' }}
            >
              <option value="">{t('accounts.allStatuses')}</option>
              <option value="registered">{translateAccountStatus('registered', language)}</option>
              <option value="trial">{t('dashboard.trial')}</option>
              <option value="subscribed">{t('dashboard.subscribed')}</option>
              <option value="free">{t('accounts.free')}</option>
              <option value="eligible">{t('accounts.eligible')}</option>
              <option value="expired">{t('accounts.expired')}</option>
              <option value="invalid">{t('dashboard.invalid')}</option>
            </select>
          </div>
          
          <div className="flex items-center gap-2">
            <AutoRefreshIndicator
              busy={autoRefreshBusy}
              intervalSeconds={survivalRefreshSeconds}
              nextAt={nextAutoRefreshAt}
              lastAt={lastAutoRefreshAt}
            />
            <label className="hidden items-center gap-1.5 text-[11px] text-[var(--text-muted)] sm:flex" title="选择自动同步账号列表和存活时间的间隔">
              间隔
              <select
                value={survivalRefreshSeconds}
                onChange={event => setSurvivalRefreshSeconds(Number(event.target.value))}
                className="rounded-md border border-[var(--border)] bg-transparent px-2 py-1 text-[11px] text-[var(--text-primary)] focus:border-[var(--text-primary)] focus:outline-none focus:ring-1 focus:ring-[var(--text-primary)]"
                aria-label="自动刷新间隔"
              >
                <option value={0}>手动</option>
                <option value={15}>15 秒</option>
                <option value={30}>30 秒</option>
                <option value={60}>1 分钟</option>
                <option value={120}>2 分钟</option>
              </select>
            </label>
            <Button
              variant="ghost"
              size="sm"
              disabled={batchRefreshing || loading}
              className="h-7 px-2.5 text-[var(--text-muted)] hover:text-amber-500 hover:bg-amber-500/10"
              title={t('accounts.refreshCreditsTitle')}
              onClick={async () => {
                setBatchRefreshing(true)
                try {
                  const res = await apiFetch(`/accounts/check-all?platform=${tab}`, { method: 'POST' })
                  if (res?.task_id) {
                    setBatchTask({ taskId: res.task_id, title: t('accounts.refreshAllCreditsTask', { platform: platformLabel }) })
                    setBatchTaskStatus(null)
                  }
                } catch (e) {
                  console.error(e)
                  setBatchRefreshing(false)
                }
              }}
            >
              <Zap className={`mr-1 h-3.5 w-3.5 ${batchRefreshing ? 'animate-pulse' : ''}`} />
              {batchRefreshing ? t('accounts.refreshingCredits') : t('accounts.refreshCredits')}
            </Button>
            <Button variant="ghost" size="sm" onClick={() => load()} disabled={loading} className="h-7 w-7 p-0 text-[var(--text-muted)] hover:text-[var(--text-primary)]">
              <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            </Button>
            {selectedCount > 0 && (
              <Button
                size="sm"
                variant="ghost"
                disabled={bulkDeleting}
                className="h-7 px-2.5 text-red-500 hover:bg-red-500/10 hover:text-red-600"
                onClick={async () => {
                  if (!confirm(t('accounts.deleteSelectedConfirm', { count: selectedCount }))) return
                  setBulkDeleting(true)
                  try {
                    await Promise.allSettled(
                      [...selectedIds].map(id => apiFetch(`/accounts/${id}`, { method: 'DELETE' }))
                    )
                    setSelectedIds(new Set())
                    load()
                  } finally {
                    setBulkDeleting(false)
                  }
                }}
              >
                <Trash2 className="mr-1.5 h-3.5 w-3.5" />
                {bulkDeleting ? t('common.deleting') : t('common.delete')}
              </Button>
            )}
          </div>
        </div>
      </Card>

      <Card className="min-h-0 flex-1 overflow-hidden p-0 border border-[var(--border)] shadow-sm">
        <div className="flex h-full min-h-0 flex-col">
          <div className="glass-table-wrap min-h-0 flex-1 overflow-auto">
        <table className="table-fixed w-full min-w-[900px] text-sm">
          <colgroup>
            <col className="w-10" />
            <col className="w-[24%]" />
            <col className="w-[9%]" />
            <col className="w-[18%]" />
            <col className="w-[11%]" />
            <col className="w-[9%]" />
            <col className="w-[6%]" />
            <col className="w-[9%]" />
            <col className="w-[9%]" />
          </colgroup>
          <thead className="sticky top-0 z-10  bg-[var(--bg-pane)]/80">
            <tr className="border-b border-[var(--border)] text-xs uppercase tracking-wider font-medium text-[var(--text-muted)]">
              <th className="w-10 px-3 py-2 text-left">
                <input
                  type="checkbox"
                  checked={allSelectedOnPage}
                  onChange={togglePage}
                  className="checkbox-accent rounded-[3px] border-[var(--border)] focus:ring-[var(--text-primary)] focus:ring-offset-0 bg-transparent text-[var(--text-primary)]"
                />
              </th>
              <th className="px-3 py-2 text-left">{t('common.email')}</th>
              <th className="px-3 py-2 text-left">{t('common.password')}</th>
              <th className="px-3 py-2 text-left">{t('common.status')}</th>
              <th className="px-3 py-2 text-left">存活时间</th>
              <th className="px-3 py-2 text-left">邮箱接码</th>
              <th className="px-3 py-2 text-left">{t('accounts.link')}</th>
              <th className="px-3 py-2 text-left">{t('accounts.registeredAt')}</th>
              <th className="px-3 py-2 text-right">{t('common.actions')}</th>
            </tr>
          </thead>
          <tbody>
            {accounts.length === 0 && (
              <tr>
                <td colSpan={9} className="px-4 py-24 text-center">
                  <div className="flex flex-col items-center justify-center space-y-3">
                    <div className="flex h-12 w-12 items-center justify-center rounded-full bg-[var(--bg-pane)] border border-[var(--border)] shadow-sm">
                      <svg className="h-6 w-6 text-[var(--text-muted)]" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4" /></svg>
                    </div>
                    <h3 className="text-sm font-medium text-[var(--text-primary)]">{t('accounts.emptyTitle')}</h3>
                    <p className="text-xs text-[var(--text-muted)] max-w-sm">{t('accounts.emptyDesc')}</p>
                  </div>
                </td>
              </tr>
            )}
            {accounts.map(acc => (
              (() => {
                const overview = getAccountOverview(acc)
                const verificationMailbox = getVerificationMailbox(acc)
                const primaryMetrics = getPrimaryMetrics(acc)
                const displayBadges = getDisplayBadges(acc)
                const sub2SyncInfo = getSub2SyncInfo(acc)
                return (
              <tr key={acc.id} className="group border-b border-[var(--border)]/30 hover:bg-[var(--text-primary)]/[0.02] transition-colors cursor-pointer"
                  onClick={() => setDetail(acc)}>
                <td className="px-3 py-2.5" onClick={e => e.stopPropagation()}>
                  <input
                    type="checkbox"
                    checked={selectedIds.has(acc.id)}
                    onChange={() => toggleOne(acc.id)}
                    className="checkbox-accent rounded-[3px] border-[var(--border)] focus:ring-[var(--text-primary)] focus:ring-offset-0 bg-transparent text-[var(--text-primary)] transition-all opacity-40 group-hover:opacity-100 data-[state=checked]:opacity-100"
                  />
                </td>
                <td className="px-3 py-2.5 font-mono text-sm text-[var(--text-primary)] align-top">
                  <div className="flex min-w-0 items-center gap-1.5">
                    <span className="truncate tracking-tight" title={acc.email}>{acc.email}</span>
                    <button onClick={e => { e.stopPropagation(); copy(emailApiLine(acc.email)) }} title="复制 Email+邮件API" className="text-[var(--text-muted)] hover:text-[var(--text-primary)] opacity-0 group-hover:opacity-100 transition-opacity"><Copy className="h-3 w-3" /></button>
                  </div>
                  {verificationMailbox && (verificationMailbox.email || verificationMailbox.account_id || verificationMailbox.provider) && (
                    <div
                      className="mt-1 truncate text-xs text-[var(--text-muted)] flex items-center gap-1"
                      title={`验证邮箱: ${verificationMailbox.email || '-'} · ${verificationMailbox.provider || '-'}`}
                    >
                      <svg className="w-3 h-3 opacity-60 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z" /><polyline points="22,6 12,13 2,6" /></svg>
                      <span className="truncate">{verificationMailbox.email || '-'}</span>
                    </div>
                  )}
                  {overview?.remote_email && overview.remote_email !== acc.email && (
                    <div className="mt-1 truncate text-xs text-[var(--text-muted)]" title={`远端邮箱: ${overview.remote_email}`}>
                      远端邮箱: {overview.remote_email}
                    </div>
                  )}
                  {displayBadges.length > 0 && (
                    <div className="mt-1.5 flex flex-wrap gap-1">
                      {displayBadges.slice(0, 3).map((badge: any, index: number) => (
                        <span key={`${badge?.label || 'badge'}-${index}`} className="rounded border border-[var(--border)]/50 bg-[var(--bg-pane)]/40 px-1 py-0.5 text-[11px] font-medium text-[var(--text-muted)] shadow-sm">
                          {badge?.label}
                        </span>
                      ))}
                    </div>
                  )}
                </td>
                <td className="px-3 py-2.5 font-mono text-[13px] text-[var(--text-muted)] align-top">
                  <div className="flex min-w-0 items-center gap-1.5">
                    <span className="truncate select-none tracking-[0.18em] text-[var(--text-muted)]" title="点击复制按钮复制密码">••••••••</span>
                    <button onClick={e => { e.stopPropagation(); copy(acc.password) }} className="text-[var(--text-muted)] hover:text-[var(--text-primary)] opacity-0 group-hover:opacity-100 transition-opacity"><Copy className="h-3 w-3" /></button>
                  </div>
                </td>
                <td className="px-3 py-2.5 align-top">
                  <div className="min-w-0 flex flex-col items-start gap-1.5">
                    {(() => {
                      const status = getDisplayStatus(acc);
                      const variant = String(STATUS_VARIANT[status] || 'secondary');
                      const styles = (({
                        success: "bg-emerald-500/10 text-emerald-500 ring-emerald-500/20",
                        warning: "bg-amber-500/10 text-amber-500 ring-amber-500/20",
                        danger: "bg-red-500/10 text-red-500 ring-red-500/20",
                        secondary: "bg-[var(--text-primary)]/5 text-[var(--text-secondary)] ring-[var(--border)]",
                        default: "bg-blue-500/10 text-blue-500 ring-blue-500/20"
                      } as Record<string, string>)[variant]) || "bg-[var(--text-primary)]/5 text-[var(--text-secondary)] ring-[var(--border)]";
                      
                      return (
                        <span
                          className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${styles}`}
                          title={getValidityReason(acc) || undefined}
                        >
                          <span className={`mr-1 h-1 w-1 rounded-full ${variant === 'success' ? 'bg-emerald-500 shadow-[0_0_4px_rgba(16,185,129,0.6)]' : variant === 'warning' ? 'bg-amber-500 shadow-[0_0_4px_rgba(245,158,11,0.6)]' : variant === 'danger' ? 'bg-red-500 shadow-[0_0_4px_rgba(239,68,68,0.6)]' : variant === 'default' ? 'bg-blue-500' : 'bg-gray-400'}`}></span>
                          {translateAccountStatus(status, language)}
                        </span>
                      );
                    })()}
                    {sub2SyncInfo && (
                      <span
                        className={`inline-flex max-w-full items-center rounded px-1.5 py-0.5 text-[11px] font-medium ring-1 ring-inset ${
                          sub2SyncInfo.tone === 'success'
                            ? 'bg-emerald-500/10 text-emerald-500 ring-emerald-500/20'
                            : sub2SyncInfo.tone === 'warning'
                              ? 'bg-amber-500/10 text-amber-500 ring-amber-500/20'
                              : sub2SyncInfo.tone === 'danger'
                                ? 'bg-red-500/10 text-red-500 ring-red-500/20'
                                : 'bg-[var(--text-primary)]/5 text-[var(--text-secondary)] ring-[var(--border)]'
                        }`}
                        title={sub2SyncInfo.title || undefined}
                      >
                        <span className="truncate">{sub2SyncInfo.label}</span>
                      </span>
                    )}
                    {primaryMetrics.length > 0 ? (
                      <div className="flex max-w-full flex-col gap-1">
                        {primaryMetrics.slice(0, 2).map((metric: any) => (
                          <div key={metric.key || metric.label} className="flex items-center gap-1.5">
                            <span className="h-1 w-1 rounded-full bg-[var(--text-muted)] opacity-50"></span>
                            <span className="text-xs tracking-tight text-[var(--text-muted)] whitespace-nowrap">
                              <span className="font-medium text-[var(--text-secondary)] mr-0.5">{metric.label}:</span>
                              {metric.value}
                            </span>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div className="max-w-full">
                        <div
                          className="truncate text-xs text-[var(--text-muted)]"
                          title={getCompactStatusMeta(acc)}
                        >
                          {getCompactStatusMeta(acc)}
                        </div>
                        {getValidityReason(acc) && (
                          <div className="mt-0.5 max-w-[320px] truncate text-[11px] text-[var(--text-muted)]" title={getValidityReason(acc)}>
                            {getValidityReason(acc)}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </td>
                <td className="px-3 py-2.5 align-top" onClick={e => e.stopPropagation()}>
                  <SurvivalCell acc={acc} onChanged={load} />
                </td>
                <td className="px-3 py-2.5 align-top">
                  <MailboxCodeCell
                    acc={acc}
                    onResult={(title, payload) => setActionResult({ title, payload })}
                  />
                </td>
                <td className="px-3 py-2.5 align-top">
                  {getCashierUrl(acc) ? (
                    <div className="flex items-center gap-1.5 whitespace-nowrap opacity-70 group-hover:opacity-100 transition-opacity">
                      <button onClick={e => { e.stopPropagation(); copy(getCashierUrl(acc)) }} className="text-[var(--text-muted)] hover:text-[var(--text-primary)] transition-colors p-0.5 rounded hover:bg-[var(--bg-pane)]" title="复制链接"><Copy className="h-3 w-3" /></button>
                      <a href={getCashierUrl(acc)} target="_blank" rel="noreferrer" onClick={e => e.stopPropagation()} className="text-[var(--text-muted)] hover:text-[var(--text-primary)] transition-colors p-0.5 rounded hover:bg-[var(--bg-pane)]" title="打开收银台"><ExternalLink className="h-3 w-3" /></a>
                    </div>
                  ) : <span className="text-[var(--text-muted)]/50 text-xs">-</span>}
                </td>
                <td className="px-3 py-2.5 font-mono text-xs text-[var(--text-muted)] whitespace-nowrap align-top">
                  {acc.created_at ? formatDateTime(acc.created_at, language, { 
                    month: '2-digit', day: '2-digit',
                    hour: '2-digit', minute: '2-digit',
                    hour12: false 
                  }) : '-'}
                </td>
                <td className="px-3 py-2.5 align-top" onClick={e => e.stopPropagation()}>
                  <div className="flex items-center justify-end opacity-60 group-hover:opacity-100 transition-opacity">
                    <ActionMenu
                      acc={acc}
                      onDetail={() => setDetail(acc)}
                      onDelete={() => load()}
                      onResult={(title, payload) => setActionResult({ title, payload })}
                      onChanged={() => load()}
                    />
                  </div>
                </td>
              </tr>
                )
              })()
            ))}
          </tbody>
        </table>
          </div>
          <div className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-t border-[var(--border)] bg-[var(--bg-card)] px-4 py-2.5 text-xs text-[var(--text-muted)]">
            <div>
              共 {total.toLocaleString()} 个账号 · 第 {page}/{totalPages} 页
            </div>
            <div className="flex items-center gap-2">
              <label className="flex items-center gap-2">
                每页
                <select
                  value={pageSize}
                  onChange={event => {
                    setPage(1)
                    setPageSize(Number(event.target.value))
                  }}
                  className="control-surface control-surface-compact h-8 w-20 py-1 text-xs"
                >
                  <option value={20}>20</option>
                  <option value={30}>30</option>
                  <option value={50}>50</option>
                  <option value={100}>100</option>
                </select>
              </label>
              <button
                type="button"
                disabled={page <= 1 || loading}
                onClick={() => setPage(current => Math.max(current - 1, 1))}
                className="h-8 rounded-md border border-[var(--border)] px-3 text-[var(--text-secondary)] disabled:opacity-40"
              >
                上一页
              </button>
              <button
                type="button"
                disabled={page >= totalPages || loading}
                onClick={() => setPage(current => Math.min(current + 1, totalPages))}
                className="h-8 rounded-md border border-[var(--border)] px-3 text-[var(--text-secondary)] disabled:opacity-40"
              >
                下一页
              </button>
            </div>
          </div>
        </div>
      </Card>
    </div>
  )
}
