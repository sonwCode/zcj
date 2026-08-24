import {
  type UIEvent,
  useCallback,
  useDeferredValue,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { Check, ChevronDown, Loader2, RefreshCw, Search, SlidersHorizontal } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { apiFetch } from '@/lib/utils'

export type SmsBowerPriceRow = {
  country: string
  name: string
  price: number
  price_cny: number
  count: number
  provider_count: number
  eligible: boolean
  providers?: Array<{
    provider_id?: string
    price?: number
    price_cny?: number
    count?: number
    stock?: number
    qty?: number
    rank?: string
  }>
  price_tiers?: Array<{
    price: number
    price_cny: number
    count: number
    provider_ids: string[]
  }>
}

export type SmsBowerSelectionValue = {
  country: string
  countries: string[]
  maxPriceUsd: string
  providerIdsByCountry: Record<string, string[]>
  minStock: number
  usdCnyRate: number
  providerRejectThreshold: number
  codeTimeoutSeconds: number
  phoneMaxAttempts: number
  noNumbersWaitSeconds: number
  tierCooldownMinutes: number
}

type SmsCountryOption = {
  id: string
  name: string
}

type CountrySortKey = 'recommended' | 'price' | 'stock' | 'providers' | 'name'
type ProviderFilter = 'all' | 'within_price' | 'gold' | 'selected'

type Props = {
  value: SmsBowerSelectionValue
  onChange: (patch: Partial<SmsBowerSelectionValue>) => void
  queryProxy?: string
  credentialsConfigured?: boolean
}

function providerRank(provider: { rank?: string }) {
  const raw = String(provider.rank || '').toLowerCase()
  if (raw.includes('gold')) return 'gold'
  if (raw.includes('silver')) return 'silver'
  if (raw.includes('copper') || raw.includes('bronze')) return 'copper'
  return 'standard'
}

function rankMeta(rank: string) {
  if (rank === 'gold') return { label: '黄金', className: 'border-amber-300/50 bg-amber-400/20 text-amber-300' }
  if (rank === 'silver') return { label: '银级', className: 'border-slate-300/50 bg-slate-300/20 text-slate-200' }
  if (rank === 'copper') return { label: '铜级', className: 'border-orange-400/50 bg-orange-500/20 text-orange-300' }
  return { label: '普通', className: 'border-cyan-400/30 bg-cyan-400/10 text-cyan-200' }
}

function isVirtualCountry(row: Pick<SmsBowerPriceRow, 'country' | 'name'> | undefined) {
  if (!row) return false
  return String(row.country) === '12' || /虚拟|virtual|voip/i.test(String(row.name || ''))
}

function providerPriceCny(provider: { price?: number; price_cny?: number }, usdCnyRate: number) {
  const explicit = Number(provider.price_cny)
  if (Number.isFinite(explicit) && explicit > 0) return explicit
  const usd = Number(provider.price)
  return Number.isFinite(usd) && usd > 0 ? usd * usdCnyRate : 0
}

function providerStock(provider: { count?: number; stock?: number; qty?: number }) {
  const stock = Number(provider.count ?? provider.stock ?? provider.qty)
  return Number.isFinite(stock) ? Math.max(stock, 0) : 0
}

function formatUsd(value: number) {
  return Number(value || 0).toFixed(4).replace(/0+$/, '').replace(/\.$/, '') || '0'
}

function uniqueStrings(values: unknown[]) {
  return Array.from(new Set(values.map(value => String(value || '').trim()).filter(Boolean)))
}

export function SmsBowerPriceSelector({
  value,
  onChange,
  queryProxy = '',
  credentialsConfigured = false,
}: Props) {
  const [priceRows, setPriceRows] = useState<SmsBowerPriceRow[]>([])
  const [countryOptions, setCountryOptions] = useState<SmsCountryOption[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [bulkPriceCny, setBulkPriceCny] = useState('1')
  const [search, setSearch] = useState('')
  const [countryFilter, setCountryFilter] = useState<'all' | 'within_price' | 'selected'>('all')
  const [countrySort, setCountrySort] = useState<CountrySortKey>('recommended')
  const [providerFilter, setProviderFilter] = useState<ProviderFilter>('all')
  const [expandedCountry, setExpandedCountry] = useState(value.country || '')
  const [renderLimit, setRenderLimit] = useState(15)
  const autoLoadedRef = useRef(false)
  const deferredSearch = useDeferredValue(search)

  const mergedCountryOptions = useMemo(() => {
    const options = new Map<string, SmsCountryOption>()
    countryOptions.forEach(option => options.set(String(option.id), option))
    priceRows.forEach(row => {
      const id = String(row.country)
      if (!options.has(id)) options.set(id, { id, name: row.name || id })
    })
    if (value.country && !options.has(value.country)) {
      options.set(value.country, { id: value.country, name: value.country })
    }
    return Array.from(options.values()).sort((left, right) => left.name.localeCompare(right.name, 'zh-CN'))
  }, [countryOptions, priceRows, value.country])

  const visibleRows = useMemo(() => {
    const keyword = deferredSearch.trim().toLowerCase()
    const maxPrice = Math.max(Number(value.maxPriceUsd || 0), 0)
    const minStock = Math.max(Number(value.minStock || 0), 0)
    return priceRows
      .filter(row => !isVirtualCountry(row))
      .filter(row => (
        !keyword
        || String(row.country).toLowerCase().includes(keyword)
        || String(row.name || '').toLowerCase().includes(keyword)
      ))
      .filter(row => {
        if (countryFilter === 'selected') return value.countries.includes(String(row.country))
        if (countryFilter === 'within_price') return maxPrice <= 0 || Number(row.price || 0) <= maxPrice
        return true
      })
      .sort((left, right) => {
        if (countrySort === 'price') return Number(left.price || 0) - Number(right.price || 0)
        if (countrySort === 'stock') return Number(right.count || 0) - Number(left.count || 0)
        if (countrySort === 'providers') {
          return Number(right.provider_count || right.providers?.length || 0)
            - Number(left.provider_count || left.providers?.length || 0)
        }
        if (countrySort === 'name') return String(left.name || left.country).localeCompare(String(right.name || right.country), 'zh-CN')
        const leftEligible = Number(left.count || 0) >= minStock && (maxPrice <= 0 || Number(left.price || 0) <= maxPrice)
        const rightEligible = Number(right.count || 0) >= minStock && (maxPrice <= 0 || Number(right.price || 0) <= maxPrice)
        if (leftEligible !== rightEligible) return Number(rightEligible) - Number(leftEligible)
        const priceComparison = Number(left.price || 0) - Number(right.price || 0)
        return priceComparison || Number(right.count || 0) - Number(left.count || 0)
      })
  }, [countryFilter, countrySort, deferredSearch, priceRows, value.countries, value.maxPriceUsd, value.minStock])

  const renderedRows = useMemo(() => visibleRows.slice(0, renderLimit), [renderLimit, visibleRows])

  const visibleProviders = useCallback((row: SmsBowerPriceRow) => {
    const selected = new Set(value.providerIdsByCountry[String(row.country)] || [])
    const maxPrice = Math.max(Number(value.maxPriceUsd || 0), 0)
    return [...(row.providers || [])]
      .filter(provider => {
        const providerId = String(provider.provider_id || '')
        if (providerFilter === 'selected') return selected.has(providerId)
        if (providerFilter === 'gold') return providerRank(provider) === 'gold'
        if (providerFilter === 'within_price') return maxPrice <= 0 || Number(provider.price || 0) <= maxPrice
        return true
      })
      .sort((left, right) => {
        const priceComparison = Number(left.price || 0) - Number(right.price || 0)
        return priceComparison || providerStock(right) - providerStock(left)
      })
  }, [providerFilter, value.maxPriceUsd, value.providerIdsByCountry])

  const loadPrices = useCallback(async () => {
    setLoading(true)
    setError('')
    setNotice('')
    try {
      try {
        const countryResult = await apiFetch('/sms/smsbower/countries')
        const countries: Array<Record<string, unknown>> = Array.isArray(countryResult?.countries)
          ? countryResult.countries
          : []
        setCountryOptions(countries
          .map(item => ({
            id: String(item?.id || item?.country || '').trim(),
            name: String(item?.chn || item?.eng || item?.name || item?.id || '').trim(),
          }))
          .filter((item: SmsCountryOption) => item.id))
      } catch {
        setCountryOptions([])
      }

      const result = await apiFetch('/sms/smsbower/top-countries', {
        method: 'POST',
        body: JSON.stringify({
          service: 'dr',
          proxy: queryProxy.trim(),
          min_stock: Math.max(Number(value.minStock || 0), 0),
          max_price: Math.max(Number(value.maxPriceUsd || 0), 0),
          top_n: 0,
          usd_cny_rate: Math.max(Number(value.usdCnyRate || 7.2), 0.01),
        }),
      })
      const rows = Array.isArray(result?.countries) ? result.countries : []
      setPriceRows(rows)
      if (rows.length === 0) setError('当前 OpenAI/ChatGPT 服务没有可租号码')
    } catch (loadError) {
      setPriceRows([])
      setError(loadError instanceof Error ? loadError.message : String(loadError || '查询价格失败'))
    } finally {
      setLoading(false)
    }
  }, [queryProxy, value.maxPriceUsd, value.minStock, value.usdCnyRate])

  useEffect(() => {
    if (autoLoadedRef.current) return
    autoLoadedRef.current = true
    void loadPrices()
  }, [loadPrices])

  useEffect(() => {
    setRenderLimit(15)
  }, [countryFilter, countrySort, deferredSearch, priceRows])

  useEffect(() => {
    const virtualCountries = new Set(priceRows.filter(isVirtualCountry).map(row => String(row.country)))
    if (virtualCountries.size === 0 || !value.countries.some(country => virtualCountries.has(country))) return
    const countries = value.countries.filter(country => !virtualCountries.has(country))
    const providerIdsByCountry = Object.fromEntries(
      Object.entries(value.providerIdsByCountry).filter(([country]) => !virtualCountries.has(country)),
    )
    onChange({
      country: countries[0] || '',
      countries,
      providerIdsByCountry,
    })
  }, [onChange, priceRows, value.countries, value.providerIdsByCountry])

  const selectCountry = (row: SmsBowerPriceRow) => {
    if (isVirtualCountry(row)) {
      setError(`${row.name || row.country} 属于虚拟/VOIP 号码国家，已阻止选择`)
      return
    }
    const country = String(row.country)
    const selecting = !value.countries.includes(country)
    const countries = selecting
      ? uniqueStrings([...value.countries, country])
      : value.countries.filter(item => item !== country)
    const providerIdsByCountry = { ...value.providerIdsByCountry }
    if (!selecting) delete providerIdsByCountry[country]
    const patch: Partial<SmsBowerSelectionValue> = {
      country: selecting ? country : countries[0] || '',
      countries,
      providerIdsByCountry,
    }
    if (selecting && Number(row.price || 0) > Number(value.maxPriceUsd || 0)) {
      patch.maxPriceUsd = formatUsd(Number(row.price || 0))
    }
    setExpandedCountry(country)
    setError('')
    onChange(patch)
  }

  const toggleProvider = (
    row: SmsBowerPriceRow,
    provider: NonNullable<SmsBowerPriceRow['providers']>[number],
  ) => {
    if (isVirtualCountry(row)) {
      setError(`${row.name || row.country} 属于虚拟/VOIP 号码国家，已阻止选择该档位`)
      return
    }
    const country = String(row.country)
    const providerId = String(provider.provider_id || '').trim()
    if (!providerId) return
    const currentIds = new Set(value.providerIdsByCountry[country] || [])
    const selecting = !currentIds.has(providerId)
    if (selecting) currentIds.add(providerId)
    else currentIds.delete(providerId)
    const providerIdsByCountry = { ...value.providerIdsByCountry }
    if (currentIds.size > 0) providerIdsByCountry[country] = Array.from(currentIds)
    else delete providerIdsByCountry[country]
    const patch: Partial<SmsBowerSelectionValue> = {
      country,
      countries: uniqueStrings([...value.countries, country]),
      providerIdsByCountry,
    }
    if (selecting && Number(provider.price || 0) > Number(value.maxPriceUsd || 0)) {
      patch.maxPriceUsd = formatUsd(Number(provider.price || 0))
    }
    setExpandedCountry(country)
    setError('')
    onChange(patch)
  }

  const selectProvidersBelowCny = () => {
    const limit = Number(bulkPriceCny)
    if (!Number.isFinite(limit) || limit <= 0) {
      setNotice('请输入大于 0 的人民币单价上限')
      return
    }
    const countries: string[] = []
    const providerIdsByCountry: Record<string, string[]> = {}
    let providerCount = 0
    let highestUsd = 0
    const rate = Math.max(Number(value.usdCnyRate || 7.2), 0.01)

    priceRows.forEach(row => {
      if (isVirtualCountry(row)) return
      const ids: string[] = []
      ;(row.providers || []).forEach(provider => {
        const id = String(provider.provider_id || '').trim()
        const priceCny = providerPriceCny(provider, rate)
        const stock = providerStock(provider)
        if (!id || priceCny <= 0 || priceCny > limit || stock <= 0) return
        ids.push(id)
        highestUsd = Math.max(highestUsd, Number(provider.price || 0))
      })
      const uniqueIds = uniqueStrings(ids)
      if (uniqueIds.length === 0) return
      const country = String(row.country)
      countries.push(country)
      providerIdsByCountry[country] = uniqueIds
      providerCount += uniqueIds.length
    })

    if (countries.length === 0) {
      setNotice(`没有找到低于等于 ¥${limit.toFixed(2)} 且有库存的实体号码档位`)
      return
    }

    const patch: Partial<SmsBowerSelectionValue> = {
      country: countries[0],
      countries,
      providerIdsByCountry,
    }
    if (highestUsd > Number(value.maxPriceUsd || 0)) patch.maxPriceUsd = formatUsd(highestUsd)
    setExpandedCountry(countries[0])
    setError('')
    setNotice(`已按 ¥${limit.toFixed(2)} 上限选择 ${countries.length} 个国家、${providerCount} 个有库存档位`)
    onChange(patch)
  }

  const handleListScroll = (event: UIEvent<HTMLDivElement>) => {
    const target = event.currentTarget
    if (target.scrollHeight - target.scrollTop - target.clientHeight > 120) return
    setRenderLimit(current => Math.min(current + 15, visibleRows.length))
  }

  return (
    <div className="mt-4 overflow-hidden rounded-xl border border-cyan-400/25 bg-cyan-400/[0.045]">
      <div className="border-b border-cyan-400/15 px-4 py-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-semibold text-[var(--text-primary)]">SMSBower 实时价格与档位</span>
              <Badge variant="secondary">OpenAI · dr</Badge>
              <Badge variant={credentialsConfigured ? 'success' : 'warning'}>
                {credentialsConfigured ? '已使用设置页凭据' : '凭据待确认'}
              </Badge>
            </div>
            <p className="mt-1 text-xs leading-5 text-[var(--text-muted)]">
              先按实时库存选择国家，再按需限定 Provider 档位；未选择具体档位时使用该国家价格上限内的可用档位。
            </p>
          </div>
          <button
            type="button"
            onClick={() => void loadPrices()}
            disabled={loading}
            className="inline-flex h-9 items-center gap-2 rounded-lg border border-cyan-400/30 bg-cyan-400/10 px-3 text-xs font-medium text-cyan-200 transition-colors hover:bg-cyan-400/15 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
            {loading ? '查询中…' : '刷新实时价格'}
          </button>
        </div>
      </div>

      <div className="space-y-4 p-4">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <label className="block min-w-0">
            <span className="mb-1.5 block text-xs font-medium text-[var(--text-secondary)]">默认接码国家</span>
            <select
              value={value.country}
              onChange={(event) => {
                const country = event.target.value
                if (!country) return
                onChange({
                  country,
                  countries: uniqueStrings([...value.countries, country]),
                })
                setExpandedCountry(country)
              }}
              className="control-surface appearance-none"
            >
              {mergedCountryOptions.length === 0 ? <option value={value.country}>{value.country || '正在加载…'}</option> : null}
              {mergedCountryOptions.map(option => {
                const row = priceRows.find(item => String(item.country) === option.id)
                const virtual = isVirtualCountry(row || { country: option.id, name: option.name } as SmsBowerPriceRow)
                return (
                  <option key={option.id} value={option.id} disabled={virtual}>
                    {option.name} · {option.id}{row ? ` · $${Number(row.price || 0).toFixed(4)} · 库存 ${Number(row.count || 0).toLocaleString()}` : ''}{virtual ? ' · 虚拟/VOIP' : ''}
                  </option>
                )
              })}
            </select>
          </label>

          <label className="block min-w-0">
            <span className="mb-1.5 block text-xs font-medium text-[var(--text-secondary)]">单号最高价格（USD）</span>
            <input
              value={value.maxPriceUsd}
              onChange={(event) => onChange({ maxPriceUsd: event.target.value.replace(/[^0-9.]/g, '').slice(0, 8) })}
              inputMode="decimal"
              className="control-surface font-mono"
              placeholder="0.13"
            />
          </label>

          <label className="block min-w-0">
            <span className="mb-1.5 block text-xs font-medium text-[var(--text-secondary)]">单号验证码等待</span>
            <select
              value={value.codeTimeoutSeconds}
              onChange={(event) => onChange({ codeTimeoutSeconds: Number(event.target.value) })}
              className="control-surface appearance-none"
            >
              <option value={180}>3 分钟</option>
              <option value={240}>4 分钟</option>
              <option value={300}>5 分钟</option>
            </select>
          </label>

          <label className="block min-w-0">
            <span className="mb-1.5 block text-xs font-medium text-[var(--text-secondary)]">单账号换号上限</span>
            <select
              value={value.phoneMaxAttempts}
              onChange={(event) => onChange({ phoneMaxAttempts: Number(event.target.value) })}
              className="control-surface appearance-none"
            >
              <option value={3}>3 个号码</option>
              <option value={5}>5 个号码</option>
              <option value={8}>8 个号码</option>
              <option value={12}>12 个号码</option>
            </select>
          </label>
        </div>

        <details className="group rounded-lg border border-[var(--border-soft)] bg-[var(--chip-bg)]">
          <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-3 py-2.5 text-xs font-medium text-[var(--text-secondary)]">
            <span className="flex items-center gap-2"><SlidersHorizontal className="h-3.5 w-3.5" />库存、汇率与失败档位策略</span>
            <ChevronDown className="h-3.5 w-3.5 transition-transform group-open:rotate-180" />
          </summary>
          <div className="grid gap-3 border-t border-[var(--border-soft)] p-3 sm:grid-cols-2 lg:grid-cols-5">
            <label className="text-[11px] text-[var(--text-muted)]">
              最低库存
              <input
                type="number"
                min={0}
                value={value.minStock}
                onChange={(event) => onChange({ minStock: Math.max(Number(event.target.value || 0), 0) })}
                className="control-surface mt-1"
              />
            </label>
            <label className="text-[11px] text-[var(--text-muted)]">
              USD/CNY 汇率
              <input
                value={value.usdCnyRate}
                onChange={(event) => onChange({ usdCnyRate: Math.max(Number(event.target.value || 0), 0.01) })}
                inputMode="decimal"
                className="control-surface mt-1"
              />
            </label>
            <label className="text-[11px] text-[var(--text-muted)]">
              暂无号码时等待
              <select
                value={value.noNumbersWaitSeconds}
                onChange={(event) => onChange({ noNumbersWaitSeconds: Number(event.target.value) })}
                className="control-surface mt-1 appearance-none"
              >
                <option value={0}>立即切换/结束</option>
                <option value={60}>等待 1 分钟</option>
                <option value={120}>等待 2 分钟</option>
                <option value={180}>等待 3 分钟</option>
                <option value={300}>等待 5 分钟</option>
              </select>
            </label>
            <label className="text-[11px] text-[var(--text-muted)]">
              失败档位冷却
              <select
                value={value.tierCooldownMinutes}
                onChange={(event) => onChange({ tierCooldownMinutes: Number(event.target.value) })}
                className="control-surface mt-1 appearance-none"
              >
                <option value={30}>30 分钟</option>
                <option value={45}>45 分钟</option>
                <option value={60}>60 分钟</option>
              </select>
            </label>
            <label className="text-[11px] text-[var(--text-muted)]">
              Provider 拒绝阈值
              <select
                value={value.providerRejectThreshold}
                onChange={(event) => onChange({ providerRejectThreshold: Number(event.target.value) })}
                className="control-surface mt-1 appearance-none"
              >
                <option value={1}>1 次</option>
                <option value={2}>2 次</option>
                <option value={3}>3 次</option>
              </select>
            </label>
          </div>
        </details>

        {error ? <div className="rounded-lg border border-red-500/20 bg-red-500/8 px-3 py-2 text-xs text-red-300">{error}</div> : null}

        <div className="space-y-3 rounded-xl border border-[var(--border-soft)] bg-[var(--chip-bg)] p-3">
          <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
            <div className="flex min-w-0 flex-wrap items-center gap-1.5 text-xs">
              <span className="mr-1 text-[var(--text-muted)]">候选国家</span>
              {value.countries.length > 0 ? value.countries.map(country => {
                const row = priceRows.find(item => String(item.country) === country)
                return (
                  <button
                    key={country}
                    type="button"
                    onClick={() => row ? selectCountry(row) : onChange({
                      countries: value.countries.filter(item => item !== country),
                      country: value.countries.find(item => item !== country) || '',
                    })}
                    className="rounded-full border border-cyan-400/30 bg-cyan-400/10 px-2 py-1 text-[11px] text-cyan-200"
                    title="点击移除"
                  >
                    {row?.name || country} · {country} ×
                  </button>
                )
              }) : <span className="text-[11px] text-amber-300">请至少选择一个有库存国家</span>}
            </div>
            <label className="relative block w-full lg:w-60">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--text-muted)]" />
              <input
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="搜索国家名称或 ID"
                className="control-surface pl-9 text-xs"
              />
            </label>
          </div>

          <div className="grid gap-2 border-t border-[var(--border-soft)] pt-3 sm:grid-cols-2 lg:grid-cols-[1fr_1fr_1fr_auto]">
            <label className="text-[10px] text-[var(--text-muted)]">
              国家筛选
              <select
                value={countryFilter}
                onChange={(event) => setCountryFilter(event.target.value as typeof countryFilter)}
                className="control-surface mt-1 appearance-none text-xs"
              >
                <option value="all">全部国家</option>
                <option value="within_price">价格上限内</option>
                <option value="selected">仅候选国家</option>
              </select>
            </label>
            <label className="text-[10px] text-[var(--text-muted)]">
              国家排序
              <select
                value={countrySort}
                onChange={(event) => setCountrySort(event.target.value as CountrySortKey)}
                className="control-surface mt-1 appearance-none text-xs"
              >
                <option value="recommended">综合推荐</option>
                <option value="price">最低价格</option>
                <option value="stock">总库存</option>
                <option value="providers">供应商数量</option>
                <option value="name">国家名称</option>
              </select>
            </label>
            <label className="text-[10px] text-[var(--text-muted)]">
              档位筛选
              <select
                value={providerFilter}
                onChange={(event) => setProviderFilter(event.target.value as ProviderFilter)}
                className="control-surface mt-1 appearance-none text-xs"
              >
                <option value="all">全部档位</option>
                <option value="within_price">价格上限内</option>
                <option value="gold">仅黄金档</option>
                <option value="selected">仅已选择</option>
              </select>
            </label>
            <div className="flex items-end gap-2">
              <label className="min-w-0 text-[10px] text-[var(--text-muted)]">
                人民币批量选择
                <div className="mt-1 flex h-10 items-center gap-1 rounded-md border border-[var(--border-soft)] bg-[var(--bg-pane)] px-2 text-xs text-[var(--text-secondary)]">
                  <span>≤ ¥</span>
                  <input
                    value={bulkPriceCny}
                    onChange={(event) => setBulkPriceCny(event.target.value.replace(/[^0-9.]/g, '').slice(0, 8))}
                    onKeyDown={(event) => {
                      if (event.key !== 'Enter') return
                      event.preventDefault()
                      selectProvidersBelowCny()
                    }}
                    inputMode="decimal"
                    className="w-14 bg-transparent text-center font-mono text-xs text-[var(--text-primary)] outline-none"
                    aria-label="人民币批量选择价格上限"
                  />
                </div>
              </label>
              <button
                type="button"
                onClick={selectProvidersBelowCny}
                disabled={priceRows.length === 0}
                className="inline-flex h-10 shrink-0 items-center gap-1.5 rounded-md border border-emerald-400/35 bg-emerald-400/10 px-3 text-xs text-emerald-300 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <Check className="h-3.5 w-3.5" />全选
              </button>
            </div>
          </div>

          {notice ? <div className="text-[11px] text-emerald-300" role="status">{notice}</div> : null}
          <div className="text-[10px] text-[var(--text-muted)]">
            实时报价 {priceRows.length} 个国家 · 当前匹配 {visibleRows.length} 个 · 已选 {value.countries.length} 个国家 / {Object.values(value.providerIdsByCountry).reduce((total, ids) => total + ids.length, 0)} 个指定档位
          </div>
        </div>

        {priceRows.length > 0 ? (
          <div
            className="max-h-[420px] space-y-2 overflow-auto rounded-xl border border-[var(--border-soft)] bg-black/10 p-2"
            onScroll={handleListScroll}
          >
            {renderedRows.map(row => {
              const country = String(row.country)
              const selected = value.countries.includes(country)
              const expanded = expandedCountry === country
              const providers = expanded ? visibleProviders(row) : []
              return (
                <div key={country} className={`overflow-hidden rounded-lg border ${selected ? 'border-cyan-400/50 bg-cyan-400/[0.07]' : 'border-[var(--border-soft)] bg-[var(--bg-pane)]/55'}`}>
                  <div className="grid grid-cols-[28px_minmax(0,1fr)_auto] items-center gap-2 px-3 py-2.5">
                    <button
                      type="button"
                      role="checkbox"
                      aria-checked={selected}
                      aria-label={`选择国家 ${row.name} ${country}`}
                      onClick={() => selectCountry(row)}
                      className={`flex h-5 w-5 items-center justify-center rounded border ${selected ? 'border-cyan-400 bg-cyan-400 text-slate-950' : 'border-[var(--border-soft)] bg-[var(--bg-input)]'}`}
                    >
                      <Check className={`h-3.5 w-3.5 ${selected ? 'opacity-100' : 'opacity-0'}`} strokeWidth={3} />
                    </button>
                    <button type="button" onClick={() => setExpandedCountry(expanded ? '' : country)} className="min-w-0 text-left">
                      <div className="truncate text-sm font-medium text-[var(--text-primary)]">{row.name} · {country}</div>
                      <div className="mt-0.5 text-[11px] text-[var(--text-muted)]">
                        库存 {Number(row.count || 0).toLocaleString()} · {row.provider_count || row.providers?.length || 0} 个 Provider · 最低 ${Number(row.price || 0).toFixed(4)} / ¥{Number(row.price_cny || 0).toFixed(2)}
                      </div>
                    </button>
                    <button
                      type="button"
                      onClick={() => setExpandedCountry(expanded ? '' : country)}
                      className="rounded-md border border-[var(--border-soft)] px-2 py-1 text-[11px] text-[var(--text-secondary)]"
                    >
                      {expanded ? '收起' : '展开档位'}
                    </button>
                  </div>

                  {expanded ? (
                    <div className="overflow-x-auto border-t border-[var(--border-soft)] px-2 pb-2">
                      <div className="min-w-[590px]">
                        <div className="grid grid-cols-[64px_72px_90px_96px_96px_40px] gap-2 px-2 py-2 text-[10px] text-[var(--text-muted)]">
                          <span>等级</span><span>ID</span><span>库存</span><span>美元</span><span>人民币</span><span>选</span>
                        </div>
                        <div className="space-y-1">
                          {providers.map((provider, index) => {
                            const providerId = String(provider.provider_id || '')
                            const providerSelected = (value.providerIdsByCountry[country] || []).includes(providerId)
                            const meta = rankMeta(providerRank(provider))
                            return (
                              <button
                                key={`${country}-${providerId || index}`}
                                type="button"
                                role="checkbox"
                                aria-checked={providerSelected}
                                onClick={() => toggleProvider(row, provider)}
                                className={`grid w-full grid-cols-[64px_72px_90px_96px_96px_40px] items-center gap-2 rounded-md border px-2 py-1.5 text-left text-[11px] ${providerSelected ? 'border-cyan-400/45 bg-cyan-400/10' : 'border-transparent bg-black/10 hover:border-[var(--border-soft)]'}`}
                              >
                                <span className={`rounded-full border px-1.5 py-0.5 text-center text-[10px] ${meta.className}`}>{meta.label}</span>
                                <span className="font-mono text-[var(--text-secondary)]">{providerId || '-'}</span>
                                <span className="text-[var(--text-secondary)]">{providerStock(provider).toLocaleString()}</span>
                                <span className="font-mono font-semibold text-emerald-300">${Number(provider.price || 0).toFixed(4)}</span>
                                <span className="font-mono text-[var(--text-secondary)]">¥{providerPriceCny(provider, value.usdCnyRate).toFixed(2)}</span>
                                <span className={`flex h-5 w-5 items-center justify-center rounded border ${providerSelected ? 'border-cyan-400 bg-cyan-400 text-slate-950' : 'border-[var(--border-soft)] bg-[var(--bg-input)]'}`}>
                                  <Check className={`h-3.5 w-3.5 ${providerSelected ? 'opacity-100' : 'opacity-0'}`} strokeWidth={3} />
                                </span>
                              </button>
                            )
                          })}
                          {providers.length === 0 ? (
                            <div className="rounded-md border border-dashed border-[var(--border-soft)] px-3 py-4 text-center text-[11px] text-[var(--text-muted)]">
                              当前筛选条件下没有档位
                            </div>
                          ) : null}
                        </div>
                      </div>
                    </div>
                  ) : null}
                </div>
              )
            })}
            {renderedRows.length < visibleRows.length ? (
              <button
                type="button"
                onClick={() => setRenderLimit(current => Math.min(current + 15, visibleRows.length))}
                className="w-full rounded-lg border border-dashed border-[var(--border-soft)] py-2 text-xs text-[var(--text-muted)] hover:border-cyan-400/35 hover:text-cyan-200"
              >
                继续加载（剩余 {visibleRows.length - renderedRows.length} 个国家）
              </button>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  )
}
