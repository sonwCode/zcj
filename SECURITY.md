# 安全策略 Security Policy

## 支持的版本

本项目处于活跃开发阶段，安全修复仅针对最新发布版本（`main` 分支与最近一次 Release）。请在上报前确认问题在最新版本仍可复现。

## 上报安全漏洞

**请勿通过公开 Issue 上报安全漏洞**，以免在修复前被利用。

请通过以下方式私下上报：

- 使用 GitHub 的 [Private vulnerability reporting](https://docs.github.com/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability)（仓库 Security 标签页 → Report a vulnerability）
- 或邮件联系维护者（在仓库主页查看联系方式）

上报时请尽量包含：

- 受影响的组件 / 文件路径
- 复现步骤或 PoC
- 影响范围评估（数据泄露 / 权限绕过 / RCE 等）
- 建议的修复方向（可选）

我们会在合理时间内确认收到，并在修复发布后与你协调披露时间。

## 使用本项目时的安全须知

本项目会处理账号凭证、登录 token、第三方 API key 等敏感数据，请务必遵循以下实践。

### 凭证与密钥

- **绝不提交真实凭证到版本库**。以下文件已在 `.gitignore` 中忽略，请勿强制提交：
  - 账号导出文件：`acc*.json`、`*_accounts.txt`
  - 数据库：`*.db`（含全部账号凭证与 token）
  - 抓包 / 调试 dump：`*.har`、`*_inspect.txt`、`otp_*.txt`、`logger.txt`、`task_events.txt` 等
- **所有第三方 API key 走环境变量或 Web UI 配置**，不要写死进源码。参考 [.env.example](.env.example)。
- 怀疑任何凭证泄露时，**第一时间在对应平台后台吊销 / 重置**（接码平台 key、代理凭证、平台账号密码与会话）。

### 部署加固

- **主服务**：公网部署务必设置 `APP_PASSWORD` 启用访问鉴权；noVNC 设置 `VNC_PASSWORD`。
- **customer_portal_api（独立门户）**：门户不再发布任何出厂口令或密钥，`config.py` 把
  旧默认值（`change-me-in-production`、`admin123456` 等）一律当作未配置处理：
  - `PORTAL_JWT_SECRET` 未配置时自动生成随机密钥并持久化到 `.portal_jwt_secret`
    （数据库同目录，0600）。**该文件必须随数据库一起备份且不能丢**——它所在的目录若是
    临时文件系统，每次重启都会换密钥，所有已登录用户会被登出。
  - `PORTAL_ADMIN_PASSWORD` 未配置时引导口令是随机生成的，首次启动需从日志中获取。
  - `PORTAL_CORS_ORIGINS` 默认为空（不挂 CORS 中间件），仅在前后端分离调试时才需显式列出。
  - 支付回调**默认拒绝**：未配置 `PORTAL_PAYMENT_SECRET_<CHANNEL>` 或
    `PORTAL_PAYMENT_CALLBACK_SECRETS` 时回调一律 403。切勿为图省事打开
    `PORTAL_PAYMENT_ALLOW_UNSIGNED_CALLBACKS`——那等于让任何人凭空开通付费订阅。
- **端口暴露**：8000 / 6080 / 8889 仅在受信任网络开放；公网部署请置于反向代理 + TLS 之后。

### 数据最小化

- 定期清理不再使用的账号数据与导出文件。
- 不要在公开渠道（Issue、PR、日志粘贴）贴出包含真实 token / cookie / 邮箱密码的内容。

## 鉴权边界（评审红线）

本项目**不得**把自身鉴权绑定到任何外部服务或聊天群组。

- `customer_portal_api` 使用自带的 JWT 鉴权（`PORTAL_JWT_SECRET`），客户凭邮箱密码登录；
  不读取、不校验任何 Telegram / Discord / QQ 群组成员身份。
- 任何外部依赖（群组、第三方 Bot、外部鉴权网关）不可用时，主进程**不得自动关停**；
  只允许该功能降级或报错。
- 参考反例：部分开源项目要求调用方加入指定 Telegram 群，非群成员一律 HTTP 403，
  并会在校验失败时自动终止进程。该模式已明确不采纳，后续 PR 不得引入。

### 凭据加密（Vault）

- `ZCJ_VAULT_KEY` / `ZCJ_VAULT_KEY_FILE` 与 `.zcj_vault_key` 等同于数据库明文密钥，
  泄露即等于全部敏感列泄露；请与数据库备份分开保管。
- `ZCJ_VAULT_DISABLED=1` 只用于迁移或排障，不得在生产长期开启。
- 更换密钥前必须先用旧密钥解密并重新写入，否则历史数据不可读。

### 资源预占

- `resource_reservations` 表只保存资源键（邮箱/号码）与所有者标识，不保存凭据；
- 预占有 TTL，过期会被 `sweep_expired` 回收，不会永久占用池资源。

## 免责声明

本项目仅供学习和研究使用，不得用于任何商业用途，也不得用于违反目标平台服务条款（ToS）的行为。使用本项目所产生的一切后果由使用者自行承担。
