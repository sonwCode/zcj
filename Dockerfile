# Stage 1: 构建前端
FROM node:20-slim AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2: Python 后端 + 运行环境
FROM python:3.12-slim

# 使用国内 Debian 镜像源（中科大，大文件下载更稳定）
RUN sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list.d/debian.sources
# 系统依赖：Xvfb、x11vnc、noVNC、浏览器运行时库
# 注：Chromium 本体由后续 playwright install 安装，不通过 apt（包太大，国内易断连）
# 注：tzdata 是刻意保留的显式依赖。身份画像会用 ZoneInfo 把时区渲染成
#     JS 的 Date.toString()，缺 tzdata 时 ZoneInfo 会抛异常并回退成 UTC，
#     于是 Sentinel 载荷里的时区与代理出口 IP 不一致（且只在服务器上复现）。
RUN apt-get update && apt-get install -y --no-install-recommends \
    # 虚拟显示 + VNC
    xvfb x11vnc \
    # noVNC 依赖
    novnc websockify \
    # 其他
    curl ca-certificates tzdata fonts-liberation libnss3 libatk-bridge2.0-0 \
    libdrm2 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libxkbcommon0 \
    libasound2 libpango-1.0-0 libcairo2 libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装 Python 依赖（使用清华 pip 镜像加速）
COPY requirements.txt ./
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 安装 patchright/playwright 浏览器（Solver 使用）
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps chromium

# Camoufox is downloaded on demand only when that browser backend is selected.
# The cloud deployment defaults to Chromium, so the image stays independent of
# the external GitHub release download during first boot.

# 复制后端代码
ARG APP_VERSION=dev
ARG APP_GIT_SHA=unknown
ARG APP_BUILD_TIME=unknown
COPY . .
# 注入版本号
RUN printf '__version__ = "%s"\n__git_sha__ = "%s"\n__build_time__ = "%s"\n' \
    "${APP_VERSION}" "${APP_GIT_SHA}" "${APP_BUILD_TIME}" > core/version.py
# 不需要 .venv 和 frontend 源码
RUN rm -rf .venv frontend

# 复制前端构建产物
COPY --from=frontend-builder /app/static ./static

# 启动脚本
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# APP_PASSWORD: 运行时通过 -e APP_PASSWORD=xxx 设置。
# 入口脚本会在监听非回环地址却未设置密码时拒绝启动（可用 ZCJ_ALLOW_INSECURE=1 跳过，仅限本机自用）。
ENV APP_PASSWORD=""

# 只声明真正的服务端口。noVNC 的 6080 默认不启动也不建议发布，
# 需要人工接管浏览器时再显式 VNC_ENABLED=1 并自行 -p 映射到回环。
EXPOSE 8000

# 就绪探针：/api/ready 免鉴权，且真的会连一次数据库、列一次平台注册表，
# 未就绪时返回 503，编排器/负载均衡可以直接据此摘流量。
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${APP_PORT:-8000}/api/ready" || exit 1

ENTRYPOINT ["/docker-entrypoint.sh"]
