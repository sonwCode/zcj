import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Boxes, History, KeyRound, Play, Radio, Route, Settings2, ShieldCheck } from 'lucide-react'

import { TaskLogPanel } from '@/components/tasks/TaskLogPanel'
import {
  SmsBowerPriceSelector,
  type SmsBowerSelectionValue,
} from '@/components/registration/SmsBowerPriceSelector'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { apiFetch } from '@/lib/utils'

type Platform = {
  name: string
  display_name: string
  description?: string
  verification?: string
  supported_executors?: string[]
  supported_identity_modes?: string[]
  supported_oauth_providers?: string[]
}

type ProviderSetting = {
  provider_key: string
  display_name?: string
  catalog_label?: string
  enabled?: boolean
  is_default?: boolean
  configured?: boolean
  description?: string
  missing_fields?: string[]
  total_count?: number | null
  available_count?: number | null
  status_message?: string
}

type ProxyStatus = {
  manual_pool: { total: number; active: number; available: boolean }
  mihomo: { configured: boolean; available: boolean; nodes: number; error?: string }
}

type Options = {
  registration_engine: string
  platforms: Platform[]
  mailbox_settings: ProviderSetting[]
  captcha_settings: ProviderSetting[]
  sms_settings: ProviderSetting[]
  proxy_status: ProxyStatus
}

const EMPTY_OPTIONS: Options = {
  registration_engine: 'legacy_isolated',
  platforms: [],
  mailbox_settings: [],
  captcha_settings: [],
  sms_settings: [],
  proxy_status: {
    manual_pool: { total: 0, active: 0, available: false },
    mihomo: { configured: false, available: false, nodes: 0, error: '' },
  },
}

const fieldClass = 'control-surface w-full'
const executorLabels: Record<string, string> = {
  protocol: '纯协议（速度优先）',
  headless: '无头浏览器',
  headed: '可视浏览器',
}
const oauthLabels: Record<string, string> = {
  google: 'Google',
  github: 'GitHub',
  linkedin: 'LinkedIn',
  microsoft: 'Microsoft',
}

function errorText(error: unknown) {
  if (error instanceof Error && error.message) return error.message
  return String(error || '请求失败')
}

function isSmsBowerProvider(providerKey: string) {
  return ['smsbower', 'smsbower_api'].includes(String(providerKey || '').trim().toLowerCase())
}

function normalizeSmsCountryIds(value: string, fallback = '') {
  const raw = String(value || '').split(/[\s,;]+/)
  if (fallback) raw.push(fallback)
  return Array.from(new Set(raw.map(item => item.trim()).filter(Boolean)))
}

function SectionTitle({ number, title, description }: { number: string; title: string; description: string }) {
  return (
    <div className="mb-4">
      <div className="text-[10px] font-bold uppercase tracking-[0.2em] text-[var(--text-muted)]">STEP {number}</div>
      <h2 className="mt-1 font-semibold text-[var(--text-primary)]">{title}</h2>
      <p className="mt-1 text-xs leading-5 text-[var(--text-muted)]">{description}</p>
    </div>
  )
}

export default function OtherRegisterWorkbench() {
  const [options, setOptions] = useState<Options>(EMPTY_OPTIONS)
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [taskId, setTaskId] = useState('')
  const [taskResult, setTaskResult] = useState<any | null>(null)
  const [form, setForm] = useState({
    platform: '', count: 1, concurrency: 1,
    executor_type: 'protocol', identity_provider: 'mailbox',
    oauth_provider: '', oauth_email_hint: '', mail_provider: '',
    email: '', password: '', proxy: '', proxy_strategy: 'auto',
    captcha_solver: 'auto', failure_policy: 'retry_then_continue',
    max_attempts_per_account: 2, chrome_user_data_dir: '',
    chrome_cdp_url: '', otp_timeout: 300, verify_after_registration: true,
    sms_provider: '', sms_country: '187', sms_countries: '187', sms_service: 'dr', sms_max_price: '0.13',
    sms_bulk_price_cny: '', smsbower_provider_ids_by_country: {} as Record<string, string[]>,
    smsbower_auto_country_min_stock: 1, sms_usd_cny_rate: 7.2,
    smsbower_provider_reject_threshold: 2, sms_code_timeout_seconds: 180,
    sms_phone_max_attempts: 8, sms_no_numbers_wait_seconds: 120, sms_tier_cooldown_minutes: 45,
  })

  const set = (key: string, value: string | number | boolean) => {
    setForm(current => ({ ...current, [key]: value }))
  }

  useEffect(() => {
    let cancelled = false
    apiFetch('/legacy-registration/options')
      .then(data => {
        if (cancelled) return
        const loaded = data as Options
        setOptions(loaded)
        const platform = loaded.platforms?.[0]
        const mailboxes = (loaded.mailbox_settings || []).filter(item => item.enabled !== false)
        const mailbox = mailboxes.find(item => item.is_default) || mailboxes[0]
        const captchas = (loaded.captcha_settings || []).filter(item => item.enabled !== false)
        const captcha = captchas.find(item => item.is_default) || captchas[0]
        const smsProviders = loaded.sms_settings || []
        const configuredSmsProviders = smsProviders.filter(item => item.enabled !== false && item.configured !== false)
        const sms = configuredSmsProviders.find(item => item.is_default) || configuredSmsProviders[0]
        setForm(current => ({
          ...current,
          platform: platform?.name || '',
          executor_type: platform?.supported_executors?.includes('protocol')
            ? 'protocol' : platform?.supported_executors?.[0] || '',
          identity_provider: platform?.supported_identity_modes?.includes('mailbox')
            ? 'mailbox' : platform?.supported_identity_modes?.[0] || '',
          oauth_provider: platform?.supported_oauth_providers?.[0] || '',
          mail_provider: mailbox?.provider_key || '',
          captcha_solver: captcha?.provider_key || 'auto',
          sms_provider: sms?.provider_key || '',
          sms_country: isSmsBowerProvider(sms?.provider_key || '') ? (current.sms_country || '187') : current.sms_country,
          sms_countries: isSmsBowerProvider(sms?.provider_key || '') ? (current.sms_countries || '187') : current.sms_countries,
          sms_service: isSmsBowerProvider(sms?.provider_key || '') ? 'dr' : current.sms_service,
          sms_max_price: isSmsBowerProvider(sms?.provider_key || '') ? (current.sms_max_price || '0.13') : current.sms_max_price,
          max_attempts_per_account: platform?.name === 'chatgpt_free' ? 5 : current.max_attempts_per_account,
        }))
      })
      .catch(err => setError(errorText(err)))
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [])

  const platform = useMemo(
    () => options.platforms.find(item => item.name === form.platform) || null,
    [form.platform, options.platforms],
  )
  const mailboxes = useMemo(
    () => options.mailbox_settings.filter(item => item.enabled !== false),
    [options.mailbox_settings],
  )
  const captchas = useMemo(
    () => options.captcha_settings.filter(item => item.enabled !== false),
    [options.captcha_settings],
  )
  const smsProviders = useMemo(
    () => options.sms_settings,
    [options.sms_settings],
  )
  const needsMailbox = form.identity_provider === 'mailbox'
  const needsOAuth = form.identity_provider === 'oauth_browser'
  const isChatgptFree = form.platform === 'chatgpt_free'
  const selectedMailbox = options.mailbox_settings.find(item => item.provider_key === form.mail_provider)
  const selectedCaptcha = options.captcha_settings.find(item => item.provider_key === form.captcha_solver)
  const selectedSmsProvider = options.sms_settings.find(item => item.provider_key === form.sms_provider)
  const isSmsBower = isChatgptFree && isSmsBowerProvider(form.sms_provider)
  const smsCountries = useMemo(
    () => normalizeSmsCountryIds(form.sms_countries, form.sms_country),
    [form.sms_countries, form.sms_country],
  )
  const smsBowerSelection: SmsBowerSelectionValue = {
    country: form.sms_country || smsCountries[0] || '187',
    countries: smsCountries,
    maxPriceUsd: String(form.sms_max_price || '0.13'),
    bulkPriceCny: String(form.sms_bulk_price_cny || ''),
    providerIdsByCountry: form.smsbower_provider_ids_by_country,
    minStock: Math.max(Number(form.smsbower_auto_country_min_stock || 0), 0),
    usdCnyRate: Math.max(Number(form.sms_usd_cny_rate || 7.2), 0.01),
    providerRejectThreshold: Math.max(Number(form.smsbower_provider_reject_threshold || 2), 1),
    codeTimeoutSeconds: Math.min(Math.max(Number(form.sms_code_timeout_seconds || 180), 180), 300),
    phoneMaxAttempts: Math.min(Math.max(Number(form.sms_phone_max_attempts || 8), 1), 20),
    noNumbersWaitSeconds: Math.min(Math.max(Number(form.sms_no_numbers_wait_seconds || 0), 0), 600),
    tierCooldownMinutes: Math.min(Math.max(Number(form.sms_tier_cooldown_minutes || 45), 30), 60),
  }
  const applySmsBowerSelection = useCallback((patch: Partial<SmsBowerSelectionValue>) => {
    setForm(current => ({
      ...current,
      ...(patch.country !== undefined ? { sms_country: patch.country } : {}),
      ...(patch.countries !== undefined ? { sms_countries: patch.countries.join(',') } : {}),
      ...(patch.maxPriceUsd !== undefined ? { sms_max_price: patch.maxPriceUsd } : {}),
      ...(patch.bulkPriceCny !== undefined ? { sms_bulk_price_cny: patch.bulkPriceCny } : {}),
      ...(patch.providerIdsByCountry !== undefined ? { smsbower_provider_ids_by_country: patch.providerIdsByCountry } : {}),
      ...(patch.minStock !== undefined ? { smsbower_auto_country_min_stock: patch.minStock } : {}),
      ...(patch.usdCnyRate !== undefined ? { sms_usd_cny_rate: patch.usdCnyRate } : {}),
      ...(patch.providerRejectThreshold !== undefined ? { smsbower_provider_reject_threshold: patch.providerRejectThreshold } : {}),
      ...(patch.codeTimeoutSeconds !== undefined ? { sms_code_timeout_seconds: patch.codeTimeoutSeconds } : {}),
      ...(patch.phoneMaxAttempts !== undefined ? { sms_phone_max_attempts: patch.phoneMaxAttempts } : {}),
      ...(patch.noNumbersWaitSeconds !== undefined ? { sms_no_numbers_wait_seconds: patch.noNumbersWaitSeconds } : {}),
      ...(patch.tierCooldownMinutes !== undefined ? { sms_tier_cooldown_minutes: patch.tierCooldownMinutes } : {}),
    }))
  }, [])
  const proxyStatus = options.proxy_status
  const reusableBrowser = Boolean(form.chrome_user_data_dir.trim() || form.chrome_cdp_url.trim())
  const blocking = [
    !form.platform && '请选择平台',
    !form.executor_type && '请选择执行器',
    !form.identity_provider && '请选择注册方式',
    needsMailbox && !form.mail_provider && '请先在设置中配置并选择邮箱服务',
    needsMailbox && Boolean(form.mail_provider) && selectedMailbox?.configured === false && `邮箱服务不可用：${selectedMailbox.status_message || '缺少配置'}`,
    needsOAuth && !form.oauth_provider && '请选择 OAuth 提供方',
    needsOAuth && form.executor_type !== 'headed' && 'OAuth 注册必须使用可视浏览器',
    needsOAuth && form.platform === 'tavily' && !reusableBrowser && 'Tavily OAuth 需要 Chrome 用户目录或 CDP 地址',
    form.count > 1 && Boolean(form.email) && '批量注册不能固定复用同一个邮箱',
    !form.proxy && form.proxy_strategy === 'polling' && !proxyStatus.manual_pool.available && '手动代理池没有可用代理',
    !form.proxy && form.proxy_strategy === 'mihomo' && !proxyStatus.mihomo.configured && 'Mihomo 尚未配置订阅',
    !form.proxy && form.proxy_strategy === 'mihomo' && proxyStatus.mihomo.configured && !proxyStatus.mihomo.available && 'Mihomo 当前没有可用节点',
    form.captcha_solver !== 'auto' && selectedCaptcha?.configured === false && `验证码服务不可用：${selectedCaptcha.status_message || '缺少配置'}`,
    isChatgptFree && form.executor_type !== 'protocol' && 'ChatGPT Free 手机接码流程必须使用纯协议执行器',
    isChatgptFree && !form.sms_provider && 'ChatGPT Free 请先选择短信接码 Provider',
    isChatgptFree && Boolean(form.sms_provider) && options.sms_settings.find(item => item.provider_key === form.sms_provider)?.configured === false && 'ChatGPT Free 的短信接码 Provider 尚未配置 API Key',
    isSmsBower && smsCountries.length === 0 && '请至少选择一个 SMSBower 候选国家',
    isSmsBower && smsCountries.includes('12') && 'SMSBower 虚拟/VOIP 国家不能用于 ChatGPT 手机号注册',
    isSmsBower && (!Number.isFinite(Number(form.sms_max_price)) || Number(form.sms_max_price) < 0) && 'SMSBower 单号最高价格必须是大于等于 0 的数字',
  ].filter(Boolean) as string[]

  const choosePlatform = (name: string) => {
    const next = options.platforms.find(item => item.name === name)
    setForm(current => ({
      ...current,
      platform: name,
      executor_type: next?.supported_executors?.includes('protocol')
        ? 'protocol' : next?.supported_executors?.[0] || '',
      identity_provider: next?.supported_identity_modes?.includes('mailbox')
        ? 'mailbox' : next?.supported_identity_modes?.[0] || '',
      oauth_provider: next?.supported_oauth_providers?.[0] || '',
      max_attempts_per_account: current.failure_policy === 'retry_then_continue'
        ? (name === 'chatgpt_free' ? 5 : Math.min(current.max_attempts_per_account, 2))
        : 1,
    }))
  }

  const chooseIdentity = (identity: string) => {
    setForm(current => ({
      ...current,
      identity_provider: identity,
      executor_type: identity === 'oauth_browser' && platform?.supported_executors?.includes('headed')
        ? 'headed' : current.executor_type,
    }))
  }

  const chooseSmsProvider = (providerKey: string) => {
    setForm(current => ({
      ...current,
      sms_provider: providerKey,
      ...(isSmsBowerProvider(providerKey) ? {
        sms_country: current.sms_country || '187',
        sms_countries: current.sms_countries || current.sms_country || '187',
        sms_service: 'dr',
        sms_max_price: current.sms_max_price || '0.13',
      } : {}),
    }))
  }

  const submit = async () => {
    if (blocking.length) return
    setSubmitting(true)
    setError('')
    try {
      const task = await apiFetch('/legacy-registration/tasks', {
        method: 'POST', body: JSON.stringify(form),
      })
      setTaskResult(null)
      setTaskId(String(task.task_id || task.id || ''))
    } catch (err) {
      setError(errorText(err))
    } finally {
      setSubmitting(false)
    }
  }

  const loadTaskResult = async () => {
    if (!taskId) return
    try {
      const task = await apiFetch(`/tasks/${taskId}`)
      setTaskResult(task?.result?.data || null)
    } catch {
      setTaskResult(null)
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-semibold text-[var(--text-primary)]">
            <Boxes className="h-5 w-5 text-violet-400" />GPTFree 多平台注册工作台
          </h1>
          <p className="mt-1 text-sm text-[var(--text-muted)]">老项目的多平台注册能力已迁入独立任务链，不与 aBai ChatGPT 注册共享执行逻辑。</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" asChild><Link to="/settings?tab=mailbox"><Settings2 className="mr-2 h-4 w-4" />资源设置</Link></Button>
          <Button variant="outline" asChild><Link to="/tasks"><History className="mr-2 h-4 w-4" />任务记录</Link></Button>
        </div>
      </div>

      <Card className="border-emerald-500/25 bg-emerald-500/5">
        <div className="flex items-start gap-3">
          <ShieldCheck className="mt-0.5 h-5 w-5 text-emerald-400" />
          <div><div className="font-medium text-[var(--text-primary)]">注册引擎已隔离</div>
            <div className="mt-1 text-sm text-[var(--text-muted)]">这里只包含其他平台的注册流程；ChatGPT 注册与短信验证在独立页面中管理。</div>
          </div>
        </div>
      </Card>

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_360px]">
        <div className="space-y-5">
          <Card>
            <SectionTitle number="01" title="任务目标" description="选择目标平台、成功账号数和同时运行的注册窗口。" />
            <div className="grid gap-4 md:grid-cols-3">
              <label className="space-y-1.5 text-sm text-[var(--text-secondary)]"><span>平台</span><select className={fieldClass} value={form.platform} onChange={event => choosePlatform(event.target.value)} disabled={loading}>{options.platforms.map(item => <option key={item.name} value={item.name}>{item.display_name}</option>)}</select><span className="block text-xs leading-5 text-[var(--text-muted)]">{platform?.description || '加载平台注册能力中…'}</span></label>
              <label className="space-y-1.5 text-sm text-[var(--text-secondary)]"><span>成功目标</span><input className={fieldClass} type="number" min={1} max={100} value={form.count} onChange={event => set('count', Number(event.target.value))} /></label>
              <label className="space-y-1.5 text-sm text-[var(--text-secondary)]"><span>并发窗口</span><input className={fieldClass} type="number" min={1} max={20} value={form.concurrency} onChange={event => set('concurrency', Number(event.target.value))} /></label>
            </div>
          </Card>

          <Card>
            <SectionTitle number="02" title="身份与验证资源" description="邮箱池、固定调试账号、OAuth 浏览器和验证码等待时间；重试会重新分配身份。" />
            <div className="grid gap-4 md:grid-cols-2">
              <label className="space-y-1.5 text-sm text-[var(--text-secondary)]"><span>注册方式</span><select className={fieldClass} value={form.identity_provider} onChange={event => chooseIdentity(event.target.value)}>{(platform?.supported_identity_modes || []).map(mode => <option key={mode} value={mode}>{mode === 'mailbox' ? '系统邮箱' : mode === 'oauth_browser' ? 'OAuth 浏览器' : mode}</option>)}</select></label>
              {needsMailbox ? <label className="space-y-1.5 text-sm text-[var(--text-secondary)]"><span>邮箱服务</span><select className={fieldClass} value={form.mail_provider} onChange={event => set('mail_provider', event.target.value)}><option value="">请选择</option>{mailboxes.map(item => <option key={item.provider_key} value={item.provider_key}>{item.display_name || item.catalog_label || item.provider_key}</option>)}</select></label> : null}
              {isChatgptFree ? <label className="space-y-1.5 text-sm text-[var(--text-secondary)]"><span>短信接码 Provider</span><select className={fieldClass} value={form.sms_provider} onChange={event => chooseSmsProvider(event.target.value)}><option value="">请选择</option>{smsProviders.map(item => <option key={item.provider_key} value={item.provider_key}>{item.display_name || item.catalog_label || item.provider_key}{item.configured === false ? '（未配置）' : ''}</option>)}</select></label> : null}
              {needsOAuth ? <><label className="space-y-1.5 text-sm text-[var(--text-secondary)]"><span>OAuth 提供方</span><select className={fieldClass} value={form.oauth_provider} onChange={event => set('oauth_provider', event.target.value)}>{(platform?.supported_oauth_providers || []).map(provider => <option key={provider} value={provider}>{oauthLabels[provider] || provider}</option>)}</select></label><label className="space-y-1.5 text-sm text-[var(--text-secondary)]"><span>登录邮箱提示</span><input className={fieldClass} type="email" value={form.oauth_email_hint} onChange={event => set('oauth_email_hint', event.target.value)} /></label></> : null}
              <label className="space-y-1.5 text-sm text-[var(--text-secondary)]"><span>固定邮箱（仅单账号调试）</span><input className={fieldClass} type="email" value={form.email} onChange={event => set('email', event.target.value)} /></label>
              <label className="space-y-1.5 text-sm text-[var(--text-secondary)]"><span>固定密码（可选）</span><input className={fieldClass} type="password" value={form.password} onChange={event => set('password', event.target.value)} /></label>
              <label className="space-y-1.5 text-sm text-[var(--text-secondary)]"><span>邮箱验证码超时（秒）</span><input className={fieldClass} type="number" min={10} max={900} value={form.otp_timeout} onChange={event => set('otp_timeout', Number(event.target.value))} /></label>
            </div>
            {needsMailbox && form.mail_provider ? <div className={`mt-4 rounded-lg border p-3 text-xs ${selectedMailbox?.configured === false ? 'border-amber-500/30 bg-amber-500/10 text-amber-200' : 'border-[var(--border)] bg-[var(--chip-bg)] text-[var(--text-muted)]'}`}><div>{selectedMailbox?.status_message || '服务端读取已保存的邮箱配置，凭据不会回填到任务表单。'}</div>{selectedMailbox?.available_count != null ? <div className="mt-1">可用库存：{selectedMailbox.available_count}{selectedMailbox.total_count != null ? ` / ${selectedMailbox.total_count}` : ''}</div> : null}{selectedMailbox?.missing_fields?.length ? <div className="mt-1">缺少：{selectedMailbox.missing_fields.join('、')}</div> : null}</div> : null}
            {needsOAuth ? <div className="mt-4 grid gap-4 md:grid-cols-2"><label className="space-y-1.5 text-sm text-[var(--text-secondary)]"><span>Chrome 用户目录（可选）</span><input className={fieldClass} value={form.chrome_user_data_dir} onChange={event => set('chrome_user_data_dir', event.target.value)} /></label><label className="space-y-1.5 text-sm text-[var(--text-secondary)]"><span>Chrome CDP 地址（可选）</span><input className={fieldClass} value={form.chrome_cdp_url} onChange={event => set('chrome_cdp_url', event.target.value)} placeholder="http://127.0.0.1:9222" /></label></div> : null}
            {isSmsBower ? <SmsBowerPriceSelector value={smsBowerSelection} onChange={applySmsBowerSelection} queryProxy={form.proxy} credentialsConfigured={selectedSmsProvider?.configured === true} /> : null}
            {isChatgptFree && form.sms_provider && !isSmsBower ? <div className="mt-4 grid gap-4 rounded-xl border border-cyan-400/20 bg-cyan-400/[0.04] p-4 md:grid-cols-2"><label className="space-y-1.5 text-sm text-[var(--text-secondary)]"><span>接码国家</span><input className={fieldClass} value={form.sms_country} onChange={event => set('sms_country', event.target.value)} placeholder="US / 187" /></label><label className="space-y-1.5 text-sm text-[var(--text-secondary)]"><span>服务代码</span><input className={fieldClass} value={form.sms_service} onChange={event => set('sms_service', event.target.value)} placeholder="dr" /></label><label className="space-y-1.5 text-sm text-[var(--text-secondary)]"><span>单号最高价格</span><input className={fieldClass} value={form.sms_max_price} onChange={event => set('sms_max_price', event.target.value)} placeholder="不限" inputMode="decimal" /></label><label className="space-y-1.5 text-sm text-[var(--text-secondary)]"><span>单账号换号上限</span><input className={fieldClass} type="number" min={1} max={20} value={form.sms_phone_max_attempts} onChange={event => set('sms_phone_max_attempts', Number(event.target.value))} /></label></div> : null}
            {isChatgptFree ? <div className="mt-4 rounded-lg border border-violet-500/25 bg-violet-500/5 p-3 text-xs text-[var(--text-muted)]"><div className="font-medium text-violet-200">ChatGPT Free 专用验证</div><div className="mt-1">邮箱注册完成后自动租用短信号码完成手机验证，并继续获取 Codex AT/RT；不会进入 aBai ChatGPT 任务链。</div>{form.sms_provider ? <div className="mt-2">{options.sms_settings.find(item => item.provider_key === form.sms_provider)?.status_message || '服务端读取已保存的短信配置。'} <Link className="ml-2 text-sky-400" to="/settings?tab=sms">配置短信服务</Link></div> : <Link className="mt-2 inline-block text-sky-400" to="/settings?tab=sms">配置短信服务</Link>}</div> : null}
          </Card>

          <Card>
            <SectionTitle number="03" title="执行器与网络线路" description="手动代理优先；留空时可从代理池分配，也可明确选择云服务器直连。" />
            <div className="grid gap-4 md:grid-cols-2">
              <label className="space-y-1.5 text-sm text-[var(--text-secondary)]"><span>执行器</span><select className={fieldClass} value={form.executor_type} onChange={event => set('executor_type', event.target.value)}>{(platform?.supported_executors || []).map(item => <option key={item} value={item}>{executorLabels[item] || item}</option>)}</select></label>
              <label className="space-y-1.5 text-sm text-[var(--text-secondary)]"><span>验证码服务</span><select className={fieldClass} value={form.captcha_solver} onChange={event => set('captcha_solver', event.target.value)}><option value="auto">自动选择</option>{captchas.map(item => <option key={item.provider_key} value={item.provider_key}>{item.display_name || item.catalog_label || item.provider_key}</option>)}</select></label>
              <label className="space-y-1.5 text-sm text-[var(--text-secondary)]"><span>代理策略</span><select className={fieldClass} value={form.proxy_strategy} onChange={event => set('proxy_strategy', event.target.value)}><option value="auto">自动：Mihomo → 手动池 → 直连</option><option value="mihomo">仅 Mihomo（不可用则停止）</option><option value="polling">仅手动代理池（无代理则停止）</option><option value="direct">明确直连</option></select></label>
              <label className="space-y-1.5 text-sm text-[var(--text-secondary)]"><span>手动代理覆盖（可选）</span><input className={fieldClass} placeholder="http://user:pass@host:port" value={form.proxy} onChange={event => set('proxy', event.target.value)} /></label>
            </div>
            <div className="mt-4 grid gap-3 md:grid-cols-2"><div className="rounded-lg border border-[var(--border)] bg-[var(--chip-bg)] p-3 text-xs text-[var(--text-muted)]"><div className="font-medium text-[var(--text-secondary)]">Mihomo 订阅池</div><div className="mt-1">{proxyStatus.mihomo.configured ? `${proxyStatus.mihomo.nodes} 个可用节点` : '未配置'}{proxyStatus.mihomo.error ? ` · ${proxyStatus.mihomo.error}` : ''}</div><Link className="mt-2 inline-block text-sky-400" to="/settings?tab=proxy-pool">配置 Mihomo</Link></div><div className="rounded-lg border border-[var(--border)] bg-[var(--chip-bg)] p-3 text-xs text-[var(--text-muted)]"><div className="font-medium text-[var(--text-secondary)]">手动代理池</div><div className="mt-1">启用 {proxyStatus.manual_pool.active} / 总计 {proxyStatus.manual_pool.total}</div><Link className="mt-2 inline-block text-sky-400" to="/proxies">管理手动代理</Link></div></div>
          </Card>

          <Card>
            <SectionTitle number="04" title="失败策略" description="控制单个目标失败后的处理方式；重试会重新分配邮箱与代理。" />
            <div className="grid gap-4 md:grid-cols-2">
              <label className="space-y-1.5 text-sm text-[var(--text-secondary)]"><span>失败处理</span><select className={fieldClass} value={form.failure_policy} onChange={event => setForm(current => ({ ...current, failure_policy: event.target.value, max_attempts_per_account: event.target.value === 'retry_then_continue' ? (current.platform === 'chatgpt_free' ? 5 : Math.max(current.max_attempts_per_account, 2)) : 1 }))}><option value="retry_then_continue">重试后继续</option><option value="continue">记录失败并继续</option><option value="stop_on_failure">首次失败后停止投放</option></select></label>
              <label className="space-y-1.5 text-sm text-[var(--text-secondary)]"><span>每个目标最多尝试</span><input className={fieldClass} type="number" min={isChatgptFree ? 5 : 1} max={5} value={form.max_attempts_per_account} disabled={form.failure_policy !== 'retry_then_continue' || isChatgptFree} onChange={event => set('max_attempts_per_account', Number(event.target.value))} />{isChatgptFree && form.failure_policy === 'retry_then_continue' ? <span className="block text-xs leading-5 text-[var(--text-muted)]">Free 流程固定 5 次预算；线路、邮箱验证码或短信资源失败时自动换资源。</span> : null}</label>
            </div>
            <label className="mt-4 flex items-start gap-3 rounded-lg border border-[var(--border)] bg-[var(--chip-bg)] p-3 text-sm text-[var(--text-secondary)]"><input className="mt-1" type="checkbox" checked={form.verify_after_registration} onChange={event => set('verify_after_registration', event.target.checked)} /><span><span className="block font-medium text-[var(--text-primary)]">注册后立即验证账号</span><span className="mt-1 block text-xs text-[var(--text-muted)]">开启后，平台状态检查未通过的账号不会入库；若平台接口偶发误判，可关闭后只以注册返回结果为准。</span></span></label>
          </Card>
        </div>

        <Card className="h-fit xl:sticky xl:top-6">
          <div className="mb-4 flex items-center gap-2 font-medium text-[var(--text-primary)]"><KeyRound className="h-4 w-4 text-violet-400" />启动检查</div>
          <dl className="space-y-3 text-sm"><div className="flex justify-between gap-3"><dt className="text-[var(--text-muted)]">平台</dt><dd>{platform?.display_name || '-'}</dd></div><div className="flex justify-between gap-3"><dt className="text-[var(--text-muted)]">验证</dt><dd className="text-right">{platform?.verification || '-'}</dd></div><div className="flex justify-between gap-3"><dt className="text-[var(--text-muted)]">注册方式</dt><dd>{needsOAuth ? 'OAuth · ' + (oauthLabels[form.oauth_provider] || form.oauth_provider) : '系统邮箱'}</dd></div><div className="flex justify-between gap-3"><dt className="text-[var(--text-muted)]">执行器</dt><dd>{executorLabels[form.executor_type] || form.executor_type || '-'}</dd></div><div className="flex justify-between gap-3"><dt className="text-[var(--text-muted)]">线路</dt><dd>{form.proxy ? '手动代理' : form.proxy_strategy}</dd></div><div className="flex justify-between gap-3"><dt className="text-[var(--text-muted)]">规模</dt><dd>{form.count} / 并发 {form.concurrency}</dd></div></dl>
          <div className="mt-4 flex items-start gap-2 rounded-lg border border-sky-500/20 bg-sky-500/5 p-3 text-xs text-sky-200"><Radio className="mt-0.5 h-3.5 w-3.5 shrink-0" /><span>任务类型固定为 <code>legacy_register</code>，不会调用 aBai ChatGPT 注册器。</span></div>
          {blocking.length ? <div className="mt-4 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200">{blocking.map(item => <div key={item}>• {item}</div>)}</div> : null}
          {error ? <div className="mt-4 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-xs text-red-200">{error}</div> : null}
          <Button className="mt-5 w-full" onClick={() => void submit()} disabled={submitting || blocking.length > 0}><Play className="mr-2 h-4 w-4" />{submitting ? '正在创建…' : '启动其他平台注册'}</Button>
        </Card>
      </div>

      {taskId ? <Card><div className="mb-4 flex items-center justify-between"><div><div className="font-medium text-[var(--text-primary)]">05 · 运行与结果</div><div className="mt-1 text-xs text-[var(--text-muted)]">页面关闭后任务仍会运行，可从任务记录重新查看。</div></div><Route className="h-5 w-5 text-sky-400" /></div>{taskResult ? <div className="mb-4 grid gap-3 sm:grid-cols-4"><div className="rounded-lg border border-[var(--border)] p-3"><div className="text-xs text-[var(--text-muted)]">成功目标</div><div className="mt-1 text-lg font-semibold">{taskResult.target_count ?? '-'}</div></div><div className="rounded-lg border border-emerald-500/25 bg-emerald-500/5 p-3"><div className="text-xs text-[var(--text-muted)]">实际成功</div><div className="mt-1 text-lg font-semibold text-emerald-300">{taskResult.success_count ?? 0}</div></div><div className="rounded-lg border border-red-500/25 bg-red-500/5 p-3"><div className="text-xs text-[var(--text-muted)]">失败尝试</div><div className="mt-1 text-lg font-semibold text-red-300">{taskResult.failed_attempts ?? 0}</div></div><div className="rounded-lg border border-[var(--border)] p-3"><div className="text-xs text-[var(--text-muted)]">已投放尝试</div><div className="mt-1 text-lg font-semibold">{taskResult.attempts_submitted ?? 0}</div></div></div> : null}<div className="min-h-[520px]"><TaskLogPanel taskId={taskId} onDone={() => void loadTaskResult()} /></div></Card> : null}
    </div>
  )
}
