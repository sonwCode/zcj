import { useEffect, useState } from 'react'

import { apiFetch, cn } from '@/lib/utils'

function parseIsoTime(value: unknown) {
  if (!value) return null
  const parsed = new Date(String(value)).getTime()
  return Number.isFinite(parsed) ? parsed : null
}

export function SchedulerHealth({
  compact = false,
  className,
}: {
  compact?: boolean
  className?: string
}) {
  const [status, setStatus] = useState<any>(null)
  const [loadError, setLoadError] = useState('')
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    let active = true
    let timer: number | undefined
    const loadStatus = async () => {
      try {
        const payload = await apiFetch('/system/scheduler/status')
        if (!active) return
        setStatus(payload)
        setLoadError('')
      } catch (error: any) {
        if (!active) return
        setLoadError(error?.message || '无法读取检测器状态')
      } finally {
        if (active) timer = window.setTimeout(loadStatus, 5_000)
      }
    }
    void loadStatus()
    const clock = window.setInterval(() => setNow(Date.now()), 1_000)
    return () => {
      active = false
      if (timer) window.clearTimeout(timer)
      window.clearInterval(clock)
    }
  }, [])

  const heartbeatAt = parseIsoTime(status?.heartbeat_at)
  const heartbeatAgeSeconds = heartbeatAt === null
    ? null
    : Math.max(0, Math.floor((now - heartbeatAt) / 1000))
  const cycle = status?.probation_cycle || {}
  const result = cycle?.last_result || {}
  const intervalSeconds = Number(status?.continuous_check_interval_seconds || 60)
  const healthy = Boolean(
    status?.running
    && status?.thread_alive
    && heartbeatAgeSeconds !== null
    && heartbeatAgeSeconds <= 20,
  )
  const hasError = Boolean(loadError || cycle?.last_error || (status && !healthy))
  const label = cycle?.running
    ? '正在执行持续复检'
    : hasError
      ? '持续复检异常'
      : status
        ? `${intervalSeconds} 秒持续复检正常`
        : '正在读取复检状态'
  const detail = cycle?.running
    ? `本轮到期 ${Number(result?.due || 0)} 个账号`
    : hasError
      ? String(loadError || cycle?.last_error || `调度心跳已中断 ${heartbeatAgeSeconds ?? '-'} 秒`)
      : `监控 ${Number(result?.active_monitors || 0)} 个 · 上轮 ${Number(result?.checked || 0)} 个 · 延迟 ${Number(result?.max_lag_seconds || 0)} 秒`
  const title = [
    `检测周期：${intervalSeconds} 秒`,
    `队列扫描：${Number(status?.probation_scan_interval_seconds || 5)} 秒`,
    heartbeatAgeSeconds === null ? '尚无调度心跳' : `调度心跳：${heartbeatAgeSeconds} 秒前`,
    cycle?.last_completed_at ? `上轮完成：${new Date(cycle.last_completed_at).toLocaleString()}` : '等待首轮完成',
    cycle?.last_error || loadError,
  ].filter(Boolean).join('\n')

  if (compact) {
    return (
      <div
        className={cn(
          'flex h-9 w-9 items-center justify-center rounded-xl border',
          hasError
            ? 'border-red-500/25 bg-red-500/10'
            : 'border-emerald-500/20 bg-emerald-500/10',
          className,
        )}
        title={`${label}\n${detail}\n${title}`}
        role="status"
        aria-label={label}
      >
        <span className={cn('h-2.5 w-2.5 rounded-full', hasError ? 'bg-red-400' : cycle?.running ? 'bg-sky-400' : 'bg-emerald-400')} />
      </div>
    )
  }

  return (
    <div
      className={cn(
        'flex min-w-[220px] items-center gap-2 rounded-xl border px-2.5 py-2',
        hasError
          ? 'border-red-500/25 bg-red-500/10'
          : 'border-emerald-500/20 bg-emerald-500/10',
        className,
      )}
      title={title}
      role="status"
      aria-live="polite"
      aria-label={label}
    >
      <span className={cn('h-2 w-2 shrink-0 rounded-full', hasError ? 'bg-red-400' : cycle?.running ? 'bg-sky-400' : 'bg-emerald-400')} />
      <div className="min-w-0 flex-1 leading-none">
        <div className={cn('truncate text-[11px] font-semibold', hasError ? 'text-red-300' : 'text-emerald-300')}>{label}</div>
        <div className="mt-1.5 truncate text-[10px] text-[var(--text-muted)]">{detail}</div>
      </div>
    </div>
  )
}
