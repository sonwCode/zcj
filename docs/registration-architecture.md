# 注册架构（registration-architecture）

本文描述 ZCJ 账号管理器的注册子系统：一次注册任务从入队到入库经过哪些层、
哪些抽象，以及新增的“入库边界 / 代理分层 / 资源预占 / 失败归因 / 前置检查”
五个可观测点落在什么位置。

## 1. 分层

```
main.py                FastAPI 应用装配、路由挂载、init_db()
  api/                 薄路由层：解析请求、调用 application，不做业务
  application/         编排层：tasks.py（任务执行器）、accounts.py、task_logs.py
  core/                引擎与抽象：base_platform.py、db.py、http_client.py、
                       proxy_pool.py、proxy_strategy.py、vault.py、storage.py、
                       cloudflare_clearance.py、registration/*
  infrastructure/      仓储层：*_repository.py（SQLModel 持久化）
  platforms/           每平台驱动：chatgpt/、cursor/、…
  customer_portal_api/ 独立客户门户（自带鉴权，不依赖任何外部群组）
```

依赖方向自上而下；`core` 不反向依赖 `application`。

## 2. 注册抽象

`core/base_platform.py` 定义契约：

| 名称 | 作用 |
| --- | --- |
| `RegistrationContext` | 一次注册的输入：邮箱、密码、代理、验证码、短信等 |
| `RegistrationArtifacts` | 中间产物：邮箱句柄、短信句柄、浏览器上下文 |
| `RegistrationResult` | 输出：账号、token、状态、原始响应 |
| `RegistrationCapability` | 平台声明自己支持邮箱/短信/OAuth/浏览器中的哪几种 |

适配器（`platforms/*/adapters` 与 `core/registration`）：

- `BrowserRegistrationAdapter` / `BrowserRegistrationFlow`：Playwright/Patchright 有头或无头
- `ProtocolMailboxAdapter` / `ProtocolMailboxFlow`：纯 HTTP + 邮箱收码
- `ProtocolPhoneFlow`：手机号 + 接码
- `ProtocolOAuthAdapter` / `ProtocolOAuthFlow`：OAuth 授权码换 token

规格对象 `OtpSpec`、`LinkSpec` 描述“怎么从邮箱里取到验证码/链接”，
由平台按需提供，避免每个平台重复实现 IMAP/Graph 轮询。

## 3. 派发规则

`BasePlatform.register()` 按以下顺序判定执行路径：

1. `executor_type in ("headless", "headed")` → 浏览器 flow
2. `identity_provider == "oauth_browser"` → OAuth flow
3. `identity_provider == "phone"` → 手机 flow
4. 其余 → 邮箱协议 flow（默认）

## 4. 注册流水线阶段

每个账号的 `account_overview["registration_pipeline"]["stages"]` 记录阶段状态：

```
account_created → phone_verified → credentials_ready → liveness → persisted → payment
```

每个阶段是 `{"status": "...", "error": "...", "detail": {...}, "updated_at": "..."}`。
状态机为 `pending → running → passed | failed | skipped`。

### 4.1 入库边界（新增）

`core/registration/persistence.py` 把“账号可以入库”变成显式判定：

- 边界标识：`BOUNDARY_VERSION = "stable-http-200-at-v1"`
- 判定：结果非空、有身份标识、状态属于成功集合、访问令牌长度 ≥ 20
- 失败码：`empty_result` / `missing_identity` / `status_not_success` /
  `missing_access_token` / `missing_secret`

`application/tasks.py` 在 `persisted` 阶段调用 `evaluate_persistence(account)`：

- **始终**把 `PersistenceDecision.log_fields()` 写入阶段 `detail`，便于事后审计；
- **仅在**显式开启时才拒绝入库：
  `ZCJ_ENFORCE_PERSISTENCE_BOUNDARY=1` 或任务 `extra["enforce_persistence_boundary"]=true`。

默认关闭是刻意的：部分平台返回的令牌字段不同，强行开启会让这些平台全部失败。

## 5. 代理分层解析（新增）

`core/proxy_strategy.py` 实现四层优先级：

```
account（账号自身代理） > stable_runtime（稳定代理运行时）
  > pool（代理池） > explicit（显式传入） > legacy_global（旧版全局）
```

- 默认顺序保持 ZCJ 既有语义：`account > explicit > stable_runtime > pool > legacy_global`，
  因此现有测试与线上行为不变；
- 严格顺序通过 `ZCJ_PROXY_TIER_ORDER=account,stable_runtime,pool,explicit,legacy_global` 开启；
- 池内“粘性/轮换”语义仍由 `ProxyPool` 独占管理（`next_route` / `acquire_route`），
  解析器只负责“选哪一层”，不重复实现租约，避免并发 worker 共享同一出口。

## 6. 资源原子预占（新增）

`infrastructure/reservation_repository.py` + `core/db.py::ResourceReservationModel`：

- 唯一约束 `(pool, resource_key)` 是原子性原语：并发 INSERT 只有一个 COMMIT 成功；
- 已存在的行用条件 UPDATE（比较 `status` 与 `expires_at`）做 compare-and-set；
- `release` / `sweep_expired` 回收；`claim_first` 逐个尝试候选。

已接入本地微软邮箱池：`LocalMicrosoftMailbox.get_email()` 在进程内锁之外再做一次
数据库级预占，失败则跳到下一个候选，因此多进程/多 worker 不会领到同一个邮箱。
永久失败（`mark_registration_failure`）会释放预占；瞬时失败由 TTL 兜底。

## 7. 失败归因（新增）

`core/registration/attribution.py` 把任意错误串映射为稳定分类：

`proxy_blocked` / `rate_limited` / `captcha_fail` / `phone_risk` / `otp_timeout` /
`mailbox_error` / `credential_incomplete` / `network_error` / `upstream_error` / `unknown`

HTTP 状态码优先（407/403→代理，429→限流，5xx→上游），否则按关键词表匹配；
`summarize_attributions(rows)` 输出按数量排序的分桶结果，供看板与离线脚本使用。

## 8. 前置检查（新增）

`core/registration/preflight.py` 在注册开始前做**本地**检查（不联网）：

- Python ≥ 3.10；
- `curl_cffi` 版本；
- HTTP 栈（`curl_cffi`/`httpx`/`requests` 至少一个可用）；
- 浏览器栈（`playwright`/`patchright`/`camoufox`，仅浏览器任务要求）；
- 浏览器指纹档（如 `chrome146`）。

默认只记录日志；`ZCJ_ENFORCE_PREFLIGHT=1` 或 `extra["enforce_preflight"]=true` 时
检查失败会直接终止任务并给出可读原因。

## 9. Agent Identity

`platforms/chatgpt/agent_identity.py` 已实现 Ed25519 身份：本地生成种子、
`crypto_sign_seed_keypair` 派生密钥、密封盒（`crypto_box_seal_open`）解密，
可在支持该模式的平台上跳过短信二次验证。这是既有能力，无需新增依赖。