import { useEffect, useMemo, useRef, useState } from "react";
import {
  Check,
  ChevronDown,
  ChevronRight,
  CircleStop,
  Copy,
  ListFilter,
  Loader2,
  ScrollText,
  TriangleAlert,
} from "lucide-react";

import { API_BASE, apiFetch, copyTextToClipboard } from "@/lib/utils";
import { getTaskStatusText, isCancellableTaskStatus, isTerminalTaskStatus } from "@/lib/tasks";
import { useI18n } from "@/lib/i18n-context";

/**
 * 单条日志事件。`subtaskId` 来自后端 ``serialize_event(...).detail.subtask_id``——
 * ``TaskLogger.log`` 在每个并发 worker 进入时通过 thread-local 自动注入。前端
 * 按这个字段分组折叠展示；空字符串表示主任务（任务级状态、汇总日志等）。
 */
type LogEvent = {
  id: number;
  line: string;
  subtaskId: string;
  subtaskLabel: string;
  level: string;
  logView: "summary" | "diagnostic";
};

type StreamPayload = {
  id?: number;
  line?: string;
  level?: string;
  type?: string;
  detail?: {
    subtask_id?: string;
    subtask_label?: string;
    log_view?: string;
    log_stage?: string;
  };
  done?: boolean;
  status?: string;
};

type TaskSnapshot = {
  id?: string;
  task_id?: string;
  status?: string;
  progress?: string;
  progress_detail?: {
    current?: number;
    total?: number;
    label?: string;
  };
  error?: string;
  errors?: string[];
  success?: number;
  error_count?: number;
  cashier_urls?: string[];
  result?: {
    sub2_sync?: {
      attempted?: number;
      synced?: number;
      cooling?: number;
      pending?: number;
      invalid?: number;
      failed?: number;
      items?: Array<{ email?: string; status?: string; error?: string }>;
    };
    data?: {
      failure_summary?: Array<{ code: string; label: string; count: number; sample?: string }>;
    };
  };
};

type LogGroup = {
  id: string;
  label: string;
  events: LogEvent[];
};

const MAIN_GROUP_ID = "__main__";
const MAX_BUFFERED_LOG_EVENTS = 1000;
const LOG_FLUSH_INTERVAL_MS = 160;

function classifyLine(line: string, level: string): string {
  if (level === "error") return "text-red-400";
  if (level === "warning") return "text-amber-300";
  if (line.includes("✓") || line.includes("成功")) return "text-emerald-400";
  if (line.includes("✗") || line.includes("失败") || line.includes("错误"))
    return "text-red-400";
  return "text-[var(--text-secondary)]";
}

export function TaskLogPanel({
  taskId,
  onDone,
  onTaskUpdate,
}: {
  taskId: string;
  onDone: (status: string) => void;
  onTaskUpdate?: (task: TaskSnapshot) => void;
}) {
  const { t, language } = useI18n();
  const [events, setEvents] = useState<LogEvent[]>([]);
  const [task, setTask] = useState<TaskSnapshot | null>(null);
  const [doneStatus, setDoneStatus] = useState<string | null>(null);
  const [canceling, setCanceling] = useState(false);
  const [logView, setLogView] = useState<"summary" | "all">("summary");
  const [copyStatus, setCopyStatus] = useState<"idle" | "copied" | "failed">("idle");
  // 折叠状态：默认全展开（undefined / false 都视为展开）
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const seenEventIdsRef = useRef<Set<number>>(new Set());
  const cursorRef = useRef(0);
  const doneRef = useRef(false);
  const onDoneRef = useRef(onDone);
  const onTaskUpdateRef = useRef(onTaskUpdate);
  const sseHealthyRef = useRef(false);
  const eventSourceRef = useRef<EventSource | null>(null);
  const initialEventsLoadedRef = useRef(false);
  const pendingTerminalStatusRef = useRef<string | null>(null);
  const pendingEventsRef = useRef<LogEvent[]>([]);
  const flushTimerRef = useRef<number | null>(null);
  const taskSyncInFlightRef = useRef(false);
  const taskSignatureRef = useRef("");
  const copyResetTimerRef = useRef<number | null>(null);

  useEffect(() => {
    onDoneRef.current = onDone;
  }, [onDone]);

  useEffect(() => {
    onTaskUpdateRef.current = onTaskUpdate;
  }, [onTaskUpdate]);

  useEffect(() => {
    if (!taskId) return;
    seenEventIdsRef.current = new Set();
    cursorRef.current = 0;
    doneRef.current = false;
    sseHealthyRef.current = false;
    initialEventsLoadedRef.current = false;
    pendingTerminalStatusRef.current = null;
    pendingEventsRef.current = [];
    taskSignatureRef.current = "";
    if (flushTimerRef.current !== null) {
      window.clearTimeout(flushTimerRef.current);
      flushTimerRef.current = null;
    }
    setEvents([]);
    setTask(null);
    setDoneStatus(null);
    setCollapsed({});

    const flushEvents = () => {
      flushTimerRef.current = null;
      const batch = pendingEventsRef.current.splice(0);
      if (!batch.length) return;
      setEvents((prev) => [...prev, ...batch].slice(-MAX_BUFFERED_LOG_EVENTS));
    };

    const scheduleFlush = () => {
      if (flushTimerRef.current !== null) return;
      flushTimerRef.current = window.setTimeout(flushEvents, LOG_FLUSH_INTERVAL_MS);
    };

    const pushEvent = (payload: StreamPayload) => {
      const eventId = Number(payload?.id || 0);
      if (eventId && seenEventIdsRef.current.has(eventId)) return;
      if (eventId) {
        seenEventIdsRef.current.add(eventId);
        cursorRef.current = Math.max(cursorRef.current, eventId);
      }
      if (payload?.line) {
        const detail = payload?.detail || {};
        pendingEventsRef.current.push({
          id: eventId || seenEventIdsRef.current.size,
          line: String(payload.line),
          subtaskId: String(detail?.subtask_id || ""),
          subtaskLabel: String(detail?.subtask_label || ""),
          level: String(payload?.level || "info"),
          logView: detail?.log_view === "diagnostic" ? "diagnostic" : "summary",
        });
        scheduleFlush();
      }
      if (payload?.done && !doneRef.current) {
        if (!initialEventsLoadedRef.current) {
          pendingTerminalStatusRef.current = payload.status || "succeeded";
          return;
        }
        flushEvents();
        doneRef.current = true;
        sseHealthyRef.current = false;
        eventSourceRef.current?.close();
        eventSourceRef.current = null;
        const nextStatus = payload.status || "succeeded";
        setDoneStatus(nextStatus);
        onDoneRef.current(nextStatus);
      }
    };

    const hydrateTaskEvents = async (since = 0) => {
      let cursor = Math.max(Number(since || 0), 0);
      for (let page = 0; page < 50; page += 1) {
        const data = await apiFetch(
          '/tasks/' + taskId + '/events?since=' + cursor + '&limit=500',
        );
        const items = Array.isArray(data?.items) ? data.items : [];
        if (!items.length) break;
        for (const item of items) pushEvent(item);
        const nextCursor = items.reduce(
          (max: number, item: StreamPayload) => Math.max(max, Number(item?.id || 0)),
          cursor,
        );
        if (nextCursor <= cursor) break;
        cursor = nextCursor;
        if (items.length < 500) break;
      }
    };

    const syncTask = async () => {
      if (taskSyncInFlightRef.current || document.visibilityState !== "visible") return;
      taskSyncInFlightRef.current = true;
      try {
        const latest = await apiFetch(`/tasks/${taskId}`);
        const signature = JSON.stringify({
          status: latest?.status,
          progress: latest?.progress,
          progress_detail: latest?.progress_detail,
          error: latest?.error,
          errors: latest?.errors,
          success: latest?.success,
          error_count: latest?.error_count,
          sub2_sync: latest?.result?.sub2_sync,
        });
        if (signature !== taskSignatureRef.current) {
          taskSignatureRef.current = signature;
          setTask(latest);
          onTaskUpdateRef.current?.(latest);
        }
        if (isTerminalTaskStatus(latest.status) && !doneRef.current) {
          if (!initialEventsLoadedRef.current) {
            pendingTerminalStatusRef.current = latest.status;
          } else {
            await hydrateTaskEvents(cursorRef.current);
            pushEvent({ done: true, status: latest.status });
          }
        }
      } finally {
        taskSyncInFlightRef.current = false;
      }
    };

    const startLogTransport = async () => {
      await hydrateTaskEvents(0);
      initialEventsLoadedRef.current = true;
      await syncTask();
      if (doneRef.current) return;

      const es = new EventSource(
        API_BASE + '/tasks/' + taskId + '/logs/stream?since=' + cursorRef.current,
      );
      eventSourceRef.current = es;
      es.onopen = () => {
        sseHealthyRef.current = true;
      };
      es.onmessage = (e) => {
        sseHealthyRef.current = true;
        pushEvent(JSON.parse(e.data));
      };
      es.onerror = () => {
        if (doneRef.current) {
          es.close();
          if (eventSourceRef.current === es) {
            eventSourceRef.current = null;
          }
          return;
        }
        sseHealthyRef.current = false;
      };
      if (pendingTerminalStatusRef.current && !doneRef.current) {
        const terminalStatus = pendingTerminalStatusRef.current;
        pendingTerminalStatusRef.current = null;
        pushEvent({ done: true, status: terminalStatus });
      }
    };

    startLogTransport().catch(() => {
      initialEventsLoadedRef.current = true;
      syncTask().catch(() => {});
    });

    // 进度需要持续轮询：SSE 只发 events，progress 在 task model 上，
    // 必须主动 GET /tasks/{id} 拿。原实现里只在 SSE 不健康时轮询，导致
    // SSE 正常时进度从来不更新。
    const progressPoll = window.setInterval(() => {
      if (doneRef.current) return;
      syncTask().catch(() => {});
    }, 3000);

    const fallbackPoll = window.setInterval(async () => {
      if (doneRef.current || sseHealthyRef.current || document.visibilityState !== "visible") return;
      try {
        const data = await apiFetch(
          `/tasks/${taskId}/events?since=${cursorRef.current}`,
        );
        for (const item of data.items || []) {
          pushEvent(item);
        }
      } catch {
        // passive
      }
    }, 3000);

    return () => {
      sseHealthyRef.current = false;
      eventSourceRef.current?.close();
      eventSourceRef.current = null;
      if (flushTimerRef.current !== null) {
        window.clearTimeout(flushTimerRef.current);
        flushTimerRef.current = null;
      }
      window.clearInterval(progressPoll);
      window.clearInterval(fallbackPoll);
      if (copyResetTimerRef.current !== null) {
        window.clearTimeout(copyResetTimerRef.current);
        copyResetTimerRef.current = null;
      }
      setCanceling(false);
    };
  }, [taskId]);

  const visibleEvents = useMemo(
    () =>
      logView === "all"
        ? events
        : events.filter((event) => event.logView !== "diagnostic"),
    [events, logView],
  );

  // 按 subtaskId 把事件切成分组：主任务 + 每个 worker。
  // 顺序按"首次出现"排，保证 worker 折叠面板顺序稳定（worker_1 / worker_2…）。
  const groups: LogGroup[] = useMemo(() => {
    const map = new Map<string, LogGroup>();
    map.set(MAIN_GROUP_ID, {
      id: MAIN_GROUP_ID,
      label: t("taskLog.mainGroup"),
      events: [],
    });
    for (const ev of visibleEvents) {
      const key = ev.subtaskId || MAIN_GROUP_ID;
      if (!map.has(key)) {
        map.set(key, {
          id: key,
          label: ev.subtaskLabel || key,
          events: [],
        });
      }
      const group = map.get(key)!;
      group.events.push(ev);
      if (key !== MAIN_GROUP_ID && ev.subtaskLabel) {
        group.label = ev.subtaskLabel;
      }
    }
    return Array.from(map.values());
  }, [visibleEvents, t]);

  const toggleGroup = (id: string) => {
    setCollapsed((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const currentStatus = doneStatus || task?.status || "running";
  const canCancel = isCancellableTaskStatus(currentStatus) && !canceling;
  const progress = task?.progress_detail || {};
  const progressTotal = Number(progress.total || 0);
  const progressCurrent = Number(progress.current || 0);
  const progressPercent =
    progressTotal > 0
      ? Math.min(100, Math.round((progressCurrent / progressTotal) * 100))
      : 0;
  const errorText =
    task?.error || (Array.isArray(task?.errors) ? task.errors[0] : "");
  // SMS_POOL_EXHAUSTED 是后端约定的"号码不可用"标记前缀，渲染成更友好
  // 的中文（用户诉求："号池没号结束当前线程，并且前端弹窗此号码不可用"）
  const friendlyError = String(errorText || "").includes("SMS_POOL_EXHAUSTED")
    ? t("ctfGptPlus.smsPoolExhausted")
    : errorText;
  const failureSummary = Array.isArray(task?.result?.data?.failure_summary)
    ? task.result.data.failure_summary
    : [];
  const sub2Sync = task?.result?.sub2_sync;
  const statusTone =
    currentStatus === "succeeded"
      ? "border-emerald-400/40 bg-emerald-400/10 text-emerald-200"
      : currentStatus === "failed"
        ? "border-red-400/40 bg-red-400/10 text-red-200"
        : currentStatus === "cancelled" || currentStatus === "interrupted"
          ? "border-amber-400/40 bg-amber-400/10 text-amber-200"
          : "border-sky-400/40 bg-sky-400/10 text-sky-200";

  const copyLogs = async () => {
    // Preserve worker correlation in copied diagnostics. The rendered panel is
    // grouped by subtask, but a flat clipboard export previously discarded
    // that grouping and made concurrent SMS/OAuth timelines ambiguous.
    const copied = await copyTextToClipboard(
      visibleEvents
        .map((ev) => `${ev.subtaskId ? `[${ev.subtaskId}] ` : ""}${ev.line}`)
        .join("\n"),
    );
    setCopyStatus(copied ? "copied" : "failed");
    if (copyResetTimerRef.current !== null) {
      window.clearTimeout(copyResetTimerRef.current);
    }
    copyResetTimerRef.current = window.setTimeout(() => {
      setCopyStatus("idle");
      copyResetTimerRef.current = null;
    }, 1800);
  };

  const cancelTask = async () => {
    if (!canCancel) return;
    setCanceling(true);
    try {
      const updated = await apiFetch(
        "/tasks/" + encodeURIComponent(taskId) + "/cancel",
        { method: "POST" },
      );
      setTask(updated);
      onTaskUpdateRef.current?.(updated);
    } catch (error) {
      setTask((current) => ({
        ...(current || {}),
        error: error instanceof Error ? error.message : "停止任务失败",
      }));
    } finally {
      setCanceling(false);
    }
  };

  return (
    <div className="flex h-full flex-col gap-4">
      <div className="grid gap-3 md:grid-cols-3">
        <div className={`rounded-2xl border px-4 py-3 ${statusTone}`}>
          <div className="text-[11px] uppercase tracking-[0.18em] opacity-70">
            {t("taskLog.status")}
          </div>
          <div className="mt-1 text-sm font-semibold">
            {getTaskStatusText(currentStatus, language)}
          </div>
        </div>
        <div className="rounded-2xl border border-[var(--border)] bg-[var(--bg-hover)] px-4 py-3">
          <div className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">
            {t("taskLog.progress")}
          </div>
          <div className="mt-1 text-sm font-semibold text-[var(--text-primary)]">
            {progress.label || task?.progress || "0/0"}
          </div>
        </div>
        <div className="rounded-2xl border border-[var(--border)] bg-[var(--bg-hover)] px-4 py-3">
          <div className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">
            {t("taskLog.events")}
          </div>
          <div className="mt-1 text-sm font-semibold text-[var(--text-primary)]">
            {logView === "summary" && visibleEvents.length !== events.length
              ? t("taskLog.filteredLogCount", {
                  visible: visibleEvents.length,
                  total: events.length,
                })
              : t("taskLog.logCount", { count: events.length })}
          </div>
        </div>
      </div>

      <div className="h-2 overflow-hidden rounded-full bg-[var(--bg-hover)] ring-1 ring-[var(--border)]">
        <div
          className={`h-full rounded-full transition-all duration-500 ${
            currentStatus === "failed"
              ? "bg-red-400"
              : currentStatus === "succeeded"
                ? "bg-emerald-400"
                : "bg-sky-400"
          }`}
          style={{
            width: `${progressTotal > 0 ? progressPercent : isTerminalTaskStatus(currentStatus) ? 100 : 18}%`,
          }}
        />
      </div>

      {errorText ? (
        <div className="rounded-2xl border border-red-400/35 bg-red-500/10 px-4 py-3 text-sm text-red-100">
          <div className="mb-1 font-semibold">
            {t("taskLog.failureReason")}
          </div>
          <div className="break-words text-red-100/85">{friendlyError}</div>
        </div>
      ) : null}

      {failureSummary.length > 0 ? (
        <div className="rounded-lg border border-amber-400/30 bg-amber-400/[0.07] px-4 py-3">
          <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-amber-200">
            <TriangleAlert className="h-4 w-4" />
            注册失败分类
          </div>
          <div className="space-y-2">
            {failureSummary.map((item) => (
              <div key={item.code} className="grid gap-1 border-t border-amber-300/15 pt-2 first:border-t-0 first:pt-0 sm:grid-cols-[150px_70px_minmax(0,1fr)]">
                <div className="text-xs font-medium text-[var(--text-primary)]">{item.label}</div>
                <div className="text-xs text-amber-200">{item.count} 次</div>
                <div className="truncate text-xs text-[var(--text-muted)]" title={item.sample || ''}>{item.sample || '-'}</div>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {sub2Sync && Number(sub2Sync.attempted || 0) > 0 ? (
        <div className="rounded-lg border border-sky-400/30 bg-sky-400/[0.07] px-4 py-3">
          <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-sky-200">
            <Check className="h-4 w-4" />
            Sub2 自动上传
          </div>
          <div className="grid gap-2 text-xs sm:grid-cols-3 lg:grid-cols-6">
            <div>已尝试：{Number(sub2Sync.attempted || 0)}</div>
            <div className="text-emerald-300">已上传：{Number(sub2Sync.synced || 0)}</div>
            <div className="text-amber-300">等待：{Number(sub2Sync.pending || 0)}</div>
            <div className="text-orange-300">冷却：{Number(sub2Sync.cooling || 0)}</div>
            <div className="text-red-300">失效：{Number(sub2Sync.invalid || 0)}</div>
            <div className="text-red-300">失败：{Number(sub2Sync.failed || 0)}</div>
          </div>
          {Array.isArray(sub2Sync.items) && sub2Sync.items.some(item => item.error) ? (
            <div
              className="mt-2 truncate text-xs text-[var(--text-muted)]"
              title={sub2Sync.items.find(item => item.error)?.error || ''}
            >
              {sub2Sync.items.find(item => item.error)?.email || '-'}：
              {sub2Sync.items.find(item => item.error)?.error || '-'}
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">
            {t("taskLog.liveLog")}
          </div>
          <div className="mt-1 text-sm font-medium text-[var(--text-primary)]">
            {t("taskLog.liveTitle")}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <div className="inline-flex rounded-md border border-[var(--border)] bg-[var(--bg-input)] p-0.5">
            <button
              type="button"
              onClick={() => setLogView("summary")}
              className={`inline-flex h-7 items-center gap-1.5 rounded px-2 text-xs ${
                logView === "summary"
                  ? "bg-[var(--bg-hover)] text-[var(--text-primary)]"
                  : "text-[var(--text-muted)] hover:text-[var(--text-secondary)]"
              }`}
            >
              <ListFilter className="h-3.5 w-3.5" />
              {t("taskLog.summaryView")}
            </button>
            <button
              type="button"
              onClick={() => setLogView("all")}
              className={`inline-flex h-7 items-center gap-1.5 rounded px-2 text-xs ${
                logView === "all"
                  ? "bg-[var(--bg-hover)] text-[var(--text-primary)]"
                  : "text-[var(--text-muted)] hover:text-[var(--text-secondary)]"
              }`}
            >
              <ScrollText className="h-3.5 w-3.5" />
              {t("taskLog.allView")}
            </button>
          </div>
          <button
            type="button"
            onClick={copyLogs}
            disabled={visibleEvents.length === 0}
            className="inline-flex h-8 min-w-24 items-center justify-center gap-1.5 rounded-md border border-[var(--border)] bg-[var(--bg-hover)] px-3 text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)] disabled:cursor-not-allowed disabled:opacity-45"
          >
            {copyStatus === "copied" ? <Check className="h-3.5 w-3.5" /> : copyStatus === "failed" ? <TriangleAlert className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
            {copyStatus === "copied" ? t("taskLog.copied") : copyStatus === "failed" ? t("taskLog.copyFailed") : t("taskLog.copyLogs")}
          </button>
          {isCancellableTaskStatus(currentStatus) ? (
            <button
              type="button"
              onClick={cancelTask}
              disabled={canceling}
              className="inline-flex h-8 items-center justify-center gap-1.5 rounded-md border border-red-400/35 bg-red-400/10 px-3 text-xs text-red-300 hover:bg-red-400/20 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {canceling ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <CircleStop className="h-3.5 w-3.5" />
              )}
              {canceling ? "正在停止" : "停止任务"}
            </button>
          ) : null}
        </div>
      </div>

      <div className="min-h-[260px] flex-1 overflow-y-auto rounded-xl border border-[var(--border)] bg-[var(--bg-input)] p-3 font-mono text-xs">
        {visibleEvents.length === 0 ? (
          <div className="flex h-full min-h-[180px] items-center justify-center rounded-2xl border border-dashed border-[var(--border)] text-[var(--text-muted)]">
            {t("taskLog.waiting")}
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            {groups.map((group) => {
              if (group.id === MAIN_GROUP_ID && group.events.length === 0) {
                return null;
              }
              return (
                <LogGroupView
                  key={group.id}
                  group={group}
                  collapsed={!!collapsed[group.id]}
                  isMain={group.id === MAIN_GROUP_ID}
                  onToggle={() => toggleGroup(group.id)}
                />
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * 单个分组（主任务或一个 worker）。
 *
 * 用 React 自身的虚拟 DOM diff 渲染日志列表，关键点：
 *   - 每条事件用稳定的 ``id`` 当 key（避免 React 整列重渲），
 *   - 折叠时 events 被卸载，DOM 不留滞；展开时按 100 条上限做软裁剪
 *     （单 worker 一般 50~80 条事件，超过 100 条只显示最近的 100 条，
 *     底部加提示让用户知道历史被截断），保护极端长任务不挂死浏览器。
 */
const MAX_VISIBLE_PER_GROUP = 120;

function LogGroupView({
  group,
  collapsed,
  isMain,
  onToggle,
}: {
  group: LogGroup;
  collapsed: boolean;
  isMain: boolean;
  onToggle: () => void;
}) {
  const { t } = useI18n();
  const total = group.events.length;
  const truncated = total > MAX_VISIBLE_PER_GROUP;
  const visible = truncated
    ? group.events.slice(total - MAX_VISIBLE_PER_GROUP)
    : group.events;
  const logScrollRef = useRef<HTMLDivElement>(null);

  // 展开时新事件到来自动滚到底部
  useEffect(() => {
    if (collapsed) return;
    const frame = window.requestAnimationFrame(() => {
      const element = logScrollRef.current;
      if (element) element.scrollTop = element.scrollHeight;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [collapsed, total]);

  return (
    <div className="overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--bg-pane)]/40">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center gap-2 border-b border-[var(--border)] bg-[var(--bg-hover)]/60 px-3 py-1.5 text-left text-[11px] uppercase tracking-[0.16em] text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
      >
        {collapsed ? (
          <ChevronRight className="h-3.5 w-3.5" />
        ) : (
          <ChevronDown className="h-3.5 w-3.5" />
        )}
        <span className="truncate">
          {isMain ? t("taskLog.mainGroup") : group.label}
        </span>
        <span className="ml-auto text-[10px] text-[var(--text-muted)]">
          {t("taskLog.logCount", { count: total })}
        </span>
      </button>
      {!collapsed && (
        <div ref={logScrollRef} className="max-h-[280px] overflow-y-auto px-2 py-2">
          {truncated && (
            <div className="mb-2 rounded border border-amber-400/30 bg-amber-400/10 px-2 py-1 text-[10px] text-amber-200">
              {t("taskLog.truncatedHint", {
                shown: MAX_VISIBLE_PER_GROUP,
                total,
              })}
            </div>
          )}
          <div className="space-y-1">
            {visible.map((ev) => (
              <div
                key={ev.id}
                className={`rounded-md border border-white/5 bg-white/[0.025] px-3 py-1.5 leading-5 ${classifyLine(ev.line, ev.level)}`}
              >
                {ev.line}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
