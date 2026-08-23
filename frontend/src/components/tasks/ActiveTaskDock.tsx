import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useNavigate } from "react-router-dom";
import {
  Activity,
  ArrowUpRight,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  X,
} from "lucide-react";

import { TaskLogPanel } from "@/components/tasks/TaskLogPanel";
import {
  getTaskStatusText,
  isTerminalTaskStatus,
} from "@/lib/tasks";
import { apiFetch } from "@/lib/utils";
import { useI18n } from "@/lib/i18n-context";

type TaskSnapshot = {
  id: string;
  task_id?: string;
  type?: string;
  platform?: string;
  status: string;
  progress?: string;
  progress_detail?: {
    current?: number;
    total?: number;
    label?: string;
  };
  success?: number;
  error_count?: number;
  error?: string;
  created_at?: string;
  updated_at?: string;
  finished_at?: string;
};

type RecentTask = {
  task: TaskSnapshot;
  expiresAt: number;
};

const ACTIVE_STATUSES = new Set([
  "pending",
  "claimed",
  "running",
  "cancel_requested",
]);

const RECENT_TASK_TTL_MS = 18_000;
const TASK_REFRESH_MS = 5_000;
const MAX_TASKS_IN_DOCK = 8;

function taskIdOf(task: Partial<TaskSnapshot>) {
  return String(task.id || task.task_id || "");
}

function taskTitle(task: TaskSnapshot) {
  const typeLabels: Record<string, string> = {
    register: "注册",
    phone_bind: "手机号验证",
    codex_oauth: "Codex OAuth",
    get_rt: "获取 RT",
    get_rt_bypass: "获取 RT（绕过）",
    refresh_all_credits: "刷新额度",
  };
  const type = typeLabels[String(task.type || "")] || String(task.type || "任务");
  const platform = String(task.platform || "").trim();
  return platform ? `${type} · ${platform}` : type;
}

function taskProgress(task: TaskSnapshot) {
  const detail = task.progress_detail || {};
  return String(detail.label || task.progress || `${detail.current || 0}/${detail.total || 0}`);
}

function progressPercent(task: TaskSnapshot) {
  const detail = task.progress_detail || {};
  const total = Number(detail.total || 0);
  const current = Number(detail.current || 0);
  if (total > 0) return Math.min(100, Math.round((current / total) * 100));
  return isTerminalTaskStatus(task.status) ? 100 : 12;
}

function formatElapsed(createdAt?: string) {
  if (!createdAt) return "";
  const created = new Date(createdAt).getTime();
  if (!Number.isFinite(created)) return "";
  const seconds = Math.max(0, Math.floor((Date.now() - created) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function statusTone(status: string) {
  if (status === "succeeded") {
    return {
      dot: "bg-emerald-400",
      bar: "bg-emerald-400",
      text: "text-emerald-300",
    };
  }
  if (status === "failed") {
    return { dot: "bg-red-400", bar: "bg-red-400", text: "text-red-300" };
  }
  if (status === "cancelled" || status === "interrupted" || status === "cancel_requested") {
    return { dot: "bg-amber-400", bar: "bg-amber-400", text: "text-amber-300" };
  }
  return { dot: "bg-sky-400", bar: "bg-sky-400", text: "text-sky-300" };
}

function normalizeTask(raw: any): TaskSnapshot | null {
  const id = taskIdOf(raw || {});
  if (!id) return null;
  return {
    ...raw,
    id,
    status: String(raw?.status || "pending"),
  };
}

export default function ActiveTaskDock() {
  const { language } = useI18n();
  const navigate = useNavigate();
  const [activeTasks, setActiveTasks] = useState<TaskSnapshot[]>([]);
  const [recentTasks, setRecentTasks] = useState<RecentTask[]>([]);
  const [latestLogs, setLatestLogs] = useState<Record<string, string>>({});
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(true);
  const previousStatusesRef = useRef<Record<string, string>>({});
  const eventCursorsRef = useRef<Record<string, number>>({});
  const refreshInFlightRef = useRef(false);
  const mountedRef = useRef(true);

  const syncTaskEvents = useCallback(async (task: TaskSnapshot) => {
    const taskId = task.id;
    const since = eventCursorsRef.current[taskId] || 0;
    try {
      const data = await apiFetch(
        `/tasks/${encodeURIComponent(taskId)}/events?since=${since}&limit=50`,
      );
      const items = Array.isArray(data?.items) ? data.items : [];
      if (!items.length || !mountedRef.current) return;
      let cursor = since;
      let latestLine = "";
      for (const item of items) {
        const eventId = Number(item?.id || 0);
        if (eventId > cursor) cursor = eventId;
        const line = String(item?.line || item?.message || "").trim();
        if (line) latestLine = line;
      }
      eventCursorsRef.current[taskId] = cursor;
      if (latestLine) {
        setLatestLogs((current) => ({ ...current, [taskId]: latestLine }));
      }
    } catch {
      // The global dock is passive. The full TaskLogPanel will retry over SSE.
    }
  }, []);

  const refreshTasks = useCallback(async () => {
    if (refreshInFlightRef.current || document.visibilityState !== "visible") return;
    refreshInFlightRef.current = true;
    try {
      const data = await apiFetch("/tasks?page=1&page_size=30");
      if (!mountedRef.current) return;
      const normalized: TaskSnapshot[] = (Array.isArray(data?.items) ? data.items : [])
        .map((item: any) => normalizeTask(item))
        .filter((task: TaskSnapshot | null): task is TaskSnapshot => Boolean(task));
      const now = Date.now();
      const previousStatuses = previousStatusesRef.current;
      const nextStatuses: Record<string, string> = {};
      const newlyFinished = normalized.filter((task: TaskSnapshot) => {
        const id = task.id;
        nextStatuses[id] = task.status;
        return isTerminalTaskStatus(task.status) && ACTIVE_STATUSES.has(previousStatuses[id] || "");
      });
      previousStatusesRef.current = nextStatuses;

      const active = normalized
        .filter((task: TaskSnapshot) => ACTIVE_STATUSES.has(task.status))
        .sort((a: TaskSnapshot, b: TaskSnapshot) => String(a.created_at || "").localeCompare(String(b.created_at || "")))
        .reverse();
      setActiveTasks(active);
      setRecentTasks((current) => {
        const activeIds = new Set(active.map((task) => task.id));
        const next = current.filter(
          (item) => item.expiresAt > now && !activeIds.has(item.task.id),
        );
        for (const task of newlyFinished) {
          if (!next.some((item) => item.task.id === task.id)) {
            next.push({ task, expiresAt: now + RECENT_TASK_TTL_MS });
          }
        }
        return next
          .sort((a, b) => String(b.task.finished_at || b.task.updated_at || "").localeCompare(String(a.task.finished_at || a.task.updated_at || "")))
          .slice(0, 4);
      });

      if (!collapsed) {
        await Promise.all(active.slice(0, MAX_TASKS_IN_DOCK).map(syncTaskEvents));
      }
    } catch {
      // Keep the last known dock state when the API is temporarily unavailable.
    } finally {
      refreshInFlightRef.current = false;
    }
  }, [collapsed, syncTaskEvents]);

  useEffect(() => {
    mountedRef.current = true;
    void refreshTasks();
    const timer = window.setInterval(() => void refreshTasks(), TASK_REFRESH_MS);
    return () => {
      mountedRef.current = false;
      window.clearInterval(timer);
    };
  }, [refreshTasks]);

  useEffect(() => {
    if (!recentTasks.length) return;
    const timer = window.setInterval(() => {
      setRecentTasks((current) => {
        const now = Date.now();
        return current.filter((item) => item.expiresAt > now);
      });
    }, 2_000);
    return () => window.clearInterval(timer);
  }, [recentTasks.length]);

  const visibleRecentTasks = useMemo(
    () => recentTasks.filter((item) => !activeTasks.some((task) => task.id === item.task.id)),
    [activeTasks, recentTasks],
  );
  const visibleTasks = useMemo(
    () => [...activeTasks, ...visibleRecentTasks.map((item) => item.task)].slice(0, MAX_TASKS_IN_DOCK),
    [activeTasks, visibleRecentTasks],
  );
  const selectedTask = visibleTasks.find((task) => task.id === selectedTaskId);

  if (visibleTasks.length === 0 && !selectedTaskId) return null;

  const logDrawer = selectedTaskId && typeof document !== "undefined"
    ? createPortal(
        <div
          className="fixed inset-0 z-[180] bg-slate-950/55 backdrop-blur-[2px]"
          role="presentation"
          onClick={() => setSelectedTaskId(null)}
        >
          <section
            className="absolute inset-y-0 right-0 flex w-[min(780px,100vw)] flex-col border-l border-[var(--border)] bg-[var(--bg-card)] shadow-2xl shadow-black/40"
            role="dialog"
            aria-modal="true"
            aria-label="任务实时日志"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="flex shrink-0 items-center justify-between border-b border-[var(--border)] px-5 py-4">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <Activity className="h-4 w-4 shrink-0 text-[var(--text-accent)]" />
                  <h2 className="truncate text-sm font-semibold text-[var(--text-primary)]">
                    {selectedTask ? taskTitle(selectedTask) : "任务实时日志"}
                  </h2>
                </div>
                {selectedTask && (
                  <div className="mt-1 truncate font-mono text-[10px] text-[var(--text-muted)]">
                    {selectedTask.id}
                  </div>
                )}
              </div>
              <button
                type="button"
                onClick={() => setSelectedTaskId(null)}
                className="sub2-icon-button ml-3 shrink-0"
                title="关闭日志面板"
                aria-label="关闭日志面板"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="min-h-0 flex-1 overflow-hidden p-4">
              <TaskLogPanel
                taskId={selectedTaskId}
                onDone={() => {
                  void refreshTasks();
                }}
              />
            </div>
            <div className="flex shrink-0 items-center justify-between gap-3 border-t border-[var(--border)] px-5 py-3">
              <span className="truncate text-[11px] text-[var(--text-muted)]">
                关闭此面板不会取消任务，任务会继续在后台执行
              </span>
              <button
                type="button"
                onClick={() => {
                  setSelectedTaskId(null);
                  navigate(`/history?task=${encodeURIComponent(selectedTaskId)}`);
                }}
                className="inline-flex shrink-0 items-center gap-1.5 text-xs text-[var(--text-accent)] hover:text-[var(--text-primary)]"
              >
                查看任务记录
                <ArrowUpRight className="h-3.5 w-3.5" />
              </button>
            </div>
          </section>
        </div>,
        document.body,
      )
    : null;

  return (
    <>
      <section
        className="fixed bottom-4 right-4 z-[130] w-[min(440px,calc(100vw-24px))] overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--bg-card)] shadow-2xl shadow-black/30 backdrop-blur-xl"
        aria-label="运行中任务"
      >
        <div className="flex items-center gap-2 border-b border-[var(--border)] px-3.5 py-2.5">
          <span className="relative flex h-2.5 w-2.5 shrink-0">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-sky-400 opacity-60" />
            <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-sky-400" />
          </span>
          <span className="text-xs font-semibold text-[var(--text-primary)]">
            {activeTasks.length > 0 ? "正在运行" : "最近完成"}
          </span>
          {activeTasks.length > 0 && (
            <span className="rounded-full bg-sky-400/15 px-1.5 py-0.5 text-[10px] font-semibold text-sky-300">
              {activeTasks.length}
            </span>
          )}
          <span className="ml-auto text-[10px] text-[var(--text-muted)]">
            关闭弹窗后仍持续记录
          </span>
          <button
            type="button"
            onClick={() => setCollapsed((value) => !value)}
            className="sub2-icon-button !p-1"
            title={collapsed ? "展开任务列表" : "收起任务列表"}
            aria-label={collapsed ? "展开任务列表" : "收起任务列表"}
          >
            {collapsed ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
          </button>
        </div>
        {!collapsed && (
          <div className="max-h-[min(54vh,420px)] space-y-1 overflow-y-auto p-2">
            {visibleTasks.map((task) => {
              const tone = statusTone(task.status);
              const terminal = isTerminalTaskStatus(task.status);
              const latestLine = latestLogs[task.id] || (task.error ? String(task.error) : "等待日志...");
              return (
                <button
                  type="button"
                  key={task.id}
                  onClick={() => setSelectedTaskId(task.id)}
                  className="group block w-full rounded-lg border border-transparent px-2.5 py-2 text-left transition-colors hover:border-[var(--border)] hover:bg-[var(--bg-hover)]"
                >
                  <div className="flex items-center gap-2">
                    <span className={`h-2 w-2 shrink-0 rounded-full ${tone.dot} ${terminal ? "" : "animate-pulse"}`} />
                    <span className="min-w-0 flex-1 truncate text-xs font-semibold text-[var(--text-primary)]">
                      {taskTitle(task)}
                    </span>
                    <span className={`shrink-0 text-[10px] font-medium ${tone.text}`}>
                      {getTaskStatusText(task.status, language)}
                    </span>
                    <ChevronRight className="h-3.5 w-3.5 shrink-0 text-[var(--text-muted)] transition-transform group-hover:translate-x-0.5" />
                  </div>
                  <div className="mt-1 truncate pl-4 font-mono text-[10px] text-[var(--text-secondary)]" title={latestLine}>
                    {latestLine}
                  </div>
                  <div className="mt-1.5 flex items-center gap-2 pl-4">
                    <div className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-[var(--bg-hover)]">
                      <div
                        className={`h-full rounded-full transition-[width] duration-500 ${tone.bar}`}
                        style={{ width: `${progressPercent(task)}%` }}
                      />
                    </div>
                    <span className="shrink-0 font-mono text-[10px] text-[var(--text-muted)]">
                      {taskProgress(task)}
                    </span>
                    <span className="w-8 shrink-0 text-right text-[10px] text-[var(--text-muted)]">
                      {formatElapsed(task.created_at)}
                    </span>
                  </div>
                </button>
              );
            })}
          </div>
        )}
        {!collapsed && visibleTasks.length > MAX_TASKS_IN_DOCK && (
          <div className="border-t border-[var(--border)] px-3 py-2 text-center text-[10px] text-[var(--text-muted)]">
            还有更多任务，请前往任务日志查看
          </div>
        )}
        <div className="flex items-center justify-between border-t border-[var(--border)] px-3.5 py-2">
          <span className="text-[10px] text-[var(--text-muted)]">
            实时摘要 · 点击任务查看完整日志
          </span>
          <button
            type="button"
            onClick={() => navigate("/history")}
            className="inline-flex items-center gap-1 text-[10px] text-[var(--text-accent)] hover:text-[var(--text-primary)]"
          >
            任务日志
            <ArrowUpRight className="h-3 w-3" />
          </button>
        </div>
      </section>
      {logDrawer}
    </>
  );
}
