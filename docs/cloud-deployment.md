# 云服务器部署

这份文档记录 ZCJ 部署到云主机（单机 Docker / docker compose）时的约束与默认值。
目标是把"本地能跑"和"公网上能安全地一直跑"之间的差距显式写出来，而不是靠部署者自己踩。

## 1. 一句话结论

ZCJ **只能单进程**运行。要扩容就增加容器数量（每个容器一份独立数据库），
不要给 uvicorn 加 `--workers`。

## 2. 启动入口的默认行为

`docker-entrypoint.sh` 按"最小暴露面"启动，三个硬性检查会在启动前直接退出：

| 条件 | 行为 |
| --- | --- |
| `UVICORN_WORKERS` 不为空且不等于 1 | 拒绝启动 |
| 监听地址不是回环，但 `APP_PASSWORD` 为空 | 拒绝启动 |
| `VNC_ENABLED=1` 但 `VNC_PASSWORD` 为空 | 拒绝启动 |

三个检查都有显式逃生口（`ZCJ_ALLOW_INSECURE`、`ZCJ_ALLOW_INSECURE_VNC`、
`APP_HOST=127.0.0.1`），但都需要你明确写出来。

### 为什么必须单进程

`main.py` 的 lifespan 会在**进程内**启动四个后台循环：

```
scheduler / task_runtime / solver_manager / lifecycle_manager
```

uvicorn 的每个 worker 都是独立进程，各自会再跑一遍 lifespan。于是同一个任务被
重复派发、同一个账号被并发探测、同一个代理被重复预占，而且日志里看不出来——
每个 worker 都觉得自己是对的。这不是性能问题，是数据正确性问题。

## 3. 必填环境变量

```bash
ZCJ_APP_PASSWORD=<强密码>   # compose 里是必填的，留空直接报错退出
```

`APP_PASSWORD` 为空时 `AuthMiddleware` 会放行所有 `/api` 请求，实例持有的账号口令、
平台 Token 和代理凭据都会对任何能访问端口的人开放。compose 用
`${ZCJ_APP_PASSWORD:?...}` 强制要求；直接 `docker run` 时由入口脚本兜底。

## 4. 虚拟显示与指纹的关系

有头浏览器需要一个 X display，入口脚本默认起 Xvfb（`XVFB_ENABLED=1`）。
这里有一个容易忽略的耦合：

* **有头模式**下，页面的 `screen.width/height` 来自 Xvfb 的尺寸，
  **所有账号共用同一个值**。
* **headless 模式**下，`screen` 跟随每个 context 自己的 viewport，
  所以身份画像里的屏幕尺寸才真正起到"每个会话不一样"的作用。

原来 Xvfb 固定 `1280x800x24`，比画像里任何一档屏幕都小，会把 viewport 卡住。
现在默认 `1920x1080x24`，可用 `XVFB_SCREEN` / `ZCJ_XVFB_SCREEN` 覆盖。

两种模式下 Sentinel 载荷都不会自相矛盾：浏览器路径会用
`_browser_profile_from_page()` 把页面真实上报的 `screen`/`locale`/时区/核心数读回来覆盖画像。
差别只在于**跨账号的多样性**。要多样性就用 headless。

## 5. 远程接管浏览器（VNC）默认关闭

原来的入口脚本无条件启动 `x11vnc`，且在没有 `VNC_PASSWORD` 时用 `-nopw`，
`websockify` 监听所有网卡。也就是说只要把 6080 映射出去，任何人都能接管一个
已经登录了账号的浏览器会话。本地用无所谓，公网上是一扇敞开的门。

现在：

* 默认**完全不启动** VNC；
* 需要时 `VNC_ENABLED=1`，且必须给 `VNC_PASSWORD`；
* x11vnc 固定 `-localhost`，noVNC 默认只绑 `127.0.0.1`；
* Dockerfile 只 `EXPOSE 8000`，不再把 6080 写进镜像元数据。

需要看的时候建议走 SSH 端口转发，而不是直接映射端口：

```bash
ssh -L 6080:127.0.0.1:6080 user@server
```

## 6. 健康检查

`/api/ready` 免鉴权，会真的连一次数据库、列一次平台注册表；
**未就绪时返回 503**（原来无论是否就绪都返回 200，编排器只能去解析 body）。

`/api/health` 保持 200，它是存活探针：进程活着但依赖挂了，重启进程并不能解决问题。

镜像里带了 `HEALTHCHECK`，compose 里也写了一份，`docker ps` 能直接看到健康状态。

## 7. 资源与优雅退出

| 项 | 值 | 原因 |
| --- | --- | --- |
| `shm_size` | `1gb` | 容器默认 `/dev/shm` 只有 64MB，Chrome 页面一多就崩。启动参数里已有 `--disable-dev-shm-usage` 兜底，但那是退化路径 |
| `stop_grace_period` | `45s` | 必须大于 `APP_GRACEFUL_SHUTDOWN_SECONDS`（默认 30s）。docker 默认 10s 就 SIGKILL，正在跑的注册会被腰斩 |
| `deploy.resources.limits.memory` | `4g`（`ZCJ_MEMORY_LIMIT` 可调） | 浏览器内存泄漏会拖垮整台机器 |
| `restart` | `unless-stopped` | |

### 重启后会发生什么

`services/task_runtime.py` 启动时会调用 `mark_incomplete_tasks_interrupted()`，
把上次进程死掉时还在跑的任务标记为 `interrupted`，所以重启不会留下"永远 running"的僵尸任务。

配合 `ZCJ_RESUME_REGISTRATION`（默认关），已经创建但没走完的账号可以被续跑，
而不是重新注册一遍。

## 8. 时区数据

身份画像用 `ZoneInfo` 把时区渲染成 JS 的 `Date.toString()`。缺 tzdata 时
`ZoneInfo` 抛异常、渲染静默退化成 UTC，载荷里的时区就和代理出口 IP 对不上，
**而且只在精简镜像上复现**。

`python:3.12-slim` 本身已经装了 tzdata（见上游 Dockerfile 的 runtime dependencies），
本仓库的 Dockerfile 仍然把它显式列出来，避免哪天换基础镜像时静默退化。
哨兵自检新增 `timezone_data` 一项，缺数据会直接报失败而不是悄悄放过。

## 9. 磁盘增长（长期运行的主要风险）

`task_events` 表**每写一行日志就插一条记录**，而 `TaskLogger.log()` 每条都单独
`commit()`。`browser_register.py` 一个文件里就有 200+ 处 `log()` 调用，
一次注册轻轻松松上百条事件。而 `TaskLogsRepository` 只有读方法——
**从来没有任何东西删除过这张表**。

服务器连续跑下去，两件事会同时发生：

1. SQLite 主文件无限增长；
2. WAL 模式下 `-wal` 文件跟着涨，而且因为没开 `auto_vacuum`，
   删了行也不会把空间还给文件系统。

现在由 `core/retention.py` 在既有维护周期里清理：

| 环境变量 | 默认 | 作用 |
| --- | --- | --- |
| `ZCJ_TASK_EVENT_RETENTION_DAYS` | `14` | 按时间保留；设 0 关闭 |
| `ZCJ_TASK_EVENT_MAX_ROWS` | `200000` | 硬行数上限，超了删最旧的；设 0 关闭 |
| `ZCJ_RETENTION_VACUUM` | 未设置 | 设 1 才在清理后跑完整 `VACUUM` 归还磁盘 |

几个刻意的选择：

* **分批删除**（默认 5000 一批），不会为了清理把写锁占很久；
* **不动正在跑的任务的事件**——前端正在看这些进度，删了只丢历史不省磁盘；
* **不清理 `task_logs`**——那是账号的成功/失败记录，是数据不是日志；
* **`VACUUM` 默认关闭**——它要独占锁重写整个库，放在实时服务上要自己确认时机。

`tests/test_task_event_retention.py` 覆盖了窗口、上限、运行中任务保护、
dry-run、分批和关闭开关。

### 另外两处会写盘的地方

* `platforms/chatgpt/browser_register.py` 的 `_dump_debug()` 会在失败时往
  `/tmp` 写截图和 HTML。前缀是固定的（如 `chatgpt_password_fail`），
  所以是覆盖而不是堆积；但 `/tmp` 在容器里属于可写层，重启才清。
* `platforms/chatgpt/cpa_session.py` 会把截图和 JSON 写到
  `OPAI_DEBUG_WORKSPACE_DIR`（默认 `debug/workspace_step2`，相对路径）。
  文件名带时间戳，**会一直累积**。不需要排障就把它指到 `/tmp` 或定期清理。

## 10. 部署前预检

`scripts/cloud_preflight.py` 把"只在服务器上才复现"的问题在启动前查一遍。
只读、不联网，有 FAIL 时退出码非零：

```bash
python3 scripts/cloud_preflight.py          # 完整报告
python3 scripts/cloud_preflight.py --quiet  # 只在有警告/失败时输出
```

入口脚本默认会在启动前跑一次（`--quiet`），结果进容器日志；
设 `ZCJ_PREFLIGHT=0` 可关掉。它失败不阻止启动——硬性拦截在入口脚本的 guard 里。

覆盖的检查：

| 检查 | 为什么 |
| --- | --- |
| Python 版本 ≥ 3.10 | 代码用了 `X \| None` 语法 |
| 核心依赖可导入 | fastapi / sqlmodel / sqlalchemy / pydantic / requests / curl_cffi（前五个缺失会导致服务起不来） |
| **时区库 33 个地区全部可解析** | 缺 tzdata 会让载荷时区退化成 UTC，与出口 IP 矛盾，且只在精简镜像上复现 |
| `APP_PASSWORD` | 为空等于所有 `/api` 接口无鉴权 |
| `UVICORN_WORKERS` | 多 worker 会重复派发任务 |
| X display | 设了 `DISPLAY` 但对应的 socket 不存在 → 有头浏览器起不来（`:99` 与 `:99.0` 指同一个 socket） |
| Chrome/Chromium | 浏览器路径需要 |
| `/dev/shm` ≥ 256MB | Docker 默认 64MB，Chrome 会崩 |
| 数据库目录与**已存在的库文件**都可写 | 挂载卷权限；只读的库文件会在首次写入时才报错 |
| 磁盘剩余 ≥ 2GB | 任务事件表持续增长 |
| 日志保留未关闭 | 全关掉就是无限增长 |
| VNC 有密码且绑回环 | 否则等于开放远程接管 |
| 凭据加密已生效 | vault 失效时会**静默**降级为明文写入（缺 PyNaCl、数据卷不可写） |

测试分布：`tests/test_cloud_preflight.py`（15 个，AuthMiddleware / 单进程 / 保留策略 /
VNC / 时区的分支），另有 `tests/test_preflight_database_path.py`、
`tests/test_preflight_auth_bind.py`、`tests/test_preflight_display.py` 分别覆盖
数据库路径、监听地址与 X display。

`tests/test_preflight_chrome.py` 覆盖浏览器发现（含 `PLAYWRIGHT_BROWSERS_PATH` 下
带版本号的目录布局）；`tests/test_preflight_vault.py` 覆盖凭据加密的四种结论
（正常 / 缺 PyNaCl / 密钥不可写 / 显式关闭）。

`tests/test_preflight_dependencies.py` 覆盖核心依赖清单（逐个屏蔽导入验证会 FAIL）；
`tests/test_preflight_db_file.py` 覆盖只读库文件必须 FAIL。

`check_python` / `check_shm` 目前**没有**专门的用例
（它们只做存在性与阈值判断，且依赖具体环境）。

## 11. 可选组件：GoPay 协议 SDK

`platforms/gopay/` 依赖一个**不随本仓库分发**的协议 SDK（包名 `opai`）。
`.gitignore` 排除了 `platforms/gopay-deploy/`，所以**任何全新克隆（包括服务器）
都没有它**。

两种提供方式：

1. 把 SDK 放到 `platforms/gopay-deploy/app/src/`（`_opai_loader` 会自动注入 `sys.path`）；
2. 或 `pip install -e .` 到 site-packages，使其可直接 `import opai`。

缺失时只有 GoPay 相关功能不可用，其余平台不受影响。
`ensure_opai_on_path()` 会抛出明确错误并给出期望路径，而不是让调用方在几层之后
撞上一个光秃秃的 `No module named 'opai'`。

部署前想确认，可以跑：

```bash
python3 -c "from platforms.gopay._opai_loader import opai_available; print(opai_available())"
```

## 12. 可选组件：客户门户（`customer_portal_api/`）

仓库里的 `customer_portal_api/` 是**独立的第二个 FastAPI 应用**，监听 `8100`。
**容器不会启动它**——`docker-entrypoint.sh`、`Dockerfile` 与根 `docker-compose.yml`
都只拉起主服务（8000）。照着本文档部署完，门户是不存在的，需要就要**另外单独起一个进程**。

它有自己的部署材料，不必重复本文档：

- `customer_portal_api/README.md`：本地启动与 Docker 两种方式
- `customer_portal_api/Dockerfile`、`customer_portal_api/docker-compose.yml`
- `customer_portal_api/.env.example`：变量模板

在生产上需要注意三点（细节见 `SECURITY.md`「部署加固」）：

1. **不存在出厂口令**：`PORTAL_ADMIN_PASSWORD` 留空时会生成一次性随机口令并打印到
   启动日志；`PORTAL_JWT_SECRET` 留空时会生成随机密钥并持久化到数据库同目录的
   `.portal_jwt_secret`。该文件**必须随数据库一起备份**，且不能落在临时文件系统上，
   否则每次重启换密钥、所有已登录用户被登出。
2. 支付回调**默认拒绝**：未配置 `PORTAL_PAYMENT_SECRET_<渠道>` 或
   `PORTAL_PAYMENT_CALLBACK_SECRETS` 时一律 403。
   `PORTAL_PAYMENT_ALLOW_UNSIGNED_CALLBACKS` 只用于本地联调，**不要在生产开启**。
3. 默认使用**门户自己的库**（`customer_portal_api/customer_portal.db`）。要让门户与主
   服务共用同一个 SQLite 库，需显式设置 `PORTAL_DATABASE_URL`；此时两个进程会同时写库，
   需确认并发写入与备份策略都能覆盖。

## 13. 横向扩容

因为调度器是进程内单例，扩容单位是**容器**，不是 worker：

```
          ┌─ zcj-1 (db-1.sqlite)
nginx ────┼─ zcj-2 (db-2.sqlite)
          └─ zcj-3 (db-3.sqlite)
```

注意 SQLite 是文件库：多个容器**不能共享同一个 db 文件**（NFS 上尤其不行）。
每个实例要有自己的 `./data`。如果需要统一视图，那是另一个量级的改造（换 Postgres），
不在当前范围内。
