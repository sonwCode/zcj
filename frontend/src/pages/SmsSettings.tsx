import { useCallback, useEffect, useState } from 'react'
import ProviderCards from '@/components/settings/ProviderCards'
import { getConfigOptions } from '@/lib/app-data'
import type { ProviderOption, ProviderSetting } from '@/lib/config-options'
import { useI18n } from '@/lib/i18n-context'

export default function SmsSettings() {
  const { t } = useI18n()
  const [catalog, setCatalog] = useState<ProviderOption[]>([])
  const [settings, setSettings] = useState<ProviderSetting[]>([])
  const [error, setError] = useState('')
  const load = useCallback(async () => {
    try {
      const options = await getConfigOptions()
      setCatalog(options.sms_providers || [])
      setSettings(options.sms_settings || [])
      setError('')
    } catch {
      setCatalog([])
      setSettings([])
      setError(t('register.providerMetadataError'))
    }
  }, [t])
  useEffect(() => { void load() }, [load])
  return (
    <div className="space-y-4">
      {error && <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-300">{error}</div>}
      <div className="rounded-lg border border-[var(--accent-edge)] bg-[var(--accent-soft)] px-4 py-3 text-sm text-[var(--text-secondary)]">
        短信接码仅用于 ChatGPT 注册的 add_phone 验证。测试按钮只查询余额，不会租用手机号。
      </div>
      <ProviderCards providerType="sms" catalog={catalog} settings={settings} onReload={load} />
    </div>
  )
}
