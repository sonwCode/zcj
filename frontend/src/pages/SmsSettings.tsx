import { useCallback, useEffect, useState } from 'react'
import { Smartphone, Settings2, TestTube, Star, CheckCircle, XCircle } from 'lucide-react'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { apiFetch } from '@/lib/utils'
import { getConfigOptions, invalidateConfigOptionsCache } from '@/lib/app-data'
import type { ProviderOption, ProviderSetting } from '@/lib/config-options'

export default function SmsSettings() {
  const [catalog, setCatalog] = useState<ProviderOption[]>([])
  const [settings, setSettings] = useState<ProviderSetting[]>([])
  const [error, setError] = useState('')
  const [searchTerm, setSearchTerm] = useState('')
  const [editingProvider, setEditingProvider] = useState<string | null>(null)
  const [formData, setFormData] = useState<Record<string, string>>({})
  const [testingProvider, setTestingProvider] = useState<string | null>(null)
  const [testResults, setTestResults] = useState<Map<string, { success: boolean; message: string }>>(new Map())

  const load = useCallback(async () => {
    try {
      const options = await getConfigOptions()
      setCatalog(options.sms_providers || [])
      setSettings(options.sms_settings || [])
      setError('')
    } catch {
      setCatalog([])
      setSettings([])
      setError('加载接码服务配置失败')
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const settingsMap = new Map(settings.map(s => [s.provider_key, s]))

  const filteredCatalog = catalog.filter(p => {
    if (!p.provider_key) return false
    if (!searchTerm) return true
    const term = searchTerm.toLowerCase()
    return p.label.toLowerCase().includes(term) ||
      p.provider_key.toLowerCase().includes(term) ||
      (p.description || '').toLowerCase().includes(term)
  })

  const handleEdit = (providerKey: string) => {
    const definition = catalog.find(p => p.provider_key === providerKey)
    const setting = settingsMap.get(providerKey)
    if (!definition) return

    const initial: Record<string, string> = {}
    definition.fields?.forEach(field => {
      if (field.category === 'auth') {
        initial[field.key] = field.secret ? '' : (setting?.auth?.[field.key] || '')
      } else if (field.category === 'config') {
        initial[field.key] = String(setting?.config?.[field.key] ?? (field as any).default ?? '')
      }
    })
    setFormData(initial)
    setEditingProvider(providerKey)
  }

  const handleSave = async () => {
    if (!editingProvider) return
    const definition = catalog.find(p => p.provider_key === editingProvider)
    if (!definition) return

    const auth: Record<string, string> = {}
    const config: Record<string, string> = {}

    definition.fields?.forEach(field => {
      const value = formData[field.key]
      if (field.category === 'auth') {
        if (value) auth[field.key] = value
      } else if (field.category === 'config') {
        if (value) config[field.key] = value
      }
    })

    try {
      await apiFetch(`/api/provider-settings/${editingProvider}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ auth, config })
      })
      await invalidateConfigOptionsCache()
      await load()
      setEditingProvider(null)
      setFormData({})
    } catch (err) {
      alert('保存失败: ' + String(err))
    }
  }

  const handleTest = async (providerKey: string) => {
    setTestingProvider(providerKey)
    try {
      const result = await apiFetch(`/api/provider-settings/${providerKey}/test`, { method: 'POST' })
      setTestResults(new Map(testResults.set(providerKey, { success: true, message: result.message || '测试成功' })))
    } catch (err) {
      setTestResults(new Map(testResults.set(providerKey, { success: false, message: String(err) })))
    } finally {
      setTestingProvider(null)
    }
  }

  const handleToggle = async (providerKey: string, enabled: boolean) => {
    try {
      await apiFetch(`/api/provider-settings/${providerKey}/toggle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled })
      })
      await invalidateConfigOptionsCache()
      await load()
    } catch (err) {
      alert('切换失败: ' + String(err))
    }
  }

  const handleSetDefault = async (providerKey: string) => {
    try {
      await apiFetch(`/api/provider-settings/${providerKey}/default`, { method: 'POST' })
      await invalidateConfigOptionsCache()
      await load()
    } catch (err) {
      alert('设为默认失败: ' + String(err))
    }
  }

  if (editingProvider) {
    const definition = catalog.find(p => p.provider_key === editingProvider)
    if (!definition) return null

    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-[var(--text-primary)]">配置 {definition.label}</h2>
          <div className="flex gap-2">
            <Button variant="outline" onClick={() => setEditingProvider(null)}>取消</Button>
            <Button onClick={handleSave}>保存</Button>
          </div>
        </div>

        <Card className="p-6">
          <div className="space-y-4">
            {definition.fields?.map(field => (
              <div key={field.key}>
                <label className="block text-sm font-medium text-[var(--text-primary)] mb-2">
                  {field.label}
                  {(field as any).required && <span className="text-red-500 ml-1">*</span>}
                </label>
                <input
                  type={field.secret ? 'password' : 'text'}
                  value={formData[field.key] || ''}
                  onChange={e => setFormData({ ...formData, [field.key]: e.target.value })}
                  placeholder={field.placeholder || field.label}
                  className="w-full px-3 py-2 bg-[var(--bg-input)] border border-[var(--border)] rounded-md text-[var(--text-primary)] focus:outline-none focus:ring-2 focus:ring-[var(--accent)]"
                />
                {(field as any).description && (
                  <p className="mt-1 text-sm text-[var(--text-muted)]">{(field as any).description}</p>
                )}
              </div>
            ))}
          </div>
        </Card>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* 页面标题 */}
      <div>
        <h1 className="flex items-center gap-2 text-xl font-semibold text-[var(--text-primary)]">
          <Smartphone className="h-5 w-5 text-sky-400" />
          接码服务
        </h1>
        <p className="mt-1 text-sm text-[var(--text-muted)]">
          配置手机验证码接收服务，支持多个供应商
        </p>
      </div>

      {error && (
        <div className="rounded border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-300">
          {error}
        </div>
      )}

      {/* 搜索栏 */}
      <div className="flex items-center gap-3">
        <input
          type="text"
          value={searchTerm}
          onChange={e => setSearchTerm(e.target.value)}
          placeholder="搜索服务..."
          className="flex-1 px-4 py-2 bg-[var(--bg-input)] border border-[var(--border)] rounded-md text-[var(--text-primary)] placeholder-[var(--text-muted)] focus:outline-none focus:ring-2 focus:ring-[var(--accent)]"
        />
        <div className="text-sm text-[var(--text-muted)]">
          {filteredCatalog.filter(p => p.provider_key && settingsMap.has(p.provider_key)).length} / {filteredCatalog.length}
        </div>
      </div>

      {/* 卡片网格 */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {filteredCatalog.map(provider => {
          if (!provider.provider_key) return null
          const setting = settingsMap.get(provider.provider_key)
          const isEnabled = setting?.enabled ?? false
          const isDefault = setting?.is_default ?? false
          const isConfigured = setting ? (
            (setting.auth && Object.keys(setting.auth).length > 0) ||
            (setting.config && Object.keys(setting.config).length > 0)
          ) : false
          const isTesting = testingProvider === provider.provider_key
          const testResult = testResults.get(provider.provider_key)

          return (
            <Card key={provider.provider_key} className="p-5">
              <div className="flex items-start justify-between mb-4">
                <div className="flex items-center gap-3">
                  <div className="p-2 rounded-lg bg-[var(--bg-card)] border border-[var(--border)]">
                    <Smartphone className="h-5 w-5 text-sky-400" />
                  </div>
                  <div>
                    <div className="font-medium text-[var(--text-primary)]">{provider.label}</div>
                    {provider.description && (
                      <div className="text-sm text-[var(--text-muted)] mt-0.5">{provider.description}</div>
                    )}
                  </div>
                </div>
                <label className="relative inline-flex items-center cursor-pointer">
                  <input
                    type="checkbox"
                    checked={isEnabled}
                    onChange={e => handleToggle(provider.provider_key!, e.target.checked)}
                    className="sr-only peer"
                  />
                  <div className="w-11 h-6 bg-gray-200 peer-focus:outline-none peer-focus:ring-4 peer-focus:ring-blue-300 dark:peer-focus:ring-blue-800 rounded-full peer dark:bg-gray-700 peer-checked:after:translate-x-full rtl:peer-checked:after:-translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all dark:border-gray-600 peer-checked:bg-blue-600"></div>
                </label>
              </div>

              <div className="flex items-center gap-2 mb-4">
                {isConfigured ? (
                  <>
                    <CheckCircle className="h-4 w-4 text-[var(--tone-success)]" />
                    <span className="text-sm text-[var(--tone-success)]">已配置</span>
                  </>
                ) : (
                  <>
                    <XCircle className="h-4 w-4 text-[var(--tone-danger)]" />
                    <span className="text-sm text-[var(--tone-danger)]">未配置</span>
                  </>
                )}
                {isDefault && (
                  <>
                    <Star className="h-4 w-4 text-amber-400 fill-amber-400" />
                    <span className="text-sm text-amber-400">默认</span>
                  </>
                )}
              </div>

              {testResult && (
                <div className={`mb-4 p-3 rounded-md text-sm ${
                  testResult.success
                    ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
                    : 'bg-red-500/10 text-red-400 border border-red-500/20'
                }`}>
                  {testResult.message}
                </div>
              )}

              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => handleEdit(provider.provider_key!)}
                  className="flex-1"
                >
                  <Settings2 className="h-3.5 w-3.5 mr-1.5" />
                  编辑
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => handleTest(provider.provider_key!)}
                  disabled={isTesting || !isConfigured}
                  className="flex-1"
                >
                  <TestTube className="h-3.5 w-3.5 mr-1.5" />
                  {isTesting ? '测试中...' : '测试'}
                </Button>
                {!isDefault && isConfigured && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => handleSetDefault(provider.provider_key!)}
                    title="设为默认"
                  >
                    <Star className="h-3.5 w-3.5" />
                  </Button>
                )}
              </div>
            </Card>
          )
        })}
      </div>

      {filteredCatalog.length === 0 && (
        <div className="text-center py-12 text-[var(--text-muted)]">
          未找到匹配的服务
        </div>
      )}
    </div>
  )
}
