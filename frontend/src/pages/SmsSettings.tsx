import { useCallback, useEffect, useState } from 'react'
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
        initial[field.key] = setting?.config?.[field.key] || ''
      } else {
        initial[field.key] = setting?.config?.[field.key] || setting?.auth?.[field.key] || ''
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
      const value = formData[field.key] || ''
      if (field.category === 'auth') {
        auth[field.key] = value
      } else if (field.category === 'config') {
        config[field.key] = value
      } else {
        config[field.key] = value
      }
    })

    try {
      await apiFetch(`/api/provider-settings/${editingProvider}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider: editingProvider, auth, config })
      })
      await invalidateConfigOptionsCache()
      setEditingProvider(null)
      await load()
    } catch (err) {
      alert('保存失败: ' + String(err))
    }
  }

  const handleTest = async (providerKey: string) => {
    const definition = catalog.find(p => p.provider_key === providerKey)
    const setting = settingsMap.get(providerKey)
    if (!definition) return

    const auth: Record<string, string> = {}
    const config: Record<string, string> = {}

    definition.fields?.forEach(field => {
      const value = setting?.auth?.[field.key] || setting?.config?.[field.key] || ''
      if (field.category === 'auth') {
        auth[field.key] = value
      } else {
        config[field.key] = value
      }
    })

    setTestingProvider(providerKey)

    try {
      const res = await apiFetch(`/api/provider-settings/${providerKey}/test`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider: providerKey, auth, config })
      })
      const data = await res.json()
      setTestResults(prev => new Map(prev).set(providerKey, { 
        success: data.success, 
        message: data.message || (data.success ? '测试成功' : '测试失败') 
      }))
    } catch (err) {
      setTestResults(prev => new Map(prev).set(providerKey, { 
        success: false, 
        message: String(err) 
      }))
    } finally {
      setTestingProvider(null)
    }
  }

  const handleToggle = async (providerKey: string, enabled: boolean) => {
    try {
      await apiFetch(`/api/provider-settings/${providerKey}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider: providerKey, enabled })
      })
      await invalidateConfigOptionsCache()
      await load()
    } catch (err) {
      alert('切换状态失败: ' + String(err))
    }
  }

  const handleDelete = async (providerKey: string) => {
    const providerLabel = catalog.find(p => p.provider_key === providerKey)?.label || providerKey
    if (!confirm(`确认删除 ${providerLabel} 的配置？`)) return
    try {
      await apiFetch(`/api/provider-settings/${providerKey}`, { method: 'DELETE' })
      await invalidateConfigOptionsCache()
      await load()
    } catch (err) {
      alert('删除失败: ' + String(err))
    }
  }

  return (
    <div className="space-y-4">
      {error && (
        <div className="rounded border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-300">
          {error}
        </div>
      )}

      <div className="rounded border border-blue-500/20 bg-blue-500/10 px-4 py-3 text-sm text-blue-200">
        当前接码服务仅用于手机验证码接收。点击"测试"按钮查询余额，不会消耗额度。
      </div>

      {/* 搜索栏 */}
      <div className="flex items-center gap-3">
        <input
          type="text"
          value={searchTerm}
          onChange={e => setSearchTerm(e.target.value)}
          placeholder="搜索邮箱、token、来源..."
          className="flex-1 px-4 py-2 bg-[#0f1419] border border-gray-700 rounded text-sm text-white placeholder-gray-500 focus:outline-none focus:border-blue-500"
        />
        <div className="text-sm text-gray-400">
          已显示 {filteredCatalog.filter(p => p.provider_key && settingsMap.has(p.provider_key)).length} / 
          补链接数 {filteredCatalog.length}
        </div>
      </div>

      {/* 表格 */}
      <div className="bg-white rounded shadow overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 border-b border-gray-200">
            <tr>
              <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wide">
                <input type="checkbox" className="w-4 h-4" />
              </th>
              <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wide">ID</th>
              <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wide">邮箱</th>
              <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wide">来源</th>
              <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wide">Token</th>
              <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wide">2FA</th>
              <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wide">Codex</th>
              <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wide">创建时间</th>
              <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wide">操作</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {filteredCatalog.map(provider => {
              if (!provider.provider_key) return null
              const setting = settingsMap.get(provider.provider_key)
              const isEnabled = setting?.enabled ?? false
              const isTesting = testingProvider === provider.provider_key
              const testResult = testResults.get(provider.provider_key)
              const hasConfig = setting && (
                (setting.auth && Object.keys(setting.auth).length > 0) ||
                (setting.config && Object.keys(setting.config).length > 0)
              )

              return (
                <tr key={provider.provider_key} className="hover:bg-gray-50 transition-colors">
                  <td className="px-4 py-3">
                    <input type="checkbox" className="w-4 h-4" />
                  </td>
                  <td className="px-4 py-3 text-gray-900 font-mono text-xs">
                    #{provider.provider_key}
                  </td>
                  <td className="px-4 py-3 text-gray-900 font-medium">
                    {provider.label}
                  </td>
                  <td className="px-4 py-3 text-gray-600">
                    {provider.description || '-'}
                  </td>
                  <td className="px-4 py-3">
                    {hasConfig ? (
                      <span className="text-green-600 font-medium">已配置</span>
                    ) : (
                      <span className="text-gray-400">-</span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <span className={`inline-block px-2 py-1 text-xs rounded ${
                      isEnabled ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-600'
                    }`}>
                      {isEnabled ? '启用' : '关闭'}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    {testResult && (
                      <span className={testResult.success ? 'text-green-600' : 'text-red-600'}>
                        {testResult.success ? '成功' : '失败'}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-gray-500 text-xs">
                    {setting ? new Date().toISOString().split('T')[0] : '-'}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      <button
                        onClick={() => handleEdit(provider.provider_key!)}
                        className="text-blue-600 hover:text-blue-700 font-medium"
                      >
                        编辑
                      </button>
                      <button
                        onClick={() => handleTest(provider.provider_key!)}
                        disabled={isTesting || !hasConfig}
                        className="text-blue-600 hover:text-blue-700 font-medium disabled:text-gray-400"
                      >
                        {isTesting ? '测试中' : '测试'}
                      </button>
                      {isEnabled ? (
                        <button
                          onClick={() => handleToggle(provider.provider_key!, false)}
                          className="text-gray-600 hover:text-gray-700 font-medium"
                        >
                          关闭
                        </button>
                      ) : (
                        <button
                          onClick={() => handleToggle(provider.provider_key!, true)}
                          className="text-green-600 hover:text-green-700 font-medium"
                        >
                          启用
                        </button>
                      )}
                      {setting && (
                        <button
                          onClick={() => handleDelete(provider.provider_key!)}
                          className="text-red-600 hover:text-red-700 font-medium"
                        >
                          删除
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {/* 编辑弹窗 */}
      {editingProvider && (() => {
        const definition = catalog.find(p => p.provider_key === editingProvider)
        if (!definition) return null
        const setting = settingsMap.get(editingProvider)

        return (
          <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50">
            <div className="bg-white rounded-lg w-full max-w-2xl max-h-[90vh] overflow-y-auto shadow-2xl">
              <div className="px-6 py-4 border-b border-gray-200 flex justify-between items-center bg-gray-50">
                <h3 className="text-lg font-semibold text-gray-900">
                  编辑配置 - {definition.label}
                </h3>
                <button 
                  onClick={() => setEditingProvider(null)} 
                  className="text-gray-400 hover:text-gray-600 transition-colors"
                >
                  <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
              <div className="px-6 py-4 space-y-4">
                {definition.fields?.map(field => {
                  const secretPreserved = field.secret && Boolean(setting?.auth_preview?.[field.key])
                  
                  return (
                    <div key={field.key}>
                      <label className="block text-sm font-medium text-gray-700 mb-1.5">
                        {field.label}
                        {field.secret && <span className="text-red-500 ml-1">*</span>}
                      </label>
                      {field.type === 'textarea' ? (
                        <textarea
                          value={formData[field.key] || ''}
                          onChange={e => setFormData({ ...formData, [field.key]: e.target.value })}
                          placeholder={secretPreserved ? '已保存，留空保持不变' : (field.placeholder || '')}
                          className="w-full px-3 py-2 border border-gray-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                          rows={4}
                        />
                      ) : field.type === 'toggle' ? (
                        <label className="flex items-center gap-2 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={formData[field.key] === 'true' || formData[field.key] === '1'}
                            onChange={e => setFormData({ ...formData, [field.key]: e.target.checked ? 'true' : 'false' })}
                            className="w-4 h-4 text-blue-600"
                          />
                          <span className="text-sm text-gray-600">{field.hint || field.label}</span>
                        </label>
                      ) : (
                        <input
                          type={field.secret ? 'password' : 'text'}
                          value={formData[field.key] || ''}
                          onChange={e => setFormData({ ...formData, [field.key]: e.target.value })}
                          placeholder={secretPreserved ? '已保存，留空保持不变' : (field.placeholder || '')}
                          className="w-full px-3 py-2 border border-gray-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                        />
                      )}
                      {field.hint && field.type !== 'toggle' && (
                        <p className="text-xs text-gray-500 mt-1">{field.hint}</p>
                      )}
                    </div>
                  )
                })}
              </div>
              <div className="px-6 py-4 border-t border-gray-200 flex justify-end gap-3 bg-gray-50">
                <button
                  onClick={() => setEditingProvider(null)}
                  className="px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded hover:bg-gray-50 transition-colors"
                >
                  取消
                </button>
                <button
                  onClick={handleSave}
                  className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded hover:bg-blue-700 transition-colors"
                >
                  保存配置
                </button>
              </div>
            </div>
          </div>
        )
      })()}
    </div>
  )
}
