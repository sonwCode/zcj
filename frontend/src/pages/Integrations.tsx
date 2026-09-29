import { useEffect, useState } from 'react'
import { Cable, CheckCircle, Wrench } from 'lucide-react'

import { Card } from '@/components/ui/card'
import { apiFetch } from '@/lib/utils'


export default function Integrations() {
  const [data, setData] = useState<Record<string, any>>({})
  useEffect(() => { void apiFetch('/management/integrations').then(setData) }, [])
  const labels: Record<string, string> = { registration_engine: 'aBai ChatGPT 注册', legacy_registration: '其他平台注册', mihomo: 'Mihomo', bitbrowser: 'BitBrowser', sms_blacklist: '号码黑名单' }
  return <div className="space-y-4"><div><h1 className="flex items-center gap-2 text-xl font-semibold text-[var(--text-primary)]"><Cable className="h-5 w-5 text-sky-400"/>功能适配</h1><p className="mt-1 text-sm text-[var(--text-muted)]">清楚区分已迁移能力与仍需外部实现的旧功能，避免混入缺失或未审查代码。</p></div><div className="grid gap-3 md:grid-cols-2">{Object.entries(data).map(([key, item]) => { const ready = ['ready', 'configured'].includes(item.status); return <Card key={key} className="p-4"><div className="flex items-start gap-3">{ready ? <CheckCircle className="mt-0.5 h-5 w-5 text-emerald-400"/> : <Wrench className="mt-0.5 h-5 w-5 text-amber-400"/>}<div><div className="font-medium text-[var(--text-primary)]">{labels[key] || key}</div><div className="mt-1 text-sm text-[var(--text-muted)]">{item.message || (ready ? '已接入' : '尚未配置')}</div><div className="mt-2 font-mono text-xs text-[var(--text-muted)]">{item.status}</div></div></div></Card> })}</div></div>
}
