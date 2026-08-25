import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  AlertTriangle,
  ArrowRight,
  Check,
  CheckCircle,
  ChevronDown,
  CircleDot,
  Cpu,
  Database,
  Gauge,
  History,
  Layers3,
  Loader2,
  Mail,
  MailCheck,
  MapPin,
  Orbit,
  Play,
  Radio,
  RefreshCw,
  ScanText,
  Server,
  Settings2,
  ShieldCheck,
  Smartphone,
  Users,
  Workflow,
  XCircle,
} from 'lucide-react'

import { TaskLogPanel } from '@/components/tasks/TaskLogPanel'
import {
  SmsBowerPriceSelector,
  type SmsBowerSelectionValue,
} from '@/components/registration/SmsBowerPriceSelector'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { getConfig, getConfigOptions, getPlatforms } from '@/lib/app-data'
import type {
  ConfigOptionsResponse,
  ProviderField,
  ProviderSetting,
} from '@/lib/config-options'
import {
  getCaptchaStrategyLabel,
  getProviderSelectOptions,
  listProviderFieldKeys,
} from '@/lib/config-options'
import { useI18n } from '@/lib/i18n-context'
import {
  buildExecutorOptions,
  buildRegistrationOptions,
  hasReusableOAuthBrowser,
  pickOAuthExecutor,
} from '@/lib/registration'
import { normalizeFreeSub2Model, SUB2_FREE_MODEL_OPTIONS } from '@/lib/sub2api-models'
import { getTaskStatusText, isTerminalTaskStatus, TASK_STATUS_VARIANTS } from '@/lib/tasks'
import { apiFetch } from '@/lib/utils'

type SelectOption = readonly [string | number, string]

type MailboxPoolInventory = {
  total_count: number
  available_count: number
  used_count: number
  blocked_count: number
  allow_reuse: boolean
  truncated: boolean
}

const EMPTY_CONFIG_OPTIONS: ConfigOptionsResponse = {
  mailbox_providers: [],
  captcha_providers: [],
  sms_providers: [],
  mailbox_settings: [],
  captcha_settings: [],
  sms_settings: [],
  captcha_policy: {},
  executor_options: [],
  identity_mode_options: [],
  oauth_provider_options: [],
}

const DEFAULT_FORM: Record<string, any> = {
  platform: 'chatgpt',
  email: '',
  password: '',
  count: 1,
  concurrency: 1,
  proxy: '',
  executor_type: 'headless',
  captcha_solver: 'auto',
  identity_provider: 'mailbox',
  oauth_provider: '',
  oauth_email_hint: '',
  chrome_user_data_dir: '',
  chrome_cdp_url: '',
  mail_provider: '',
  sms_provider: '',
  require_phone_verification: false,
  phone_bind_email_after_registration: true,
  email_otp_timeout_seconds: 300,
  sub2api_auto_sync: false,
  sub2api_proxy_id: 0,
  sub2api_agent_identity_region: 'CO',
  sub2api_default_model: '',
  proxy_strategy: 'auto',
  failure_policy: 'retry_then_continue',
  complete_started_attempts: false,
  post_registration_liveness_delay_seconds: 60,
  post_registration_probation_enabled: true,
  post_registration_probation_interval_seconds: 60,
  network_circuit_break_threshold: 3,
  sms_country: '',
  sms_countries: '',
  sms_max_price: '',
  sms_bulk_price_cny: '',
  smsbower_provider_ids_by_country: {},
  smsbower_auto_country_min_stock: 1,
  sms_usd_cny_rate: 7.2,
  smsbower_provider_reject_threshold: 2,
  sms_code_timeout_seconds: 180,
  sms_phone_max_attempts: 8,
  sms_no_numbers_wait_seconds: 120,
  sms_tier_cooldown_minutes: 45,
}

const SMSBOWER_MANAGED_FIELD_KEYS = new Set([
  'smsbower_default_service',
  'smsbower_default_country',
  'smsbower_max_price',
  'smsbower_auto_country',
])

const SMS_COUNTRY_PROXY_REGIONS: Record<string, { code: string; label: string }> = {
  '187': { code: 'US', label: '美国' },
  '16': { code: 'GB', label: '英国' },
  '33': { code: 'CO', label: '哥伦比亚' },
  '151': { code: 'CL', label: '智利' },
  '10': { code: 'VN', label: '越南' },
  '73': { code: 'BR', label: '巴西' },
  '22': { code: 'IN', label: '印度' },
  '6': { code: 'ID', label: '印度尼西亚' },
  '4': { code: 'PH', label: '菲律宾' },
  '52': { code: 'TH', label: '泰国' },
  '0': { code: 'RU', label: '俄罗斯' },
}

const REGION_OPTIONS: SelectOption[] = [
  ['CO', 'CO · 哥伦比亚'],
  ['US', 'US · 美国'],
  ['GB', 'GB · 英国'],
  ['CL', 'CL · 智利'],
  ['BR', 'BR · 巴西'],
  ['IN', 'IN · 印度'],
  ['ID', 'ID · 印度尼西亚'],
  ['PH', 'PH · 菲律宾'],
  ['TH', 'TH · 泰国'],
  ['VN', 'VN · 越南'],
]

const PROXY_STRATEGY_OPTIONS = [
  {
    value: 'auto',
    label: '智能分配',
    tag: '推荐',
    icon: Orbit,
    summary: '按号码国家优先找匹配代理',
    description: '有可用代理池时优先使用；没有手动代理时由启动预检决定是否直连。',
  },
  {
    value: 'polling',
    label: '池内分散',
    tag: '并发场景',
    icon: RefreshCw,
    summary: '每个新尝试重新分配一条代理',
    description: '按最少占用、最久未使用优先，尽量让并发任务分散到不同代理条目。',
  },
  {
    value: 'direct',
    label: '直连',
    tag: '仅调试',
    icon: ArrowRight,
    summary: '不使用代理池',
    description: '直接从云服务器访问目标服务，手机号国家无法跟随出口地区。',
  },
] as const

function FieldInput({
  label,
  value,
  onChange,
  type = 'text',
  placeholder = '',
  min,
  max,
  hint,
}: {
  label: string
  value: string | number
  onChange: (value: string | number) => void
  type?: 'text' | 'password' | 'number'
  placeholder?: string
  min?: number
  max?: number
  hint?: string
}) {
  return (
    <label className="block min-w-0">
      <span className="mb-1.5 block text-xs font-medium text-[var(--text-secondary)]">{label}</span>
      <input
        type={type}
        value={value}
        min={min}
        max={max}
        onChange={(event) => onChange(type === 'number' ? Number(event.target.value) : event.target.value)}
        placeholder={placeholder}
        className="control-surface"
      />
      {hint ? <span className="mt-1.5 block text-[11px] leading-4 text-[var(--text-muted)]">{hint}</span> : null}
    </label>
  )
}

function FieldSelect({
  label,
  value,
  onChange,
  options,
  hint,
}: {
  label: string
  value: string | number
  onChange: (value: string) => void
  options: readonly SelectOption[]
  hint?: string
}) {
  return (
    <label className="block min-w-0">
      <span className="mb-1.5 block text-xs font-medium text-[var(--text-secondary)]">{label}</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="control-surface appearance-none"
      >
        {options.map(([optionValue, optionLabel]) => (
          <option key={String(optionValue)} value={optionValue}>{optionLabel}</option>
        ))}
      </select>
      {hint ? <span className="mt-1.5 block text-[11px] leading-4 text-[var(--text-muted)]">{hint}</span> : null}
    </label>
  )
}

function ToggleRow({
  label,
  description,
  checked,
  onChange,
  disabled = false,
}: {
  label: string
  description?: string
  checked: boolean
  onChange: (checked: boolean) => void
  disabled?: boolean
}) {
  return (
    <label className={`flex min-h-12 items-center justify-between gap-4 rounded-xl border border-[var(--border-soft)] bg-[var(--chip-bg)] px-4 py-3 transition-colors ${disabled ? 'cursor-not-allowed opacity-65' : 'cursor-pointer hover:border-[var(--accent-edge)]'}`}>
      <span className="min-w-0">
        <span className="block text-sm font-medium text-[var(--text-primary)]">{label}</span>
        {description ? <span className="mt-0.5 block text-[11px] leading-4 text-[var(--text-muted)]">{description}</span> : null}
      </span>
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        disabled={disabled}
        className="checkbox-accent h-4 w-4 shrink-0"
      />
    </label>
  )
}

function ProviderFieldControl({
  field,
  value,
  onChange,
}: {
  field: ProviderField
  value: any
  onChange: (value: any) => void
}) {
  if (field.type === 'toggle') {
    const checked = value === true || String(value || '').toLowerCase() === 'true'
    return <ToggleRow label={field.label} description={field.hint} checked={checked} onChange={onChange} />
  }
  if (field.type === 'select' && field.options?.length) {
    return (
      <FieldSelect
        label={field.label}
        value={String(value ?? '')}
        onChange={onChange}
        options={field.options.map(option => [option.value, option.label] as const)}
        hint={field.hint}
      />
    )
  }
  if (field.type === 'textarea') {
    return (
      <label className="block min-w-0 md:col-span-2">
        <span className="mb-1.5 block text-xs font-medium text-[var(--text-secondary)]">{field.label}</span>
        <textarea
          value={String(value ?? '')}
          onChange={(event) => onChange(event.target.value)}
          placeholder={field.placeholder || ''}
          className="control-surface min-h-24 resize-y"
        />
        {field.hint ? <span className="mt-1.5 block text-[11px] leading-4 text-[var(--text-muted)]">{field.hint}</span> : null}
      </label>
    )
  }
  return (
    <FieldInput
      label={field.label}
      value={String(value ?? '')}
      onChange={onChange}
      type={field.secret ? 'password' : 'text'}
      placeholder={field.placeholder || ''}
      hint={field.hint}
    />
  )
}

function SectionHeading({
  number,
  title,
  description,
  icon: Icon,
}: {
  number: string
  title: string
  description: string
  icon: typeof Workflow
}) {
  return (
    <div className="mb-5 flex items-start gap-3">
      <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl border border-[var(--accent-edge)] bg-[var(--accent-soft)] text-[var(--text-accent)]">
        <Icon className="h-4.5 w-4.5" />
      </div>
      <div className="min-w-0">
        <div className="text-[10px] font-bold uppercase tracking-[0.2em] text-[var(--text-muted)]">STEP {number}</div>
        <h2 className="mt-0.5 text-base font-semibold tracking-[-0.02em] text-[var(--text-primary)]">{title}</h2>
        <p className="mt-1 text-xs leading-5 text-[var(--text-muted)]">{description}</p>
      </div>
    </div>
  )
}

function selectedSmsCountry(form: Record<string, any>) {
  const value = [
    form.sms_country,
    form.smsbower_default_country,
    form.herosms_country,
    form.herosms_default_country,
    form.sms_activate_default_country,
  ].find(item => String(item || '').trim())
  return String(value || '').trim()
}

function normalizeSmsCountryIds(value: unknown, fallback = '') {
  const raw = Array.isArray(value) ? value : String(value || '').split(/[\s,;]+/)
  const countries = Array.from(new Set(raw.map(item => String(item || '').trim()).filter(Boolean)))
  const normalizedFallback = String(fallback || '').trim()
  if (countries.length === 0 && normalizedFallback) countries.push(normalizedFallback)
  return countries
}

function numberOr(value: unknown, fallback: number) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

function infer711ProxyRegion(proxy: string) {
  const match = String(proxy || '').match(/(?:^|-)region-([a-z]{2})(?:-|:|@)/i)
  return match?.[1]?.toUpperCase() || ''
}

function getProviderSetting(settings: ProviderSetting[] = [], providerKey: string) {
  return settings.find(item => item.provider_key === providerKey) || null
}

function getProviderMergedValues(setting: ProviderSetting | null) {
  return { ...(setting?.config || {}), ...(setting?.auth || {}) }
}

function getDefaultProviderKey(settings: ProviderSetting[] = []) {
  return settings.find(item => item.is_default)?.provider_key || settings[0]?.provider_key || ''
}

function asBoolean(value: unknown, fallback = false) {
  if (value === undefined || value === null || value === '') return fallback
  return String(value).toLowerCase() === 'true' || value === true
}

export default function RegisterWorkbench() {
  const { t, language } = useI18n()
  const [searchParams] = useSearchParams()
  const requestedPlatform = String(searchParams.get('platform') || '').trim()
  const fromAccounts = searchParams.get('from') === 'accounts'
  const [form, setForm] = useState<Record<string, any>>(DEFAULT_FORM)
  const [platforms, setPlatforms] = useState<any[]>([])
  const [configOptions, setConfigOptions] = useState<ConfigOptionsResponse>(EMPTY_CONFIG_OPTIONS)
  const [loadingOptions, setLoadingOptions] = useState(true)
  const [optionsError, setOptionsError] = useState('')
  const [task, setTask] = useState<any>(null)
  const [polling, setPolling] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState('')
  const [mailboxInventory, setMailboxInventory] = useState<MailboxPoolInventory | null>(null)
  const [mailboxInventoryLoading, setMailboxInventoryLoading] = useState(false)
  const [mailboxInventoryError, setMailboxInventoryError] = useState('')
  const [mailboxInventoryUpdatedAt, setMailboxInventoryUpdatedAt] = useState<Date | null>(null)
  const handledTerminalTaskIdsRef = useRef<Set<string>>(new Set())
  const openedCashierTaskIdsRef = useRef<Set<string>>(new Set())
  const mailboxInventoryRequestRef = useRef(0)

  const set = useCallback((key: string, value: any) => {
    setForm(current => ({ ...current, [key]: value }))
  }, [])

  const applyTerminalTask = useCallback((latest: any, statusHint?: string) => {
    const resolved = statusHint && !latest?.status ? { ...latest, status: statusHint } : latest
    setTask(resolved)
    const taskKey = String(resolved?.task_id || resolved?.id || '')
    if (!taskKey) return
    handledTerminalTaskIdsRef.current.add(taskKey)
    if (
      (statusHint || resolved?.status) === 'succeeded'
      && Array.isArray(resolved?.cashier_urls)
      && resolved.cashier_urls.length > 0
      && !openedCashierTaskIdsRef.current.has(taskKey)
    ) {
      openedCashierTaskIdsRef.current.add(taskKey)
      resolved.cashier_urls.forEach((url: string) => window.open(url, '_blank'))
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    setLoadingOptions(true)
    Promise.all([getConfig(), getPlatforms(), getConfigOptions()])
      .then(([cfg, platformResult, optionResult]) => {
        if (cancelled) return
        const loadedPlatforms = Array.isArray(platformResult) ? platformResult : []
        const loadedOptions = optionResult || EMPTY_CONFIG_OPTIONS
        setPlatforms(loadedPlatforms)
        setConfigOptions(loadedOptions)
        setOptionsError(loadedPlatforms.length === 0 ? '平台列表为空，请刷新页面或重新登录。' : '')
        setForm(current => {
          const configuredPlatform = loadedPlatforms.some((item: any) => item.name === requestedPlatform)
            ? requestedPlatform
            : loadedPlatforms.some((item: any) => item.name === current.platform)
              ? current.platform
              : loadedPlatforms.find((item: any) => item.name === 'chatgpt')?.name
                || loadedPlatforms[0]?.name
                || current.platform
          const next: Record<string, any> = {
            ...current,
            platform: configuredPlatform,
            executor_type: cfg.default_executor || current.executor_type,
            identity_provider: cfg.default_identity_provider || current.identity_provider || 'mailbox',
            oauth_provider: cfg.default_oauth_provider || current.oauth_provider,
            oauth_email_hint: cfg.oauth_email_hint || current.oauth_email_hint,
            chrome_user_data_dir: cfg.chrome_user_data_dir || current.chrome_user_data_dir,
            chrome_cdp_url: cfg.chrome_cdp_url || current.chrome_cdp_url,
            mail_provider: getDefaultProviderKey(loadedOptions.mailbox_settings || []) || current.mail_provider,
            sms_provider: getDefaultProviderKey(loadedOptions.sms_settings || []) || current.sms_provider,
            require_phone_verification: asBoolean(
              cfg.require_phone_verification,
              current.require_phone_verification,
            ),
            sub2api_auto_sync: asBoolean(cfg.sub2api_auto_sync, current.sub2api_auto_sync),
            sub2api_proxy_id: Number(cfg.sub2api_proxy_id ?? current.sub2api_proxy_id ?? 0),
            sub2api_agent_identity_region: String(cfg.sub2api_agent_identity_region || current.sub2api_agent_identity_region || 'CO').toUpperCase(),
            sub2api_default_model: Object.prototype.hasOwnProperty.call(cfg, 'sub2api_default_model')
              ? normalizeFreeSub2Model(cfg.sub2api_default_model)
              : normalizeFreeSub2Model(current.sub2api_default_model),
          }
          listProviderFieldKeys([
            ...(loadedOptions.mailbox_providers || []),
            ...(loadedOptions.captcha_providers || []),
            ...(loadedOptions.sms_providers || []),
          ]).forEach(fieldKey => {
            next[fieldKey] = cfg[fieldKey] ?? current[fieldKey] ?? ''
          })
          return next
        })
      })
      .catch((error) => {
        if (cancelled) return
        setConfigOptions(EMPTY_CONFIG_OPTIONS)
        setOptionsError(`注册配置加载失败：${error instanceof Error ? error.message : String(error || 'unknown error')}`)
      })
      .finally(() => {
        if (!cancelled) setLoadingOptions(false)
      })
    return () => {
      cancelled = true
    }
  }, [requestedPlatform])

  const currentPlatform = useMemo(
    () => platforms.find((platform: any) => platform.name === form.platform) || null,
    [form.platform, platforms],
  )
  const platformOptions = useMemo<SelectOption[]>(
    () => platforms.map((platform: any) => [platform.name, platform.display_name] as const),
    [platforms],
  )
  const supportedExecutors = useMemo<string[]>(
    () => currentPlatform?.supported_executors || [],
    [currentPlatform],
  )
  const registrationOptions = useMemo(
    () => buildRegistrationOptions(currentPlatform, language),
    [currentPlatform, language],
  )
  const registrationFlowOptions = useMemo(() => registrationOptions.flatMap((option) => {
    if (form.platform === 'chatgpt' && option.identityProvider === 'mailbox') {
      return [
        {
          ...option,
          key: `${option.key}:email`,
          label: '系统邮箱',
          description: '只使用系统邮箱和邮箱验证码完成注册，不租用手机号',
          requiresPhoneVerification: false as boolean | null,
        },
        {
          ...option,
          key: `${option.key}:email_then_phone`,
          label: '邮箱 + 手机验证',
          description: '先用系统邮箱注册，再接码完成手机验证并获取 Codex RT',
          requiresPhoneVerification: true as boolean | null,
        },
      ]
    }
    if (form.platform === 'chatgpt' && option.identityProvider === 'phone') {
      return [{
        ...option,
        label: '手机号注册',
        description: '先接码完成手机号注册，可在完成后继续绑定邮箱',
        requiresPhoneVerification: null as boolean | null,
      }]
    }
    return [{ ...option, requiresPhoneVerification: null as boolean | null }]
  }), [form.platform, registrationOptions])
  const reusableOAuthBrowser = hasReusableOAuthBrowser({
    chrome_user_data_dir: form.chrome_user_data_dir,
    chrome_cdp_url: form.chrome_cdp_url,
  })
  const executorOptions = useMemo(
    () => buildExecutorOptions(
      form.identity_provider,
      supportedExecutors,
      reusableOAuthBrowser,
      currentPlatform?.supported_executor_options || [],
      language,
    ),
    [
      currentPlatform?.supported_executor_options,
      form.identity_provider,
      language,
      reusableOAuthBrowser,
      supportedExecutors,
    ],
  )
  const mailboxProviderOptions = useMemo<SelectOption[]>(
    () => getProviderSelectOptions(configOptions.mailbox_providers || []),
    [configOptions.mailbox_providers],
  )
  const smsProviderOptions = useMemo<SelectOption[]>(
    () => getProviderSelectOptions(configOptions.sms_providers || []),
    [configOptions.sms_providers],
  )
  const currentMailboxProvider = (configOptions.mailbox_providers || [])
    .find(provider => provider.value === form.mail_provider) || null
  const currentMailboxSetting = getProviderSetting(configOptions.mailbox_settings || [], form.mail_provider)
  const currentSmsProvider = (configOptions.sms_providers || [])
    .find(provider => provider.value === form.sms_provider) || null
  const currentSmsSetting = getProviderSetting(configOptions.sms_settings || [], form.sms_provider)
  const isSmsBower = form.sms_provider === 'smsbower_api' || form.sms_provider === 'smsbower'
  const allProviderFieldKeys = useMemo(
    () => listProviderFieldKeys([
      ...(configOptions.mailbox_providers || []),
      ...(configOptions.captcha_providers || []),
      ...(configOptions.sms_providers || []),
    ]),
    [configOptions.captcha_providers, configOptions.mailbox_providers, configOptions.sms_providers],
  )

  useEffect(() => {
    const defaultProvider = getDefaultProviderKey(configOptions.mailbox_settings || [])
    if (['mailbox', 'phone'].includes(form.identity_provider) && !form.mail_provider && defaultProvider) {
      set('mail_provider', defaultProvider)
    }
  }, [configOptions.mailbox_settings, form.identity_provider, form.mail_provider, set])

  useEffect(() => {
    if (!currentMailboxProvider) return
    const values = getProviderMergedValues(currentMailboxSetting)
    setForm(current => {
      let changed = false
      const next = { ...current }
      ;(currentMailboxProvider.fields || []).forEach(field => {
        const nextValue = values[field.key] ?? current[field.key] ?? ''
        if ((next[field.key] ?? '') !== nextValue) {
          next[field.key] = nextValue
          changed = true
        }
      })
      return changed ? next : current
    })
  }, [currentMailboxProvider, currentMailboxSetting, form.mail_provider])

  useEffect(() => {
    const defaultProvider = getDefaultProviderKey(configOptions.sms_settings || [])
    if (!form.sms_provider && defaultProvider) set('sms_provider', defaultProvider)
  }, [configOptions.sms_settings, form.sms_provider, set])

  useEffect(() => {
    if (!currentSmsProvider) return
    const values = getProviderMergedValues(currentSmsSetting)
    setForm(current => {
      let changed = false
      const next = { ...current }
      ;(currentSmsProvider.fields || []).forEach(field => {
        const nextValue = values[field.key] ?? current[field.key] ?? ''
        if ((next[field.key] ?? '') !== nextValue) {
          next[field.key] = nextValue
          changed = true
        }
      })
      if (currentSmsProvider.value === 'smsbower_api' || currentSmsProvider.value === 'smsbower') {
        const configuredCountry = String(
          values.sms_country
          || values.smsbower_country
          || values.smsbower_default_country
          || '',
        ).trim()
        const configuredMaxPrice = String(
          values.sms_max_price
          || values.smsbower_max_price
          || '',
        ).trim()
        if (!String(current.sms_country || '').trim()) {
          const initialCountry = configuredCountry || '187'
          next.sms_country = initialCountry
          next.sms_countries = initialCountry
          changed = true
        }
        if (!String(current.sms_max_price || '').trim() && configuredMaxPrice) {
          const normalizedPrice = configuredMaxPrice === '-1' ? '0' : configuredMaxPrice
          next.sms_max_price = normalizedPrice
          next.smsbower_max_price = normalizedPrice
          changed = true
        }
      }
      return changed ? next : current
    })
  }, [currentSmsProvider, currentSmsSetting, form.sms_provider])

  useEffect(() => {
    if (platforms.length === 0 || platforms.some((platform: any) => platform.name === form.platform)) return
    set('platform', platforms.find((platform: any) => platform.name === 'chatgpt')?.name || platforms[0]?.name)
  }, [form.platform, platforms, set])

  useEffect(() => {
    if (registrationOptions.length === 0) return
    const selected = registrationOptions.find(option => (
      option.identityProvider === form.identity_provider && option.oauthProvider === form.oauth_provider
    ))
    if (selected) return
    const preferred = registrationOptions.find(option => option.identityProvider === form.identity_provider)
      || registrationOptions[0]
    setForm(current => ({
      ...current,
      identity_provider: preferred.identityProvider,
      oauth_provider: preferred.oauthProvider,
    }))
  }, [form.identity_provider, form.oauth_provider, registrationOptions])

  useEffect(() => {
    const validExecutors = executorOptions.filter(option => !option.disabled)
    if (validExecutors.length === 0 || validExecutors.some(option => option.value === form.executor_type)) return
    const preferred = form.identity_provider === 'oauth_browser'
      ? pickOAuthExecutor(supportedExecutors, form.executor_type, reusableOAuthBrowser)
      : supportedExecutors.includes(form.executor_type)
        ? form.executor_type
        : supportedExecutors[0] || ''
    set('executor_type', validExecutors.find(option => option.value === preferred)?.value || validExecutors[0].value)
  }, [
    executorOptions,
    form.executor_type,
    form.identity_provider,
    reusableOAuthBrowser,
    set,
    supportedExecutors,
  ])

  const needsMailbox = ['mailbox', 'phone'].includes(form.identity_provider)
  const supportsMailboxInventory = currentMailboxProvider?.driver_type === 'local_ms_pool'
    || ['local_ms_pool', 'local_ms', 'local_gmail_pool'].includes(String(form.mail_provider || ''))
  const needsSms = form.identity_provider === 'phone' || Boolean(
    form.platform === 'chatgpt'
    && form.identity_provider === 'mailbox'
    && (form.require_phone_verification || form.sub2api_auto_sync),
  )
  const smsCountries = useMemo(
    () => normalizeSmsCountryIds(form.sms_countries, selectedSmsCountry(form)),
    [form],
  )
  const smsBowerProviderIdsByCountry = useMemo(() => {
    const raw = form.smsbower_provider_ids_by_country
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
    return Object.fromEntries(
      Object.entries(raw)
        .map(([country, ids]) => [
          String(country).trim(),
          Array.from(new Set(
            (Array.isArray(ids) ? ids : String(ids || '').split(/[\s,;]+/))
              .map(item => String(item || '').trim())
              .filter(Boolean),
          )),
        ] as const)
        .filter(([country, ids]) => country && ids.length > 0),
    )
  }, [form.smsbower_provider_ids_by_country])
  const smsBowerCredentialsConfigured = Boolean(
    Object.values(currentSmsSetting?.auth_preview || {}).some(value => String(value || '').trim())
    || Object.values(currentSmsSetting?.auth || {}).some(value => String(value || '').trim()),
  )
  const smsBowerSelection: SmsBowerSelectionValue = {
    country: selectedSmsCountry(form) || smsCountries[0] || '187',
    countries: smsCountries,
    maxPriceUsd: String(form.sms_max_price || form.smsbower_max_price || '0.13'),
    bulkPriceCny: String(form.sms_bulk_price_cny || ''),
    providerIdsByCountry: smsBowerProviderIdsByCountry,
    minStock: Math.max(numberOr(form.smsbower_auto_country_min_stock, 1), 0),
    usdCnyRate: Math.max(numberOr(form.sms_usd_cny_rate, 7.2), 0.01),
    providerRejectThreshold: Math.max(numberOr(form.smsbower_provider_reject_threshold, 2), 1),
    codeTimeoutSeconds: Math.min(Math.max(numberOr(form.sms_code_timeout_seconds, 180), 180), 300),
    phoneMaxAttempts: Math.min(Math.max(numberOr(form.sms_phone_max_attempts, 8), 1), 20),
    noNumbersWaitSeconds: Math.min(Math.max(numberOr(form.sms_no_numbers_wait_seconds, 120), 0), 600),
    tierCooldownMinutes: Math.min(Math.max(numberOr(form.sms_tier_cooldown_minutes, 45), 30), 60),
  }
  const applySmsBowerSelection = useCallback((patch: Partial<SmsBowerSelectionValue>) => {
    setForm(current => {
      const next = { ...current }
      if (patch.country !== undefined) next.sms_country = patch.country
      if (patch.countries !== undefined) next.sms_countries = patch.countries.join(',')
      if (patch.maxPriceUsd !== undefined) {
        next.sms_max_price = patch.maxPriceUsd
        next.smsbower_max_price = patch.maxPriceUsd
      }
      if (patch.bulkPriceCny !== undefined) next.sms_bulk_price_cny = patch.bulkPriceCny
      if (patch.providerIdsByCountry !== undefined) {
        next.smsbower_provider_ids_by_country = patch.providerIdsByCountry
      }
      if (patch.minStock !== undefined) next.smsbower_auto_country_min_stock = patch.minStock
      if (patch.usdCnyRate !== undefined) next.sms_usd_cny_rate = patch.usdCnyRate
      if (patch.providerRejectThreshold !== undefined) next.smsbower_provider_reject_threshold = patch.providerRejectThreshold
      if (patch.codeTimeoutSeconds !== undefined) next.sms_code_timeout_seconds = patch.codeTimeoutSeconds
      if (patch.phoneMaxAttempts !== undefined) next.sms_phone_max_attempts = patch.phoneMaxAttempts
      if (patch.noNumbersWaitSeconds !== undefined) next.sms_no_numbers_wait_seconds = patch.noNumbersWaitSeconds
      if (patch.tierCooldownMinutes !== undefined) next.sms_tier_cooldown_minutes = patch.tierCooldownMinutes
      return next
    })
  }, [])
  const summaryRegistration = needsSms && form.identity_provider === 'mailbox'
    ? '邮箱注册 + 手机验证'
    : registrationOptions.find(option => (
      option.identityProvider === form.identity_provider && option.oauthProvider === form.oauth_provider
    ))?.label || '-'
  const smsBowerProviderTierCount = Object.values(smsBowerProviderIdsByCountry)
    .reduce((total, ids) => total + ids.length, 0)
  const summarySms = !needsSms
    ? ''
    : isSmsBower
      ? `SMSBower · ${smsCountries.length} 国${smsBowerProviderTierCount > 0 ? ` / ${smsBowerProviderTierCount} 档` : ''}`
      : currentSmsProvider?.label || form.sms_provider || '-'
  const summarySmsPrice = Number(smsBowerSelection.bulkPriceCny || 0) > 0
    ? `人民币筛选 ≤ ¥${smsBowerSelection.bulkPriceCny} · 下单上限 $${smsBowerSelection.maxPriceUsd}`
    : Number(smsBowerSelection.maxPriceUsd || 0) > 0
      ? `下单上限 $${smsBowerSelection.maxPriceUsd}`
      : '不限价'
  const summaryExecutor = executorOptions.find(option => option.value === form.executor_type)?.label || '-'
  const summaryVerification = getCaptchaStrategyLabel(
    form.executor_type,
    configOptions.captcha_policy,
    configOptions.captcha_providers,
    language,
  )
  const smsCountry = selectedSmsCountry(form)
  const smsRegion = SMS_COUNTRY_PROXY_REGIONS[smsCountry]
  const configuredSub2Region = String(form.sub2api_agent_identity_region || 'CO').trim().toUpperCase()
  const requiredProxyRegion = smsRegion?.code || (form.sub2api_auto_sync ? configuredSub2Region : '')
  const requiredProxyLabel = smsRegion?.label || (form.sub2api_auto_sync ? configuredSub2Region : '自动选择')
  const explicitRegistrationProxyRegion = infer711ProxyRegion(form.proxy)
  const registrationProxyMismatch = Boolean(
    explicitRegistrationProxyRegion
    && requiredProxyRegion
    && explicitRegistrationProxyRegion !== requiredProxyRegion,
  )
  const sub2ProxyMismatch = Boolean(
    form.sub2api_auto_sync
    && smsRegion?.code
    && configuredSub2Region !== smsRegion.code,
  )
  const sub2ProxyMissing = Boolean(form.sub2api_auto_sync && Number(form.sub2api_proxy_id || 0) <= 0)
  const routeConfigurationInvalid = registrationProxyMismatch || sub2ProxyMismatch || sub2ProxyMissing
  // `sticky` remains accepted by the backend for old API callers, but the
  // workbench no longer presents it as a separate choice: ChatGPT routes are
  // pinned to a session automatically after a proxy is assigned.
  const effectiveProxyStrategy = form.proxy_strategy === 'sticky' ? 'polling' : (form.proxy_strategy || 'auto')
  const selectedProxyStrategy = PROXY_STRATEGY_OPTIONS.find(
    option => option.value === effectiveProxyStrategy,
  ) || PROXY_STRATEGY_OPTIONS[0]
  const manualProxyConfigured = Boolean(String(form.proxy || '').trim())
  const manualProxyRegionUnknown = Boolean(manualProxyConfigured && needsSms && !explicitRegistrationProxyRegion)
  const routeSummary = manualProxyConfigured
    ? `手动覆盖${explicitRegistrationProxyRegion ? ` · ${explicitRegistrationProxyRegion}` : ' · 地区待预检'}`
    : `${selectedProxyStrategy.label} · ${requiredProxyRegion || '自动地区'}`
  const selectedSub2Model = SUB2_FREE_MODEL_OPTIONS
    .find(([value]) => value === form.sub2api_default_model)?.[1]
    || form.sub2api_default_model
    || '自动选择'
  const activeTaskId = String(task?.task_id || task?.id || '')
  const taskIsTerminal = Boolean(task?.status && isTerminalTaskStatus(task.status))

  const refreshMailboxInventory = useCallback(async (silent = false) => {
    if (!needsMailbox || !supportsMailboxInventory || !form.mail_provider) {
      mailboxInventoryRequestRef.current += 1
      setMailboxInventory(null)
      setMailboxInventoryError('')
      setMailboxInventoryUpdatedAt(null)
      setMailboxInventoryLoading(false)
      return
    }

    const requestId = mailboxInventoryRequestRef.current + 1
    mailboxInventoryRequestRef.current = requestId
    if (!silent) setMailboxInventoryLoading(true)
    setMailboxInventoryError('')
    try {
      const data = await apiFetch('/mailbox-pool/inventory', {
        method: 'POST',
        body: JSON.stringify({
          provider_key: form.mail_provider,
          text: String(form.local_ms_pool_text || ''),
          pool_file: String(form.local_ms_pool_file || ''),
          state_file: String(form.local_ms_pool_state_file || ''),
          alias_enabled: asBoolean(form.mailbox_alias_enabled),
          alias_count: Math.max(numberOr(form.mailbox_alias_count, 1), 1),
          limit: 1,
        }),
      })
      if (requestId !== mailboxInventoryRequestRef.current) return
      setMailboxInventory({
        total_count: Math.max(numberOr(data?.total_count, 0), 0),
        available_count: Math.max(numberOr(data?.available_count, 0), 0),
        used_count: Math.max(numberOr(data?.used_count, 0), 0),
        blocked_count: Math.max(numberOr(data?.blocked_count, 0), 0),
        allow_reuse: Boolean(data?.allow_reuse),
        truncated: Boolean(data?.truncated),
      })
      setMailboxInventoryUpdatedAt(new Date())
    } catch (error) {
      if (requestId !== mailboxInventoryRequestRef.current) return
      setMailboxInventory(null)
      setMailboxInventoryError(error instanceof Error ? error.message : String(error || '邮箱库存读取失败'))
    } finally {
      if (requestId === mailboxInventoryRequestRef.current) setMailboxInventoryLoading(false)
    }
  }, [
    form.local_ms_pool_file,
    form.local_ms_pool_state_file,
    form.local_ms_pool_text,
    form.mail_provider,
    form.mailbox_alias_count,
    form.mailbox_alias_enabled,
    needsMailbox,
    supportsMailboxInventory,
  ])

  useEffect(() => {
    if (!needsMailbox || !supportsMailboxInventory || !form.mail_provider) {
      void refreshMailboxInventory()
      return
    }
    const timer = window.setTimeout(() => {
      void refreshMailboxInventory()
    }, 450)
    return () => window.clearTimeout(timer)
  }, [form.mail_provider, needsMailbox, refreshMailboxInventory, supportsMailboxInventory])

  useEffect(() => {
    if (!polling || !needsMailbox || !supportsMailboxInventory) return
    const timer = window.setInterval(() => {
      void refreshMailboxInventory(true)
    }, 30_000)
    return () => window.clearInterval(timer)
  }, [needsMailbox, polling, refreshMailboxInventory, supportsMailboxInventory])

  useEffect(() => {
    if (!taskIsTerminal || !needsMailbox || !supportsMailboxInventory) return
    void refreshMailboxInventory(true)
  }, [activeTaskId, needsMailbox, refreshMailboxInventory, supportsMailboxInventory, taskIsTerminal])

  const blockingIssues = useMemo(() => {
    const issues: string[] = []
    if (loadingOptions) issues.push('正在加载平台注册能力')
    else if (!currentPlatform) issues.push('请选择可用平台')
    if (!loadingOptions && registrationOptions.length === 0) issues.push('当前平台没有可用注册方式')
    if (!executorOptions.some(option => option.value === form.executor_type && !option.disabled)) {
      issues.push('请选择可用执行器')
    }
    if (needsMailbox && !form.mail_provider) issues.push('请选择邮箱服务')
    if (needsSms && !form.sms_provider) issues.push('请选择短信服务')
    if (needsSms && isSmsBower) {
      const maxPrice = Number(smsBowerSelection.maxPriceUsd)
      if (smsCountries.length === 0) issues.push('请至少选择一个 SMSBower 候选国家')
      if (smsCountries.includes('12')) issues.push('SMSBower 虚拟/VOIP 国家不能用于 ChatGPT 手机号注册')
      if (!Number.isFinite(maxPrice) || maxPrice < 0) issues.push('SMSBower 单号最高价格必须是大于等于 0 的数字')
    }
    if (sub2ProxyMissing) issues.push('Sub2 自动上传需要有效代理 ID')
    if (registrationProxyMismatch || sub2ProxyMismatch) issues.push('注册代理、短信国家和 Sub2 地区需要保持一致')
    if (Number(form.count || 0) < 1) issues.push('注册数量至少为 1')
    return issues
  }, [
    currentPlatform,
    executorOptions,
    form.count,
    form.executor_type,
    form.mail_provider,
    form.sms_provider,
    isSmsBower,
    loadingOptions,
    needsMailbox,
    needsSms,
    registrationOptions.length,
    registrationProxyMismatch,
    smsBowerSelection.maxPriceUsd,
    smsCountries,
    sub2ProxyMismatch,
    sub2ProxyMissing,
  ])

  const warnings = useMemo(() => {
    const items: string[] = []
    if (Number(form.concurrency || 1) > 5) items.push('并发超过 5 会明显增加验证码、代理和接码压力')
    if (Number(form.count || 1) > 1 && form.email) items.push('批量任务填写固定邮箱可能导致多次尝试复用同一身份')
    if (effectiveProxyStrategy === 'direct' && (needsSms || form.sub2api_auto_sync)) {
      items.push('直连模式没有稳定的地区对应关系，手机号验证更容易触发风控')
    }
    if (Number(form.post_registration_liveness_delay_seconds || 0) < 30) {
      items.push('复检等待少于 30 秒，可能漏掉注册后快速失效的账号')
    }
    if (effectiveProxyStrategy === 'polling') {
      items.push('池内分散依赖设置页中已有可用代理；每个注册流程拿到代理后会自动固定会话')
    }
    if (manualProxyRegionUnknown) {
      items.push('手动代理未带 region-XX 地区标记，启动时会以实际出口 IP 做预检，无法提前保证与号码国家一致')
    }
    return items
  }, [effectiveProxyStrategy, form, manualProxyRegionUnknown, needsSms])

  const canSubmit = blockingIssues.length === 0 && !submitting && !polling
  const targetReady = Boolean(currentPlatform && Number(form.count || 0) > 0)
  const identityReady = registrationOptions.length > 0
    && (!needsMailbox || Boolean(form.mail_provider))
    && (!needsSms || Boolean(form.sms_provider))
  const routeReady = Boolean(form.executor_type) && !registrationProxyMismatch
  const deliveryReady = !routeConfigurationInvalid
  const workflowSteps = [
    { label: '任务目标', ready: targetReady },
    { label: '身份资源', ready: identityReady },
    { label: '执行线路', ready: routeReady },
    { label: '交付质检', ready: deliveryReady },
    { label: taskIsTerminal ? '已完成' : task ? '运行中' : '等待启动', ready: taskIsTerminal },
  ]
  const firstIncompleteConfig = workflowSteps.slice(0, 4).findIndex(step => !step.ready)
  const activeStepIndex = task ? 4 : firstIncompleteConfig >= 0 ? firstIncompleteConfig : 4

  const submit = async () => {
    if (!canSubmit) return
    setSubmitting(true)
    setSubmitError('')
    try {
      const count = Math.min(Math.max(Number(form.count || 1), 1), 100)
      const concurrency = Math.min(Math.max(Number(form.concurrency || 1), 1), 20)
      const extra: Record<string, any> = {
        // The backend keeps a separate ChatGPT safety cap in `extra`.
        // Sending only the top-level concurrency made every workbench task
        // silently fall back to the legacy one-worker profile.
        high_concurrency: {
          mode: 'custom',
          concurrency,
        },
        identity_provider: form.identity_provider,
        oauth_provider: form.oauth_provider,
        oauth_email_hint: form.oauth_email_hint,
        chrome_user_data_dir: form.chrome_user_data_dir || undefined,
        chrome_cdp_url: form.chrome_cdp_url || undefined,
        phone_bind_email_after_registration: form.identity_provider === 'phone'
          ? Boolean(form.phone_bind_email_after_registration)
          : undefined,
        email_otp_timeout_seconds: form.identity_provider === 'phone'
          ? Math.min(Math.max(Number(form.email_otp_timeout_seconds || 300), 60), 600)
          : undefined,
        require_phone_verification: Boolean(
          form.platform === 'chatgpt'
          && form.identity_provider === 'mailbox'
          && (form.require_phone_verification || form.sub2api_auto_sync)
        ),
        register_mode: form.identity_provider === 'phone'
          ? 'phone'
          : needsSms && form.identity_provider === 'mailbox'
            ? 'email_then_phone'
            : 'email',
        proxy_strategy: form.proxy
          ? 'manual_template'
          : effectiveProxyStrategy,
        proxy_country: requiredProxyRegion || undefined,
        failure_policy: form.failure_policy,
        complete_started_attempts: Boolean(form.complete_started_attempts),
        post_registration_liveness_delay_seconds: Math.min(
          Math.max(Number(form.post_registration_liveness_delay_seconds || 60), 0),
          600,
        ),
        post_registration_probation_enabled: true,
        post_registration_probation_interval_seconds: 60,
        network_circuit_break_threshold: Math.min(
          Math.max(Number(form.network_circuit_break_threshold || 3), 0),
          20,
        ),
        sub2api_auto_sync: Boolean(form.sub2api_auto_sync),
        sub2api_proxy_id: Number(form.sub2api_proxy_id || 0),
        sub2api_proxy_region: configuredSub2Region,
        sub2api_model: String(form.sub2api_default_model || '').trim(),
      }
      if (form.mail_provider) extra.mail_provider = form.mail_provider
      if (form.sms_provider) extra.sms_provider = form.sms_provider
      allProviderFieldKeys.forEach(fieldKey => {
        if (form[fieldKey] !== undefined) extra[fieldKey] = form[fieldKey]
      })
      if (form.platform === 'chatgpt' && needsSms && isSmsBower) {
        const selectedCountryIds = normalizeSmsCountryIds(form.sms_countries, form.sms_country)
        if (selectedCountryIds.length === 0) throw new Error('请至少选择一个 SMSBower 候选国家')
        if (selectedCountryIds.includes('12')) throw new Error('虚拟/VOIP 国家不能用于 ChatGPT 手机号注册')

        const selectedProviderIdsByCountry = Object.fromEntries(
          selectedCountryIds
            .map(country => [country, smsBowerProviderIdsByCountry[country] || []] as const)
            .filter(([, ids]) => ids.length > 0),
        )
        const maxPrice = String(form.sms_max_price || form.smsbower_max_price || '0.13').trim()
        const bulkPriceCny = String(form.sms_bulk_price_cny || '').trim()
        extra.sms_service = 'dr'
        extra.sms_country = selectedCountryIds[0]
        extra.sms_countries = selectedCountryIds.join(',')
        extra.sms_max_price = maxPrice
        extra.smsbower_max_price = maxPrice
        if (Number(bulkPriceCny) > 0) extra.sms_bulk_price_cny = bulkPriceCny
        else delete extra.sms_bulk_price_cny
        extra.smsbower_provider_ids_by_country = selectedProviderIdsByCountry
        extra.smsbower_allow_virtual = false
        extra.smsbower_auto_country = false
        extra.smsbower_auto_country_min_stock = Math.max(numberOr(form.smsbower_auto_country_min_stock, 1), 0)
        extra.smsbower_auto_country_max_price = Math.max(numberOr(maxPrice, 0), 0)
        extra.smsbower_provider_reject_threshold = Math.max(numberOr(form.smsbower_provider_reject_threshold, 2), 1)
        extra.sms_code_timeout_seconds = Math.min(Math.max(numberOr(form.sms_code_timeout_seconds, 180), 180), 300)
        extra.sms_phone_max_attempts = Math.min(Math.max(numberOr(form.sms_phone_max_attempts, 8), 1), 20)
        extra.sms_no_numbers_wait_seconds = Math.min(Math.max(numberOr(form.sms_no_numbers_wait_seconds, 120), 0), 600)
        extra.sms_tier_cooldown_minutes = Math.min(Math.max(numberOr(form.sms_tier_cooldown_minutes, 45), 30), 60)
        extra.sms_no_numbers_retry_interval_seconds = 20
        if (selectedCountryIds.length === 1 && selectedProviderIdsByCountry[selectedCountryIds[0]]?.length) {
          extra.smsbower_provider_ids = selectedProviderIdsByCountry[selectedCountryIds[0]].join(',')
        } else {
          delete extra.smsbower_provider_ids
        }
      }
      const created = await apiFetch('/tasks/register', {
        method: 'POST',
        body: JSON.stringify({
          platform: form.platform,
          email: form.email || null,
          password: form.password || null,
          count,
          concurrency,
          proxy: form.proxy || null,
          executor_type: form.executor_type,
          captcha_solver: 'auto',
          extra,
        }),
      })
      const nextTask = {
        ...created,
        id: created?.id || created?.task_id,
        status: created?.status || 'pending',
        progress: created?.progress || `0/${count}`,
      }
      handledTerminalTaskIdsRef.current.delete(String(nextTask.id || ''))
      setTask(nextTask)
      setPolling(!isTerminalTaskStatus(nextTask.status))
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : String(error || '启动注册失败'))
    } finally {
      setSubmitting(false)
    }
  }

  const handleTaskUpdate = useCallback((latest: any) => {
    setTask(latest)
    if (isTerminalTaskStatus(latest?.status)) setPolling(false)
  }, [])

  const handleTaskDone = useCallback(async (status: string) => {
    if (!activeTaskId) return
    if (handledTerminalTaskIdsRef.current.has(activeTaskId)) {
      setPolling(false)
      return
    }
    try {
      const latest = await apiFetch(`/tasks/${activeTaskId}`)
      applyTerminalTask(latest, status)
    } catch {
      setTask((current: any) => ({ ...current, status }))
    } finally {
      setPolling(false)
    }
  }, [activeTaskId, applyTerminalTask])

  const activeTaskStats = task ? [
    { label: t('common.status'), value: getTaskStatusText(task.status, language), icon: Orbit },
    { label: t('common.progress'), value: task.progress || '0/0', icon: Workflow },
    { label: t('common.success'), value: String(task.success ?? 0), icon: CheckCircle },
    { label: t('common.failure'), value: String(task.error_count ?? task.errors?.length ?? 0), icon: XCircle },
    { label: 'Sub2 已上传', value: String(task.result?.sub2_sync?.synced ?? 0), icon: Server },
  ] : []

  return (
    <div className="space-y-5 pb-8">
      <section className="relative overflow-hidden rounded-2xl border border-[var(--border-soft)] bg-[var(--bg-pane)] px-5 py-5 sm:px-7 sm:py-6">
        <div className="pointer-events-none absolute -right-16 -top-24 h-56 w-56 rounded-full bg-[var(--accent-soft)] blur-3xl" />
        <div className="relative flex flex-col justify-between gap-5 lg:flex-row lg:items-end">
          <div className="min-w-0">
            <div className="mb-3 flex flex-wrap items-center gap-2">
              <Badge variant="secondary">REGISTER WORKBENCH</Badge>
              {fromAccounts ? <Badge variant="secondary">来自账号池</Badge> : null}
            </div>
            <h1 className="text-2xl font-semibold tracking-[-0.035em] text-[var(--text-primary)] sm:text-3xl">注册工作台</h1>
            <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--text-secondary)]">
              按注册真实顺序配置资源，启动前统一检查身份、线路、交付和存活复检；运行后在同一页查看结果。
            </p>
          </div>
          <div className="flex shrink-0 flex-wrap gap-2">
            <Button variant="outline" asChild>
              <Link to={`/accounts/${encodeURIComponent(form.platform || 'chatgpt')}`}>
                <Database className="mr-2 h-4 w-4" />账号池
              </Link>
            </Button>
            <Button variant="outline" asChild>
              <Link to="/history"><History className="mr-2 h-4 w-4" />任务历史</Link>
            </Button>
          </div>
        </div>
      </section>

      <Card className="overflow-hidden">
        <CardContent className="p-0">
          <div className="overflow-x-auto">
            <div className="grid min-w-[680px] grid-cols-5">
              {workflowSteps.map((step, index) => {
                const active = index === activeStepIndex
                return (
                  <div
                    key={`${index}-${step.label}`}
                    className={`relative border-r border-[var(--border-soft)] px-4 py-4 last:border-r-0 ${active ? 'bg-[var(--accent-soft)]' : ''}`}
                  >
                    <div className="flex items-center gap-2">
                      <span className={`flex h-6 w-6 items-center justify-center rounded-full border ${
                        step.ready
                          ? 'border-emerald-500/30 bg-emerald-500/12 text-emerald-400'
                          : active
                            ? 'border-[var(--accent-edge)] bg-[var(--bg-pane)] text-[var(--text-accent)]'
                            : 'border-[var(--border-soft)] text-[var(--text-muted)]'
                      }`}>
                        {step.ready ? <Check className="h-3.5 w-3.5" /> : <CircleDot className="h-3.5 w-3.5" />}
                      </span>
                      <span className="text-[10px] font-bold tracking-[0.18em] text-[var(--text-muted)]">0{index + 1}</span>
                    </div>
                    <div className={`mt-2 text-xs font-medium ${active ? 'text-[var(--text-primary)]' : 'text-[var(--text-secondary)]'}`}>{step.label}</div>
                  </div>
                )
              })}
            </div>
          </div>
        </CardContent>
      </Card>

      <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_360px]">
        <div className="space-y-5">
          <Card>
            <CardContent className="p-5 sm:p-6">
              <SectionHeading number="01" title="任务目标" description="先决定注册到哪里、需要多少账号，以及一次允许多少流程并行。" icon={Users} />
              <div className="grid gap-4 md:grid-cols-3">
                <FieldSelect
                  label={t('common.platform')}
                  value={form.platform}
                  onChange={value => set('platform', value)}
                  options={platformOptions}
                  hint={loadingOptions ? '正在加载平台注册能力…' : currentPlatform?.description}
                />
                <FieldInput label="成功目标" value={form.count} onChange={value => set('count', value)} type="number" min={1} max={100} hint="达到目标后停止投放新尝试" />
                <FieldInput
                  label="并发窗口"
                  value={form.concurrency}
                  onChange={value => set('concurrency', value)}
                  type="number"
                  min={1}
                  max={20}
                  hint={needsSms
                    ? '并发接码时每条流程自动租用独立号码；建议先从 1–2 验证线路'
                    : '建议从 1–2 开始，稳定后再增加'}
                />
              </div>
              <div className="mt-4 flex flex-wrap items-center gap-2">
                <span className="mr-1 text-xs text-[var(--text-muted)]">快速并发</span>
                {[1, 2, 5, 10].map(value => (
                  <button
                    key={value}
                    type="button"
                    onClick={() => set('concurrency', value)}
                    className={`rounded-lg border px-3 py-1.5 text-xs transition-colors ${
                      Number(form.concurrency) === value
                        ? 'border-[var(--accent-edge)] bg-[var(--accent-soft)] text-[var(--text-accent)]'
                        : 'border-[var(--border-soft)] bg-[var(--chip-bg)] text-[var(--text-secondary)] hover:border-[var(--accent-edge)]'
                    }`}
                  >{value}</button>
                ))}
                <span className="ml-auto flex items-center gap-1.5 text-xs text-[var(--text-muted)]">
                  <Gauge className="h-3.5 w-3.5" />最多同时运行 {Math.min(Math.max(Number(form.concurrency || 1), 1), 20)} 条流程
                </span>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardContent className="p-5 sm:p-6">
              <SectionHeading number="02" title="身份与验证资源" description="先选身份入口，再只显示这条流程真正会使用的邮箱和短信资源。" icon={Layers3} />
              {optionsError ? (
                <div className="mb-4 rounded-xl border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-300">{optionsError}</div>
              ) : null}
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                {registrationFlowOptions.map(option => {
                  const phoneVerificationActive = Boolean(form.require_phone_verification || form.sub2api_auto_sync)
                  const active = form.identity_provider === option.identityProvider
                    && form.oauth_provider === option.oauthProvider
                    && (option.requiresPhoneVerification === null
                      || option.requiresPhoneVerification === phoneVerificationActive)
                  const IdentityIcon = option.identityProvider === 'phone'
                    ? Smartphone
                    : option.identityProvider === 'mailbox'
                      ? option.requiresPhoneVerification
                        ? MailCheck
                        : Mail
                      : ShieldCheck
                  return (
                    <button
                      key={option.key}
                      type="button"
                      aria-pressed={active}
                      onClick={() => setForm(current => {
                        const controlsPhoneVerification = option.requiresPhoneVerification !== null
                        const requiresPhoneVerification = option.requiresPhoneVerification === true
                        return {
                          ...current,
                          identity_provider: option.identityProvider,
                          oauth_provider: option.oauthProvider,
                          require_phone_verification: controlsPhoneVerification
                            ? requiresPhoneVerification
                            : false,
                          sub2api_auto_sync: controlsPhoneVerification && requiresPhoneVerification
                            ? current.sub2api_auto_sync
                            : false,
                        }
                      })}
                      className={`rounded-xl border p-4 text-left transition-colors ${
                        active
                          ? 'border-[var(--accent-edge)] bg-[var(--accent-soft)]'
                          : 'border-[var(--border-soft)] bg-[var(--chip-bg)] hover:border-[var(--accent-edge)]'
                      }`}
                    >
                      <div className="flex items-start gap-3">
                        <span className={`mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${active ? 'bg-[var(--bg-pane)] text-[var(--text-accent)]' : 'bg-[var(--bg-hover)] text-[var(--text-secondary)]'}`}>
                          <IdentityIcon className="h-4 w-4" />
                        </span>
                        <span className="min-w-0">
                          <span className="flex flex-wrap items-center gap-2 text-sm font-medium text-[var(--text-primary)]">
                            {option.label}
                            {option.requiresPhoneVerification === true ? <Badge variant="success">Codex RT</Badge> : null}
                          </span>
                          <span className="mt-1 block text-xs leading-5 text-[var(--text-muted)]">{option.description}</span>
                        </span>
                      </div>
                    </button>
                  )
                })}
              </div>

              {form.identity_provider === 'mailbox' ? (
                <div className="mt-4 flex flex-wrap items-center justify-center gap-2 rounded-xl border border-[var(--border-soft)] bg-[var(--chip-bg)] px-3 py-3 text-center text-xs text-[var(--text-secondary)]">
                  <span className="flex items-center gap-1.5"><Mail className="h-4 w-4" />系统邮箱</span>
                  <ArrowRight className="h-4 w-4 text-[var(--text-muted)]" />
                  <span>邮箱 OTP</span>
                  {needsSms ? (
                    <>
                      <ArrowRight className="h-4 w-4 text-[var(--text-muted)]" />
                      <span className="flex items-center gap-1.5"><Smartphone className="h-4 w-4" />手机号</span>
                      <ArrowRight className="h-4 w-4 text-[var(--text-muted)]" />
                      <span>短信 OTP</span>
                      <ArrowRight className="h-4 w-4 text-[var(--text-muted)]" />
                      <span className="flex items-center gap-1.5"><ShieldCheck className="h-4 w-4" />Codex RT</span>
                    </>
                  ) : (
                    <>
                      <ArrowRight className="h-4 w-4 text-[var(--text-muted)]" />
                      <span className="flex items-center gap-1.5"><CheckCircle className="h-4 w-4" />完成注册</span>
                    </>
                  )}
                </div>
              ) : form.identity_provider === 'phone' ? (
                <div className="mt-4 grid grid-cols-[1fr_auto_1fr_auto_1fr] items-center gap-2 rounded-xl border border-[var(--border-soft)] bg-[var(--chip-bg)] px-3 py-3 text-center text-xs text-[var(--text-secondary)]">
                  <span className="flex items-center justify-center gap-1.5"><Smartphone className="h-4 w-4" />手机号</span>
                  <ArrowRight className="h-4 w-4 text-[var(--text-muted)]" />
                  <span>短信 OTP</span>
                  <ArrowRight className="h-4 w-4 text-[var(--text-muted)]" />
                  <span className="flex items-center justify-center gap-1.5"><MailCheck className="h-4 w-4" />邮箱 OTP</span>
                </div>
              ) : null}

              {needsMailbox ? (
                <div className="mt-5 rounded-xl border border-[var(--border-soft)] bg-[var(--bg-pane)]/40 p-4">
                  <div className="mb-4 flex items-center justify-between gap-3">
                    <div>
                      <div className="text-sm font-semibold text-[var(--text-primary)]">邮箱资源</div>
                      <div className="mt-1 text-xs text-[var(--text-muted)]">用于创建账号或手机号注册后的邮箱绑定</div>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      {supportsMailboxInventory ? (
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          disabled={mailboxInventoryLoading}
                          onClick={() => void refreshMailboxInventory()}
                        >
                          <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${mailboxInventoryLoading ? 'animate-spin' : ''}`} />
                          刷新库存
                        </Button>
                      ) : null}
                      <Badge variant={form.mail_provider ? 'secondary' : 'danger'}>{form.mail_provider ? '已选择' : '待配置'}</Badge>
                    </div>
                  </div>
                  {supportsMailboxInventory ? (
                    <div className="mb-4 rounded-xl border border-[var(--border-soft)] bg-[var(--chip-bg)] p-3">
                      {mailboxInventory ? (
                        <>
                          <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
                            {[
                              { label: '可分配邮箱', value: mailboxInventory.available_count, tone: 'text-emerald-400' },
                              { label: '邮箱总数', value: mailboxInventory.total_count, tone: 'text-[var(--text-primary)]' },
                              { label: '已占用', value: mailboxInventory.used_count, tone: 'text-amber-300' },
                              { label: '已封禁', value: mailboxInventory.blocked_count, tone: 'text-rose-400' },
                            ].map(item => (
                              <div key={item.label} className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)]/70 px-3 py-2.5">
                                <div className="text-[11px] text-[var(--text-muted)]">{item.label}</div>
                                <div className={`mt-1 text-xl font-semibold tabular-nums ${item.tone}`}>{item.value}</div>
                              </div>
                            ))}
                          </div>
                          <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-[11px] leading-4 text-[var(--text-muted)]">
                            <span>可分配只代表未被占用，不代表 OAuth 一定能正常取码。</span>
                            <span>{mailboxInventoryUpdatedAt ? `更新于 ${mailboxInventoryUpdatedAt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}` : ''}</span>
                          </div>
                        </>
                      ) : mailboxInventoryLoading ? (
                        <div className="flex min-h-20 items-center justify-center gap-2 text-xs text-[var(--text-muted)]">
                          <Loader2 className="h-4 w-4 animate-spin" />正在读取邮箱库存…
                        </div>
                      ) : (
                        <div className="flex min-h-20 items-center gap-2 rounded-lg border border-amber-500/20 bg-amber-500/8 px-3 text-xs leading-5 text-amber-300">
                          <AlertTriangle className="h-4 w-4 shrink-0" />
                          <span>{mailboxInventoryError || '暂未读取到邮箱库存，请检查邮箱池配置后刷新。'}</span>
                        </div>
                      )}
                    </div>
                  ) : null}
                  {mailboxProviderOptions.length > 0 ? (
                    <div className="grid gap-4 md:grid-cols-2">
                      <FieldSelect label={t('register.mailboxService')} value={form.mail_provider} onChange={value => set('mail_provider', value)} options={mailboxProviderOptions} />
                      {(currentMailboxProvider?.fields || []).map(field => (
                        <ProviderFieldControl key={field.key} field={field} value={form[field.key]} onChange={value => set(field.key, value)} />
                      ))}
                    </div>
                  ) : (
                    <div className="text-sm text-amber-300">没有可用邮箱服务，请先到设置中启用 Provider。</div>
                  )}
                  {currentMailboxProvider?.description ? <p className="mt-3 text-xs leading-5 text-[var(--text-muted)]">{currentMailboxProvider.description}</p> : null}
                  {form.identity_provider === 'phone' ? (
                    <div className="mt-4 grid gap-4 md:grid-cols-2">
                      <ToggleRow
                        label="注册成功后绑定邮箱"
                        description="手机号完成后继续获取邮箱 OTP，便于后续登录和恢复"
                        checked={Boolean(form.phone_bind_email_after_registration)}
                        onChange={checked => set('phone_bind_email_after_registration', checked)}
                      />
                      {form.phone_bind_email_after_registration ? (
                        <FieldSelect
                          label="邮箱验证码等待时间"
                          value={form.email_otp_timeout_seconds}
                          onChange={value => set('email_otp_timeout_seconds', value)}
                          options={[[120, '2 分钟'], [180, '3 分钟'], [300, '5 分钟'], [480, '8 分钟']]}
                        />
                      ) : null}
                    </div>
                  ) : null}
                </div>
              ) : null}

              {needsSms ? (
                <div className="mt-4 rounded-xl border border-[var(--border-soft)] bg-[var(--bg-pane)]/40 p-4">
                  <div className="mb-4 flex items-center justify-between gap-3">
                    <div>
                      <div className="text-sm font-semibold text-[var(--text-primary)]">短信资源</div>
                      <div className="mt-1 text-xs text-[var(--text-muted)]">{form.identity_provider === 'mailbox'
                        ? form.sub2api_auto_sync
                          ? '邮箱建号后继续手机验证，再进入 Sub2 交付'
                          : '邮箱建号后继续手机验证，并获取交付所需的 Codex RT'
                        : '用于手机号注册与短信 OTP'}</div>
                    </div>
                    <Badge variant={form.sms_provider ? 'secondary' : 'danger'}>{form.sms_provider ? '已选择' : '待配置'}</Badge>
                  </div>
                  {smsProviderOptions.length > 0 ? (
                    <div className="grid gap-4 md:grid-cols-2">
                      <FieldSelect label={t('register.smsService')} value={form.sms_provider} onChange={value => set('sms_provider', value)} options={smsProviderOptions} />
                      {(currentSmsProvider?.fields || [])
                        .filter(field => !(isSmsBower && SMSBOWER_MANAGED_FIELD_KEYS.has(field.key)))
                        .map(field => (
                        <ProviderFieldControl key={field.key} field={field} value={form[field.key]} onChange={value => set(field.key, value)} />
                        ))}
                    </div>
                  ) : (
                    <div className="text-sm text-amber-300">没有可用短信服务，请先到设置中启用 Provider。</div>
                  )}
                  {currentSmsProvider?.description ? <p className="mt-3 text-xs leading-5 text-[var(--text-muted)]">{currentSmsProvider.description}</p> : null}
                  {isSmsBower ? (
                    <SmsBowerPriceSelector
                      value={smsBowerSelection}
                      onChange={applySmsBowerSelection}
                      queryProxy={String(form.proxy || '')}
                      credentialsConfigured={smsBowerCredentialsConfigured}
                    />
                  ) : null}
                </div>
              ) : null}
            </CardContent>
          </Card>

          <Card>
            <CardContent className="p-5 sm:p-6">
              <SectionHeading number="03" title="执行器与网络线路" description="执行方式和出口线路放在一起配置，减少代理国家与接码国家不一致。" icon={Radio} />
              <div className="grid gap-3 md:grid-cols-3">
                {executorOptions.map(option => {
                  const active = form.executor_type === option.value
                  return (
                    <button
                      key={option.value}
                      type="button"
                      disabled={option.disabled}
                      onClick={() => !option.disabled && set('executor_type', option.value)}
                      className={`rounded-xl border p-4 text-left transition-colors ${
                        option.disabled
                          ? 'cursor-not-allowed border-[var(--border-soft)] bg-[var(--bg-hover)] opacity-45'
                          : active
                            ? 'border-[var(--accent-edge)] bg-[var(--accent-soft)]'
                            : 'border-[var(--border-soft)] bg-[var(--chip-bg)] hover:border-[var(--accent-edge)]'
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-sm font-medium text-[var(--text-primary)]">{option.label}</span>
                        {active ? <Check className="h-4 w-4 text-[var(--text-accent)]" /> : null}
                      </div>
                      <div className="mt-2 text-xs leading-5 text-[var(--text-muted)]">{option.description}</div>
                      {option.reason ? <div className="mt-2 text-xs text-amber-300">{option.reason}</div> : null}
                    </button>
                  )
                })}
              </div>

              <div className="mt-5 rounded-2xl border border-[var(--border-soft)] bg-[var(--bg-pane)]/40 p-4 sm:p-5">
                <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                  <div>
                    <div className="flex items-center gap-2 text-sm font-semibold text-[var(--text-primary)]">
                      <Orbit className="h-4 w-4 text-[var(--text-accent)]" />
                      注册出口
                    </div>
                    <div className="mt-1 text-xs leading-5 text-[var(--text-muted)]">选择代理来源；单次注册的会话固定由系统自动完成</div>
                  </div>
                  <Badge variant="secondary">目标地区：{requiredProxyRegion ? `${requiredProxyRegion} · ${requiredProxyLabel}` : '自动选择'}</Badge>
                </div>

                <div className="mt-4 grid gap-3 lg:grid-cols-3">
                  {PROXY_STRATEGY_OPTIONS.map(option => {
                    const active = effectiveProxyStrategy === option.value
                    const Icon = option.icon
                    return (
                      <button
                        key={option.value}
                        type="button"
                        onClick={() => set('proxy_strategy', option.value)}
                        className={`group rounded-xl border p-4 text-left transition-all ${
                          active
                            ? 'border-[var(--accent-edge)] bg-[var(--accent-soft)] shadow-sm'
                            : 'border-[var(--border-soft)] bg-[var(--chip-bg)] hover:border-[var(--accent-edge)] hover:bg-[var(--accent-soft)]/40'
                        }`}
                      >
                        <div className="flex items-start justify-between gap-3">
                          <span className={`flex h-9 w-9 items-center justify-center rounded-lg ${active ? 'bg-[var(--accent-edge)]/20 text-[var(--text-accent)]' : 'bg-[var(--bg-hover)] text-[var(--text-muted)] group-hover:text-[var(--text-accent)]'}`}>
                            <Icon className="h-4 w-4" />
                          </span>
                          <div className="flex items-center gap-2">
                            <Badge variant={active ? 'default' : 'secondary'}>{option.tag}</Badge>
                            {active ? <Check className="h-4 w-4 text-[var(--text-accent)]" /> : null}
                          </div>
                        </div>
                        <div className="mt-3 text-sm font-semibold text-[var(--text-primary)]">{option.label}</div>
                        <div className="mt-1 text-xs font-medium leading-5 text-[var(--text-secondary)]">{option.summary}</div>
                        <div className="mt-2 text-[11px] leading-5 text-[var(--text-muted)]">{option.description}</div>
                      </button>
                    )
                  })}
                </div>

                <div className="mt-4 rounded-xl border border-[var(--border-soft)] bg-[var(--chip-bg)] p-4">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                    <div>
                      <div className="text-xs font-medium uppercase tracking-wide text-[var(--text-muted)]">本次线路预览</div>
                      <div className="mt-1 text-base font-semibold text-[var(--text-primary)]">{routeSummary}</div>
                    </div>
                    <Badge variant={registrationProxyMismatch ? 'danger' : manualProxyRegionUnknown ? 'warning' : 'success'}>
                      {registrationProxyMismatch ? '地区不一致' : manualProxyRegionUnknown ? '等待预检' : '配置可用'}
                    </Badge>
                  </div>
                  <div className="mt-3 grid gap-2 sm:grid-cols-2">
                    <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)]/50 px-3 py-2.5">
                      <div className="flex items-center gap-2 text-[11px] text-[var(--text-muted)]"><Smartphone className="h-3.5 w-3.5" />号码国家</div>
                      <div className="mt-1 text-sm font-medium text-[var(--text-primary)]">{smsRegion ? `${requiredProxyRegion} · ${requiredProxyLabel}` : '未指定，启动时自动判断'}</div>
                    </div>
                    <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)]/50 px-3 py-2.5">
                      <div className="flex items-center gap-2 text-[11px] text-[var(--text-muted)]"><MapPin className="h-3.5 w-3.5" />目标代理地区</div>
                      <div className={`mt-1 text-sm font-medium ${registrationProxyMismatch ? 'text-red-300' : manualProxyRegionUnknown ? 'text-amber-300' : 'text-[var(--text-primary)]'}`}>
                        {manualProxyConfigured
                          ? explicitRegistrationProxyRegion || '手动代理 · 启动时预检'
                          : requiredProxyRegion || '由代理池预检选择'}
                      </div>
                    </div>
                  </div>
                </div>

                <div className="mt-3 rounded-xl border border-blue-500/20 bg-blue-500/5 p-4">
                  <div className="flex items-start gap-3">
                    <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-blue-300" />
                    <div>
                      <div className="text-sm font-medium text-[var(--text-primary)]">会话稳定性：系统自动固定</div>
                      <div className="mt-1 text-xs leading-5 text-[var(--text-muted)]">拿到 711Proxy 后，当前注册流程会自动追加 Session 并固定约 180 分钟；不需要再单独选择“固定会话”。</div>
                    </div>
                  </div>
                </div>

                <div className="mt-4 rounded-xl border border-[var(--border-soft)] bg-[var(--bg-pane)]/40 p-4">
                  <div className="mb-3 flex items-center justify-between gap-3">
                    <div>
                      <div className="text-sm font-semibold text-[var(--text-primary)]">手动代理覆盖 <span className="font-normal text-[var(--text-muted)]">（可选）</span></div>
                      <div className="mt-1 text-xs leading-5 text-[var(--text-muted)]">填写后会覆盖上面的代理策略；留空则使用代理池。</div>
                    </div>
                    <Badge variant={manualProxyConfigured ? 'default' : 'secondary'}>{manualProxyConfigured ? '已覆盖代理池' : '未启用'}</Badge>
                  </div>
                  <FieldInput
                    label="代理地址"
                    value={form.proxy}
                    onChange={value => set('proxy', value)}
                    placeholder="http://user:pass@host:port"
                  />
                  <div className="mt-2 text-[11px] leading-5 text-[var(--text-muted)]">711Proxy 建议在用户名中带 <code className="rounded bg-[var(--bg-hover)] px-1">region-US</code> 这类地区标记；其他代理会在启动时通过实际出口 IP 预检地区。</div>
                  <div className={`mt-3 flex items-start gap-2 rounded-lg border px-3 py-2.5 text-xs leading-5 ${
                    registrationProxyMismatch
                      ? 'border-red-500/25 bg-red-500/10 text-red-300'
                      : manualProxyRegionUnknown
                        ? 'border-amber-500/25 bg-amber-500/10 text-amber-300'
                        : 'border-emerald-500/20 bg-emerald-500/10 text-emerald-300'
                  }`}>
                    {registrationProxyMismatch ? <XCircle className="mt-0.5 h-4 w-4 shrink-0" /> : manualProxyRegionUnknown ? <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> : <CheckCircle className="mt-0.5 h-4 w-4 shrink-0" />}
                    <span>{registrationProxyMismatch
                      ? `手动代理地区 ${explicitRegistrationProxyRegion} 与流程要求 ${requiredProxyRegion} 不一致`
                      : manualProxyRegionUnknown
                        ? '已填写手动代理，但地区还不能确认；启动预检会检查实际出口 IP。'
                        : `线路预检条件满足：${manualProxyConfigured ? '使用手动代理' : effectiveProxyStrategy === 'direct' ? '直连' : `${selectedProxyStrategy.label} 使用代理池`}${requiredProxyRegion ? `，目标地区 ${requiredProxyRegion}` : ''}`}</span>
                  </div>
                </div>

                <details className="mt-4 rounded-xl border border-[var(--border-soft)] bg-[var(--chip-bg)] px-4 py-3">
                  <summary className="flex cursor-pointer list-none items-center justify-between gap-3 text-sm font-medium text-[var(--text-primary)]">
                    <span className="flex items-center gap-2"><Settings2 className="h-4 w-4 text-[var(--text-muted)]" />策略怎么工作</span>
                    <ChevronDown className="h-4 w-4 text-[var(--text-muted)]" />
                  </summary>
                  <ol className="mt-3 space-y-2 pl-5 text-xs leading-5 text-[var(--text-muted)]">
                    <li>先根据手机号国家计算目标地区。</li>
                    <li>再按所选策略获取代理；手动代理会覆盖代理池。</li>
                    <li>711Proxy 在注册流程中自动固定 Session，避免同一流程中途换出口。</li>
                    <li>代理失败会进入冷却，下一次尝试再换其他可用条目。</li>
                  </ol>
                </details>

                <div className="mt-3 flex items-center gap-2 text-xs text-[var(--text-muted)]">
                  <ScanText className="h-3.5 w-3.5" />验证码策略：{summaryVerification}
                </div>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardContent className="p-5 sm:p-6">
              <SectionHeading number="04" title="交付与存活质检" description="注册结束后的保存、上传和延迟复检集中在最后一步，避免“显示成功但账号已经失效”。" icon={ShieldCheck} />
              <div className="grid gap-4 md:grid-cols-3">
                <FieldSelect
                  label="任务内首次质检"
                  value={form.post_registration_liveness_delay_seconds}
                  onChange={value => set('post_registration_liveness_delay_seconds', value)}
                  options={[[15, '15 秒 · 极速'], [30, '30 秒'], [60, '60 秒 · 推荐'], [120, '2 分钟 · 严格'], [180, '3 分钟']]}
                  hint="注册任务结束前先验证一次；通过后才进入后台持续监控"
                />
                <div className="rounded-xl border border-emerald-500/20 bg-emerald-500/10 px-4 py-3">
                  <div className="flex items-center gap-2 text-xs text-emerald-300"><ShieldCheck className="h-4 w-4" />后台持续复检</div>
                  <div className="mt-2 text-sm font-medium text-[var(--text-primary)]">每 60 秒，持续运行</div>
                  <div className="mt-1 text-[11px] leading-4 text-[var(--text-muted)]">服务重启后续跑；有效或未知都排下一分钟，明确失效才停止</div>
                </div>
                <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--chip-bg)] px-4 py-3">
                  <div className="flex items-center gap-2 text-xs text-[var(--text-muted)]"><Database className="h-4 w-4" />基础交付</div>
                  <div className="mt-2 text-sm font-medium text-[var(--text-primary)]">保存到 {currentPlatform?.display_name || form.platform} 账号池</div>
                  <div className="mt-1 text-[11px] text-[var(--text-muted)]">账号状态与检测时间一并记录</div>
                </div>
              </div>

              {form.platform === 'chatgpt' ? (
                <div className="mt-4 space-y-4">
                  <ToggleRow
                    label="通过存活复检后自动上传 Sub2"
                    description="启用后邮箱注册会自动增加手机验证，并要求注册线路与 Sub2 地区一致"
                    checked={Boolean(form.sub2api_auto_sync)}
                    onChange={checked => set('sub2api_auto_sync', checked)}
                  />
                  {form.sub2api_auto_sync ? (
                    <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg-pane)]/40 p-4">
                      <div className="grid gap-4 md:grid-cols-3">
                        <FieldInput label="Sub2 代理 ID" value={form.sub2api_proxy_id} onChange={value => set('sub2api_proxy_id', value)} type="number" min={1} />
                        <FieldSelect label="Sub2 代理地区" value={configuredSub2Region} onChange={value => set('sub2api_agent_identity_region', value)} options={REGION_OPTIONS} />
                        <FieldSelect label="Sub2 模型" value={form.sub2api_default_model} onChange={value => set('sub2api_default_model', value)} options={SUB2_FREE_MODEL_OPTIONS} />
                      </div>
                      <div className="mt-4 grid gap-3 sm:grid-cols-3">
                        <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--chip-bg)] p-3">
                          <div className="flex items-center gap-2 text-[11px] text-[var(--text-muted)]"><MapPin className="h-3.5 w-3.5" />注册地区</div>
                          <div className="mt-2 text-sm font-semibold text-[var(--text-primary)]">{requiredProxyRegion || '-'} · {requiredProxyLabel}</div>
                        </div>
                        <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--chip-bg)] p-3">
                          <div className="flex items-center gap-2 text-[11px] text-[var(--text-muted)]"><Server className="h-3.5 w-3.5" />Sub2 路由</div>
                          <div className="mt-2 text-sm font-semibold text-[var(--text-primary)]">#{Number(form.sub2api_proxy_id || 0) || '-'} · {configuredSub2Region}</div>
                        </div>
                        <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--chip-bg)] p-3">
                          <div className="flex items-center gap-2 text-[11px] text-[var(--text-muted)]"><Cpu className="h-3.5 w-3.5" />模型</div>
                          <div className="mt-2 truncate text-sm font-semibold text-[var(--text-primary)]" title={selectedSub2Model}>{selectedSub2Model}</div>
                        </div>
                      </div>
                      <div className={`mt-4 flex items-start gap-2 rounded-lg border px-3 py-2.5 text-xs ${
                        routeConfigurationInvalid
                          ? 'border-red-500/25 bg-red-500/10 text-red-300'
                          : 'border-emerald-500/20 bg-emerald-500/10 text-emerald-300'
                      }`}>
                        {routeConfigurationInvalid ? <XCircle className="h-4 w-4 shrink-0" /> : <CheckCircle className="h-4 w-4 shrink-0" />}
                        <span>{sub2ProxyMissing
                          ? '请先选择有效的 Sub2 代理 ID'
                          : routeConfigurationInvalid
                            ? `地区不一致：注册需要 ${requiredProxyRegion}，Sub2 当前为 ${configuredSub2Region}`
                            : `交付链路就绪：${requiredProxyRegion} 注册 → 延迟复检 → Sub2 proxy #${Number(form.sub2api_proxy_id)} → ${selectedSub2Model}`}</span>
                      </div>
                    </div>
                  ) : null}
                </div>
              ) : null}
            </CardContent>
          </Card>

          <details className="group rounded-xl border border-[var(--border-soft)] bg-[var(--bg-pane)]">
            <summary className="flex cursor-pointer list-none items-center justify-between gap-4 px-5 py-4 sm:px-6">
              <span className="flex items-center gap-3">
                <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-[var(--chip-bg)] text-[var(--text-secondary)]"><Settings2 className="h-4 w-4" /></span>
                <span>
                  <span className="block text-sm font-semibold text-[var(--text-primary)]">高级控制</span>
                  <span className="mt-0.5 block text-xs text-[var(--text-muted)]">固定账号、失败策略和并发窗口策略</span>
                </span>
              </span>
              <ChevronDown className="h-4 w-4 text-[var(--text-muted)] transition-transform group-open:rotate-180" />
            </summary>
            <div className="border-t border-[var(--border-soft)] px-5 py-5 sm:px-6">
              <div className="grid gap-4 md:grid-cols-2">
                <FieldInput label="固定邮箱（可选）" value={form.email} onChange={value => set('email', value)} placeholder="仅建议单账号调试时使用" />
                <FieldInput label="固定密码（可选）" value={form.password} onChange={value => set('password', value)} type="password" placeholder="留空则自动生成" />
                <FieldSelect
                  label="失败处理"
                  value={form.failure_policy}
                  onChange={value => set('failure_policy', value)}
                  options={[
                    ['retry_then_continue', '失败重试后继续 · 推荐'],
                    ['continue', '记录失败并继续'],
                    ['stop_on_failure', '首次失败即停止'],
                  ]}
                  hint="批量任务建议保留默认策略"
                />
                <FieldSelect
                  label="连续网络失败熔断"
                  value={form.network_circuit_break_threshold}
                  onChange={value => set('network_circuit_break_threshold', Number(value))}
                  options={[[3, '连续 3 次 · 推荐'], [5, '连续 5 次'], [0, '关闭熔断']]}
                  hint="代理或网络连续失败时停止投放新账号，已在运行的流程仍会收尾"
                />
                <ToggleRow
                  label="预启动完整并发窗口"
                  description="并发大于成功目标时仍先启动整个窗口；可能产出超过目标的账号"
                  checked={Boolean(form.complete_started_attempts)}
                  onChange={checked => set('complete_started_attempts', checked)}
                />
              </div>
              {form.identity_provider === 'oauth_browser' ? (
                <div className="mt-5 grid gap-4 border-t border-[var(--border-soft)] pt-5 md:grid-cols-2">
                  <FieldInput label={t('register.oauthHintOptional')} value={form.oauth_email_hint} onChange={value => set('oauth_email_hint', value)} placeholder="your-account@example.com" />
                  <FieldInput label={t('settings.chromeProfile')} value={form.chrome_user_data_dir} onChange={value => set('chrome_user_data_dir', value)} placeholder="Chrome user data directory" />
                  <div className="md:col-span-2">
                    <FieldInput label={t('settings.chromeCdp')} value={form.chrome_cdp_url} onChange={value => set('chrome_cdp_url', value)} placeholder="http://localhost:9222" hint={t('register.browserReuseHint')} />
                  </div>
                </div>
              ) : null}
            </div>
          </details>
        </div>

        <aside className="space-y-4 xl:sticky xl:top-4">
          <Card className="overflow-hidden">
            <CardContent className="p-0">
              <div className={`border-b border-[var(--border-soft)] px-5 py-4 ${blockingIssues.length === 0 ? 'bg-emerald-500/8' : 'bg-amber-500/8'}`}>
                <div className="flex items-center gap-3">
                  <span className={`flex h-9 w-9 items-center justify-center rounded-xl ${blockingIssues.length === 0 ? 'bg-emerald-500/15 text-emerald-400' : 'bg-amber-500/15 text-amber-300'}`}>
                    {blockingIssues.length === 0 ? <CheckCircle className="h-4.5 w-4.5" /> : <AlertTriangle className="h-4.5 w-4.5" />}
                  </span>
                  <div>
                    <div className="text-sm font-semibold text-[var(--text-primary)]">{blockingIssues.length === 0 ? '启动前预检通过' : `还有 ${blockingIssues.length} 项待处理`}</div>
                    <div className="mt-0.5 text-[11px] text-[var(--text-muted)]">配置变动会在这里即时校验</div>
                  </div>
                </div>
              </div>
              <div className="space-y-4 p-5">
                <div className="space-y-2.5">
                  {[
                    ['平台', currentPlatform?.display_name || form.platform || '-'],
                    ['目标', `${Math.max(Number(form.count || 0), 0)} 个 · 并发 ${Math.max(Number(form.concurrency || 1), 1)}`],
                    ['身份', summaryRegistration],
                    ...(needsSms ? [['接码', summarySms]] : []),
                    ...(needsSms && isSmsBower ? [['价格', summarySmsPrice]] : []),
                    ['执行', summaryExecutor],
                    ['线路', routeSummary],
                    ['首次质检', `${Number(form.post_registration_liveness_delay_seconds || 0)} 秒后`],
                    ['持续复检', '每 60 秒 · 持久化'],
                    ['网络熔断', Number(form.network_circuit_break_threshold || 0) > 0 ? `连续 ${Number(form.network_circuit_break_threshold)} 次` : '关闭'],
                    ['交付', form.sub2api_auto_sync ? `账号池 + Sub2 #${Number(form.sub2api_proxy_id || 0) || '-'}` : '仅账号池'],
                  ].map(([label, value]) => (
                    <div key={label} className="flex items-start justify-between gap-4 text-xs">
                      <span className="shrink-0 text-[var(--text-muted)]">{label}</span>
                      <span className="min-w-0 text-right font-medium text-[var(--text-primary)]">{value}</span>
                    </div>
                  ))}
                </div>

                {blockingIssues.length > 0 ? (
                  <div className="space-y-2 rounded-xl border border-red-500/20 bg-red-500/8 p-3">
                    {blockingIssues.map(issue => (
                      <div key={issue} className="flex items-start gap-2 text-xs leading-5 text-red-300">
                        <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />{issue}
                      </div>
                    ))}
                  </div>
                ) : null}
                {warnings.length > 0 ? (
                  <div className="space-y-2 rounded-xl border border-amber-500/20 bg-amber-500/8 p-3">
                    {warnings.map(warning => (
                      <div key={warning} className="flex items-start gap-2 text-xs leading-5 text-amber-300">
                        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />{warning}
                      </div>
                    ))}
                  </div>
                ) : null}
                {submitError ? (
                  <div className="rounded-xl border border-red-500/25 bg-red-500/10 px-3 py-2.5 text-xs leading-5 text-red-300">{submitError}</div>
                ) : null}

                <Button onClick={submit} disabled={!canSubmit} className="h-11 w-full">
                  {submitting ? (
                    <><Loader2 className="mr-2 h-4 w-4 animate-spin" />正在创建任务</>
                  ) : polling ? (
                    <><Orbit className="mr-2 h-4 w-4 animate-spin" />当前任务运行中</>
                  ) : (
                    <><Play className="mr-2 h-4 w-4" />启动注册任务</>
                  )}
                </Button>
                <p className="text-center text-[10px] leading-4 text-[var(--text-muted)]">启动后无需停留在页面；全局任务坞会保留运行状态。</p>
              </div>
            </CardContent>
          </Card>
        </aside>
      </div>

      {task && activeTaskId ? (
        <section className="space-y-4 scroll-mt-4" aria-live="polite">
          <Card>
            <CardContent className="p-5 sm:p-6">
              <div className="mb-5 flex flex-col justify-between gap-3 sm:flex-row sm:items-center">
                <div>
                  <div className="flex items-center gap-2">
                    <h2 className="text-base font-semibold text-[var(--text-primary)]">05 · 运行与结果</h2>
                    <Badge variant={TASK_STATUS_VARIANTS[task.status] || 'secondary'}>{getTaskStatusText(task.status, language)}</Badge>
                  </div>
                  <div className="mt-1 break-all font-mono text-[11px] text-[var(--text-muted)]">{activeTaskId}</div>
                </div>
                <Button variant="outline" asChild><Link to="/history"><History className="mr-2 h-4 w-4" />查看全部任务</Link></Button>
              </div>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
                {activeTaskStats.map(({ label, value, icon: Icon }) => (
                  <div key={label} className="rounded-xl border border-[var(--border-soft)] bg-[var(--chip-bg)] p-3">
                    <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.15em] text-[var(--text-muted)]"><Icon className="h-3.5 w-3.5" />{label}</div>
                    <div className="mt-2 text-sm font-semibold text-[var(--text-primary)]">{value}</div>
                  </div>
                ))}
              </div>
              {task.error || task.errors?.length ? (
                <div className="mt-4 space-y-2 rounded-xl border border-red-500/20 bg-red-500/8 p-3">
                  {[...(task.errors || []), ...(task.error ? [task.error] : [])].map((error: string, index: number) => (
                    <div key={`${index}-${error}`} className="flex items-start gap-2 text-xs leading-5 text-red-300"><XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />{error}</div>
                  ))}
                </div>
              ) : null}
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-5 sm:p-6">
              <div className="mb-4 flex items-center justify-between gap-3">
                <div>
                  <h3 className="text-sm font-semibold text-[var(--text-primary)]">实时执行日志</h3>
                  <p className="mt-1 text-xs text-[var(--text-muted)]">日志流同时负责刷新任务进度，不再额外启动页面轮询。</p>
                </div>
                <span className="flex items-center gap-1.5 text-[11px] text-[var(--text-muted)]"><Radio className="h-3.5 w-3.5" />LIVE</span>
              </div>
              <TaskLogPanel taskId={activeTaskId} onDone={handleTaskDone} onTaskUpdate={handleTaskUpdate} />
            </CardContent>
          </Card>
        </section>
      ) : null}
    </div>
  )
}
