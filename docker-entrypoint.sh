#!/bin/bash
# ---------------------------------------------------------------------------
# 云服务器部署入口。
#
# 默认按"最小暴露面"启动：
#   * 虚拟显示只用来让有头浏览器能跑，VNC 默认完全关闭；
#   * 一旦打开 VNC，必须给密码，且只监听回环；
#   * 对外监听地址不是回环时，必须有 APP_PASSWORD。
#
# 所有开关都有默认值，直接 `docker run` 也不会意外把控制台或浏览器会话暴露出去。
# ---------------------------------------------------------------------------
set -euo pipefail

APP_HOST="${APP_HOST:-0.0.0.0}"
APP_PORT="${APP_PORT:-8000}"
APP_GRACEFUL_SHUTDOWN_SECONDS="${APP_GRACEFUL_SHUTDOWN_SECONDS:-30}"
XVFB_DISPLAY="${XVFB_DISPLAY:-:99}"
# 有头浏览器下，Xvfb 的尺寸就是页面 screen.width/height 的取值。
# 原来固定 1280x800：比画像里任何一档屏幕都小，会把视口卡住，
# 而且所有账号共用同一个偏小的桌面尺寸，本身就是一个聚类特征。
XVFB_SCREEN="${XVFB_SCREEN:-1920x1080x24}"

log() { echo "[entrypoint] $*"; }
fatal() { echo "[entrypoint][FATAL] $*" >&2; exit 1; }

# --- 单进程约束 -------------------------------------------------------------
# main.py 的 lifespan 会在进程内拉起 scheduler / task_runtime / solver_manager /
# lifecycle_manager 四个后台循环。uvicorn 多 worker 会让每个 worker 各起一份，
# 于是同一个任务被重复派发、同一个账号被并发探测、同一个代理被重复预占。
# ZCJ 目前只支持单进程，所以这里直接拒绝，而不是让它静默地出错。
if [ -n "${UVICORN_WORKERS:-}" ] && [ "${UVICORN_WORKERS}" != "1" ]; then
    fatal "UVICORN_WORKERS=${UVICORN_WORKERS} 不受支持：后台调度器是进程内单例，多 worker 会重复派发任务、重复探测账号。请保持单进程，靠增加容器数量横向扩容（每个容器一份独立数据库）。"
fi

# --- 鉴权 -------------------------------------------------------------------
# AuthMiddleware 在 APP_PASSWORD 为空时放行所有 /api 请求。容器默认监听 0.0.0.0，
# 端口一旦发布出去，任何人都能拿到账号口令、平台 Token 和代理凭据。
if [ -z "${APP_PASSWORD:-}" ]; then
    if [ "${ZCJ_ALLOW_INSECURE:-0}" = "1" ]; then
        log "[WARN] APP_PASSWORD 为空且 ZCJ_ALLOW_INSECURE=1：所有 /api 接口无需鉴权，仅限本机自用。"
    elif [ "${APP_HOST}" = "127.0.0.1" ] || [ "${APP_HOST}" = "localhost" ]; then
        log "[WARN] APP_PASSWORD 为空，但只监听 ${APP_HOST}，未对外暴露。"
    else
        fatal "未设置 APP_PASSWORD 却监听 ${APP_HOST}：所有 /api 接口将无需鉴权。请设置 APP_PASSWORD；仅本机自用时显式设 ZCJ_ALLOW_INSECURE=1 或 APP_HOST=127.0.0.1。"
    fi
fi

# --- 虚拟显示 ---------------------------------------------------------------
# 有头浏览器需要 X display。Xvfb 很便宜，默认总是启动；
# 纯 headless 部署可以设 XVFB_ENABLED=0 省掉它。
if [ "${XVFB_ENABLED:-1}" = "1" ]; then
    Xvfb "${XVFB_DISPLAY}" -screen 0 "${XVFB_SCREEN}" -nolisten tcp &
    export DISPLAY="${XVFB_DISPLAY}"
    _sock="/tmp/.X11-unix/X${XVFB_DISPLAY#:}"
    for _ in $(seq 1 50); do
        [ -e "${_sock}" ] && break
        sleep 0.1
    done
    log "Xvfb 已启动: ${XVFB_DISPLAY} ${XVFB_SCREEN}"
fi

# --- VNC（默认关闭） ---------------------------------------------------------
# noVNC/x11vnc 可以直接接管浏览器会话，等于把已登录的账号送出去。
# 需要人工盯页面时再 VNC_ENABLED=1，并且必须给密码。
if [ "${VNC_ENABLED:-0}" = "1" ]; then
    VNC_BIND="${VNC_BIND:-127.0.0.1}"
    VNC_PORT="${VNC_PORT:-6080}"
    if [ -z "${VNC_PASSWORD:-}" ]; then
        if [ "${ZCJ_ALLOW_INSECURE_VNC:-0}" = "1" ]; then
            log "[WARN] VNC 无密码且 ZCJ_ALLOW_INSECURE_VNC=1，仅监听 ${VNC_BIND}。"
            x11vnc -display "${XVFB_DISPLAY}" -nopw -forever -shared -localhost -rfbport 5900 &
        else
            fatal "VNC_ENABLED=1 但未设置 VNC_PASSWORD：x11vnc 会以 -nopw 启动，任何能连上 ${VNC_PORT} 的人都可以接管浏览器。请设置 VNC_PASSWORD，或显式设 ZCJ_ALLOW_INSECURE_VNC=1（仅本机自用）。"
        fi
    else
        VNC_PASSFILE="$(mktemp)"
        chmod 600 "${VNC_PASSFILE}"
        x11vnc -storepasswd "${VNC_PASSWORD}" "${VNC_PASSFILE}" >/dev/null 2>&1
        x11vnc -display "${XVFB_DISPLAY}" -rfbauth "${VNC_PASSFILE}" -forever -shared -localhost -rfbport 5900 &
    fi
    websockify --web=/usr/share/novnc "${VNC_BIND}:${VNC_PORT}" localhost:5900 &
    log "noVNC 已启动: http://${VNC_BIND}:${VNC_PORT}/vnc.html"
else
    log "VNC 未启用（需要时设 VNC_ENABLED=1 与 VNC_PASSWORD）。"
fi

# --- 部署前预检 -------------------------------------------------------------
# 只读、不联网，把环境问题打进日志而不是让它变成线上才发现的静默退化
# （缺 tzdata 导致时区退化成 UTC、/dev/shm 太小导致 Chrome 崩、磁盘快满等）。
# 硬性拦截在上面几个 guard 里，这里失败也不阻止启动。
if [ "${ZCJ_PREFLIGHT:-1}" = "1" ] && [ -f scripts/cloud_preflight.py ]; then
    python3 scripts/cloud_preflight.py --quiet || true
fi

log "启动 uvicorn: ${APP_HOST}:${APP_PORT}（单进程，优雅退出 ${APP_GRACEFUL_SHUTDOWN_SECONDS}s）"
exec uvicorn main:app \
    --host "${APP_HOST}" \
    --port "${APP_PORT}" \
    --timeout-graceful-shutdown "${APP_GRACEFUL_SHUTDOWN_SECONDS}" \
    --proxy-headers \
    --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"
