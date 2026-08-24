# ZCJ Account Manager

ZCJ 是一个自托管的账号注册与资产管理系统。主服务将多平台账号注册、邮箱/接码/代理资源、持久化任务、账号凭证、有效性检测、订阅状态和外部系统同步集中在一个 FastAPI 后端中，并提供 React 管理端。

当前仓库是项目重整前可公开的源码基线。运行数据库、日志、HAR、账号导出、代理凭证、支付/短信资料、本地指纹模板、一次性运维脚本、社群图片、本地备份和构建产物均不纳入版本库。

## 主要能力

- 多平台插件化注册，支持协议、无头浏览器和前台浏览器执行器
- 邮箱、验证码、接码和代理 Provider 配置
- 可持久化、可取消、支持并发分 lane 的后台任务
- 账号资产、平台凭证、Provider 账号与资源的统一存储
- ChatGPT 账号检测、Token 恢复、Codex OAuth、Workspace 与 Plus 相关流程
- Sub2API、Any2API、CPA 等外部系统导出或同步
- Docker、Electron/PyInstaller 与普通 Uvicorn 三种运行方式

## 目录结构

```text
api/                    FastAPI 路由
application/            用例服务与任务编排
core/                   数据库、注册抽象、调度、生命周期和资源池
domain/                 领域数据结构
infrastructure/         Repository 与平台运行适配
platforms/              各平台注册和平台动作实现
providers/              邮箱、验证码、接码、代理驱动
services/               任务运行时与本地 Solver 管理
frontend/               React/Vite 管理端源码
customer_portal_api/    实验性的独立用户门户服务
tests/                  后端测试
scripts/                不含凭据的公开维护脚本
```

## 本地运行

环境建议：Python 3.12、Node.js 20。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

Copy-Item .env.example .env
python -m uvicorn main:app --host 0.0.0.0 --port 8001
```

前端开发：

```powershell
Set-Location frontend
npm ci
npm run dev
```

前端生产构建会输出到仓库根目录的 `static/`，该目录属于构建产物，不提交到 Git。

## Docker

先从 `.env.example` 创建 `.env` 并设置至少一个访问密码，再启动：

```bash
docker compose up --build -d
```

默认映射为宿主机 `8001` 到容器 `8000`。SQLite 数据通过 `./data` 持久化。

## 测试

```powershell
pytest -q
```

快速验证已启动的主服务：

```powershell
python scripts/smoke.py http://127.0.0.1:8001/api
```

## 注册与自动检测可靠性

- 批量注册按“目标成功数”补位：单个邮箱建号、手机验证、Codex 凭据或首次存活复检失败，只结束当前尝试，不会提前终止整个批次。
- 并发 worker 使用独立任务参数、邮箱租约和接码 activation；目标 5、并发 5 时五个已启动账号会各自进入接码阶段，不会被进程级号码复用锁串行化。固定邮箱仅允许单账号、单并发，数据库以 `platform + email` 防止重复账号行。
- 同一账号从邮箱注册进入手机验证和 Codex OAuth 时，会延续自己的设备 ID 与登录 Cookie；会话档案只在该账号内部复用，不跨账号共享。
- ChatGPT 注册默认启用连续代理/网络故障熔断，连续 3 次后停止投放新账号并允许在途 worker 收尾；可用任务 `extra.network_circuit_break_threshold`（0–20，0 表示关闭）调整。
- ChatGPT 手机流程在注册完成后先做即时存活检查，随后进入持久化的 60 秒持续复检。服务重启后排程仍保留；检测有效或网络状态未知都会在下一分钟复检，明确失效后记录“最后确认有效—首次发现失效”的时间窗口并停止该账号的高频监控。
- 持续复检默认 5 并发，调度器每 5 秒扫描到期队列；已有的有效 ChatGPT 账号会自动迁入持续监控。较慢的全量检测使用独立周期。`GET /api/system/scheduler/status` 可查看调度线程、最近开始/完成时间、调度延迟、结果和错误，避免后台检测静默失效；旧的 `/api/scheduler/status` 地址仍兼容。
- 注册任务和启动日志会输出 `version@git_sha`；`GET /api/system/version` 返回提交、构建时间和进程启动时间，用于确认当前实例是否已部署到新代码。
- 可在任务 `extra` 中通过 `post_registration_liveness_delay_seconds`（0–600）、`post_registration_probation_enabled` 和 `post_registration_probation_interval_seconds`（默认 60）调整新号检测；全量与持续检测并发读取配置项 `account_check_concurrency`（1–20）。
- 注册成功与外部交付分开记录：账号落库后即保留 `registration_status=registered`；Sub2API 未完成会记录 `delivery_status=pending`，等待后台补传，不再把两种状态混为一个“失败”。
- CPA 连接检测使用只读的 Management API `GET /v0/management/auth-files`；只有 2xx 才判定连接成功，401/403 会分别提示管理密钥无效或远程管理未开启。

## 配置与数据安全

- `.env`、数据库、日志、账号文本、HAR、Token 导出和本地备份不得提交。
- `tools/`、私有指纹模板及带有真实账号、手机号、支付资料或访问参数的操作素材不进入公开仓库。
- API Key、账号密码和代理认证信息应通过 `.env`、Provider 设置或部署 Secret 注入。
- 公网部署必须设置 `APP_PASSWORD`，并在反向代理层配置 TLS 与访问控制。
- 提交前请检查 `git status`，确认没有运行数据或临时诊断文件进入暂存区。

## 当前整理方向

项目正在按以下顺序重整：先固定可回滚源码基线，再统一账号状态与凭证模型，随后收敛调度/生命周期职责，最后拆分 ChatGPT 注册、检测、付款和外部同步边界。重整期间以测试和数据库兼容为前提，不直接迁移或删除现有账号数据。

## License

见 [LICENSE](LICENSE)。
