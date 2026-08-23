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
