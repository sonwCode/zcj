import { useCallback, useEffect, useMemo, useState } from 'react'
import { Eye, History, ListTodo, RefreshCw, Square } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { TaskLogPanel } from '@/components/tasks/TaskLogPanel'
import { apiFetch } from '@/lib/utils'

const TYPE_LABELS: Record<string, string> = {
  register: '协议注册',
  legacy_register: '其他平台注册',
  refresh_token_check: '401 验活',
}

const STATUS_LABELS: Record<string, string> = {
  pending: '等待中',
  claimed: '已领取',
  running: '运行中',
  cancel_requested: '停止中',
  cancelled: '已取消',
  interrupted: '已中断',
  succeeded: '已完成',
  failed: '失败',
}

type TaskView = 'active' | 'history'

type TaskRecord = {
  id?: string
  task_id: string
  type: string
  platform?: string
  status: string
  terminal?: boolean
  cancellable?: boolean
  progress?: string
  success?: number
  error_count?: number
  error?: string
  created_at?: string
  finished_at?: string
}

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error && error.message ? error.message : fallback
}

function isActiveTask(status: string) {
  return ['pending', 'claimed', 'running', 'cancel_requested'].includes(status)
}

function isCancellableTask(status: string) {
  return ['pending', 'claimed', 'running', 'cancel_requested'].includes(status)
}

function statusClass(status: string) {
  if (status === 'succeeded') return 'bg-emerald-500/10 text-emerald-300 ring-emerald-500/30'
  if (status === 'failed') return 'bg-red-500/10 text-red-300 ring-red-500/30'
  if (status === 'cancelled' || status === 'interrupted') return 'bg-amber-500/10 text-amber-300 ring-amber-500/30'
  return 'bg-sky-500/10 text-sky-300 ring-sky-500/30'
}

function formatTime(value?: string) {
  if (!value) return '-'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toLocaleString('zh-CN', { hour12: false })
}

export default function Tasks() {
  const [view, setView] = useState<TaskView>('active')
  const [tasks, setTasks] = useState<TaskRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [stoppingId, setStoppingId] = useState<string | null>(null)
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(async (showLoading = false) => {
    if (showLoading) setLoading(true)
    try {
      const data = await apiFetch('/tasks?page=1&page_size=100')
      const all = Array.isArray(data?.items) ? data.items as TaskRecord[] : []
      setTasks(all.filter(task => view === 'active' ? isActiveTask(task.status) : !isActiveTask(task.status)))
      setError('')
    } catch (err: unknown) {
      setError(errorMessage(err, '读取任务失败'))
    } finally {
      setLoading(false)
    }
  }, [view])

  useEffect(() => {
    setSelectedTaskId(null)
    void load(true)
    const interval = view === 'active' ? 1000 : 5000
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void load()
    }, interval)
    return () => window.clearInterval(timer)
  }, [load, view])

  const stop = async (taskId: string) => {
    setStoppingId(taskId)
    try {
      await apiFetch(`/tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST' })
      await load()
    } catch (err: unknown) {
      setError(errorMessage(err, '停止任务失败'))
    } finally {
      setStoppingId(null)
    }
  }

  const selectedTask = useMemo(
    () => tasks.find(task => task.task_id === selectedTaskId) || null,
    [selectedTaskId, tasks],
  )

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-xl font-semibold text-[var(--text-primary)]">任务中心</h1>
          <p className="mt-1 text-sm text-[var(--text-muted)]">
            aBai ChatGPT 注册与其他平台注册使用不同任务链；这里统一查看、停止和追踪持久化任务。
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => void load(true)} disabled={loading}>
          <RefreshCw className={`mr-2 h-4 w-4 ${loading ? 'animate-spin' : ''}`} />刷新
        </Button>
      </div>

      <div className="inline-flex rounded-lg border border-[var(--border)] bg-[var(--bg-card)] p-1">
        <button
          type="button"
          onClick={() => setView('active')}
          className={`flex items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors ${
            view === 'active'
              ? 'bg-blue-600 text-white shadow-sm'
              : 'text-[var(--text-secondary)] hover:bg-[var(--bg-hover)]'
          }`}
        >
          <ListTodo className="h-4 w-4" />运行任务
        </button>
        <button
          type="button"
          onClick={() => setView('history')}
          className={`flex items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors ${
            view === 'history'
              ? 'bg-blue-600 text-white shadow-sm'
              : 'text-[var(--text-secondary)] hover:bg-[var(--bg-hover)]'
          }`}
        >
          <History className="h-4 w-4" />任务历史
        </button>
      </div>

      {error ? <div className="rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-300">{error}</div> : null}

      {view === 'active' ? (
        <ActiveTasks
          tasks={tasks}
          loading={loading}
          stoppingId={stoppingId}
          onStop={stop}
          onReload={load}
        />
      ) : (
        <div className="grid gap-5 xl:grid-cols-[minmax(0,1.2fr)_minmax(420px,0.8fr)]">
          <TaskHistoryTable
            tasks={tasks}
            loading={loading}
            stoppingId={stoppingId}
            selectedTaskId={selectedTaskId}
            onSelect={setSelectedTaskId}
            onStop={stop}
          />
          <div className="min-h-[560px] rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-4">
            {selectedTask ? (
              <div className="h-[650px]">
                <div className="mb-4 flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="font-medium text-[var(--text-primary)]">{TYPE_LABELS[selectedTask.type] || selectedTask.type}</div>
                    <div className="mt-1 truncate font-mono text-xs text-[var(--text-muted)]">{selectedTask.task_id}</div>
                  </div>
                  <span className={`shrink-0 rounded-full px-2 py-1 text-xs ring-1 ring-inset ${statusClass(selectedTask.status)}`}>
                    {STATUS_LABELS[selectedTask.status] || selectedTask.status}
                  </span>
                </div>
                <div className="h-[590px]">
                  <TaskLogPanel taskId={selectedTask.task_id} onDone={() => void load()} />
                </div>
              </div>
            ) : (
              <div className="flex h-full min-h-[520px] items-center justify-center text-center text-sm text-[var(--text-muted)]">
                选择一条历史任务查看完整日志
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function ActiveTasks({
  tasks,
  loading,
  stoppingId,
  onStop,
  onReload,
}: {
  tasks: TaskRecord[]
  loading: boolean
  stoppingId: string | null
  onStop: (taskId: string) => Promise<void>
  onReload: () => Promise<void>
}) {
  return (
    <div className="overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--bg-card)]">
      <div className="border-b border-[var(--border)] px-4 py-3 text-sm text-[var(--text-secondary)]">
        当前运行：<b className="text-[var(--text-primary)]">{tasks.length}</b> 个。页面刷新后会从持久化日志尾部恢复。
      </div>
      {!loading && tasks.length === 0 ? (
        <div className="px-5 py-12 text-center text-sm text-[var(--text-muted)]">当前没有运行任务</div>
      ) : (
        <div className="divide-y divide-[var(--border)]">
          {tasks.map(task => (
            <div key={task.task_id} className="space-y-3 px-4 py-4">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium text-[var(--text-primary)]">{TYPE_LABELS[task.type] || task.type}</span>
                    <span className={`rounded-full px-2 py-0.5 text-xs ring-1 ring-inset ${statusClass(task.status)}`}>
                      {STATUS_LABELS[task.status] || task.status}
                    </span>
                  </div>
                  <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-sm text-[var(--text-secondary)]">
                    <span>进度 <b className="text-[var(--text-primary)]">{task.progress || '0/0'}</b></span>
                    <span className="font-mono text-xs text-[var(--text-muted)]" title={task.task_id}>{task.task_id}</span>
                  </div>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void onStop(task.task_id)}
                  disabled={!isCancellableTask(task.status) || stoppingId === task.task_id}
                  className="shrink-0 border-red-500/35 text-red-300 hover:bg-red-500/10 hover:text-red-200"
                >
                  <Square className="mr-2 h-3.5 w-3.5" />
                  {stoppingId === task.task_id ? '停止中…' : '停止任务'}
                </Button>
              </div>
              <TaskLogPanel taskId={task.task_id} onDone={() => void onReload()} />
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function TaskHistoryTable({
  tasks,
  loading,
  stoppingId,
  selectedTaskId,
  onSelect,
  onStop,
}: {
  tasks: TaskRecord[]
  loading: boolean
  stoppingId: string | null
  selectedTaskId: string | null
  onSelect: (taskId: string) => void
  onStop: (taskId: string) => Promise<void>
}) {
  return (
    <div className="overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--bg-card)]">
      <div className="border-b border-[var(--border)] px-4 py-3 text-sm text-[var(--text-secondary)]">
        最近 {tasks.length} 条任务记录
      </div>
      {!loading && tasks.length === 0 ? (
        <div className="px-5 py-12 text-center text-sm text-[var(--text-muted)]">暂无历史任务</div>
      ) : (
        <div className="max-h-[690px] overflow-auto">
          <table className="w-full min-w-[760px] text-left text-sm">
            <thead className="sticky top-0 z-10 bg-[var(--bg-card)] text-xs text-[var(--text-muted)]">
              <tr className="border-b border-[var(--border)]">
                <th className="px-4 py-3 font-medium">任务</th>
                <th className="px-4 py-3 font-medium">状态</th>
                <th className="px-4 py-3 font-medium">结果</th>
                <th className="px-4 py-3 font-medium">创建时间</th>
                <th className="px-4 py-3 text-right font-medium">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border)]">
              {tasks.map(task => (
                <tr
                  key={task.task_id}
                  className={selectedTaskId === task.task_id ? 'bg-blue-500/10' : 'hover:bg-[var(--bg-hover)]/60'}
                >
                  <td className="px-4 py-3">
                    <div className="font-medium text-[var(--text-primary)]">{TYPE_LABELS[task.type] || task.type}</div>
                    <div className="mt-1 max-w-[220px] truncate font-mono text-[11px] text-[var(--text-muted)]" title={task.task_id}>
                      {task.task_id}
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <span className={`rounded-full px-2 py-1 text-xs ring-1 ring-inset ${statusClass(task.status)}`}>
                      {STATUS_LABELS[task.status] || task.status}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-[var(--text-secondary)]">
                    <div>进度 {task.progress || '0/0'}</div>
                    <div className="mt-1 text-xs text-[var(--text-muted)]">成功 {task.success || 0} / 失败 {task.error_count || 0}</div>
                  </td>
                  <td className="whitespace-nowrap px-4 py-3 text-xs text-[var(--text-secondary)]">{formatTime(task.created_at)}</td>
                  <td className="px-4 py-3">
                    <div className="flex justify-end gap-2">
                      {isCancellableTask(task.status) ? (
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => void onStop(task.task_id)}
                          disabled={stoppingId === task.task_id}
                          className="border-red-500/35 text-red-300 hover:bg-red-500/10"
                        >
                          <Square className="h-3.5 w-3.5" />
                        </Button>
                      ) : null}
                      <Button variant="outline" size="sm" onClick={() => onSelect(task.task_id)}>
                        <Eye className="mr-1.5 h-3.5 w-3.5" />日志
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
