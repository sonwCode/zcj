# ChatGPT 免费号注册流程（chatgpt-free-registration-flow）

本文把 ZCJ 里「注册一个 ChatGPT 免费号」的完整链路从头到尾理一遍。

> 说明：项目里没有名为 `gptfree` 的平台。`core/sub2api_sync.py:31-32` 里的
> `chatgpt_free` / `chatgpt-free` 是套餐标识，所以这里讲的是 **ChatGPT 免费号**。

## 0. 一句话概览

```
任务入队 → 准备邮箱/代理/短信  → BasePlatform.register() 四路派发
   ├─ headless/headed  → 浏览器 flow   （真浏览器，能跑 post_register 回调）
   ├─ oauth_browser    → OAuth flow
   ├─ phone            → 手机 flow
   └─ 默认             → 协议邮箱 flow（纯 HTTP，主力路径）
→ 拿到 access_token
→ Codex 凭据 / 存活检测 / 入库边界 / 入库
→ （可选）试用监控、Sub2 同步、Workspace Join
```

## 1. 派发：四条路怎么选

`core/base_platform.py:179-228` `BasePlatform.register()`，**顺序判定，先命中先走**：

| 顺序 | 条件 | 走的 flow | 关键点 |
| --- | --- | --- | --- |
| 1 | `config.executor_type in ("headless","headed")` | `BrowserRegistrationFlow` | `build_browser_registration_adapter()` 为 None 直接 `NotImplementedError` |
| 2 | `identity.identity_provider == "oauth_browser"` | `ProtocolOAuthFlow` | 只支持浏览器执行器 |
| 3 | `identity.identity_provider == "phone"` | `ProtocolPhoneFlow` | 手机号协议注册 |
| 4 | 其余（默认） | `ProtocolMailboxFlow` | **邮箱协议注册，ChatGPT 免费号主力路径** |

四路最终都收敛到：

```python
result = <Flow>(adapter).run(ctx)
return self._attach_identity_metadata(self._account_from_registration_result(result), identity)
```

`_account_from_registration_result`（`base_platform.py:127-177`）：**有 token → `REGISTERED`，没 token → `PENDING_VERIFICATION`**。

### ChatGPT 平台的声明

`platforms/chatgpt/plugin.py:313`：

```python
supported_executors    = ["protocol", "headless", "headed"]
supported_identity_modes = ["mailbox", "phone", "oauth_browser"]
supported_oauth_providers = ["google", "microsoft"]
protocol_captcha_order = ("yescaptcha_api", "twocaptcha_api", "local_solver")
```

## 2. 任务层：`_execute_register_task`

`application/tasks.py:2419`。注册前的准备：

1. **策略强制**（2419+）：`_enforce_sub2_registration_requirements(platform_name, extra)`
   —— 需要 Sub2/Codex 凭据时，会把普通邮箱任务升级成「邮箱 + 手机」流程。
2. **模式判定**：`is_chatgpt_phone_registration`、`is_chatgpt_email_then_phone`
   （`extra["require_phone_verification"]`）。
3. **并发**：`concurrency = min(requested, cap, 20 if 邮箱+手机 else count)`。
   固定 `email` 参数时只允许单账号单并发，否则直接失败。
4. **内联邮箱池**：解析 `chatgpt_api_mailbox_lines`，展开 Gmail 别名，
   剔除已用/重复，得到未使用行。
5. **代理解析**：`_resolve_registration_proxy_decision(...)` → 四层优先级
   （`account > explicit > stable_runtime > pool > legacy_global`）。
   协议模式下还会先跑 `_preflight_chatgpt_registration_proxy` 探一次出口国家，
   写进 `extra["proxy_route_country"]`。
6. **前置检查**：`run_preflight()`（本地依赖/版本/指纹档），默认只记日志。

然后（`tasks.py:3048-3056`）：

```python
platform = _build_platform_instance(platform_name, _build_payload, logger,
                                    resolved_proxy=resolved_proxy,
                                    shared_mailbox=shared_mailbox)
account  = platform.register(email=email, password=password)   # ← 进入第 3 节
```

## 3. 协议邮箱 flow（主力路径）

`build_protocol_mailbox_adapter()`（`plugin.py:814-900`）组装：

- **worker**：`ChatGPTProtocolMailboxWorker`（`platforms/chatgpt/protocol_mailbox.py`），
  注入 `mailbox`、`mailbox_account`、`mail_provider`、`proxy_url`、`proxy_route_country`；
- **runner**：`worker.run(email=ctx.identity.email, password=ctx.password)`；
- **mapper**：`_map_result` → `RegistrationResult`。

### 3.1 `RegistrationEngine.run()` 的 17 步

`platforms/chatgpt/register.py:3240`。每一步失败都带 `error_code` 直接返回：

| 步 | 方法（行号） | 做什么 | 失败码 |
| --- | --- | --- | --- |
| 1 | `_check_ip_location` (768) | 查出口 IP 国家是否被支持 | `unsupported_region` / `proxy_network_error` |
| 2 | `_create_email` (788) | 从邮箱 provider 取一个可用邮箱 | — |
| 3 | `_init_session` (1026) | 建 `curl_cffi` 会话（带代理） | — |
| 4 | `_start_oauth` (824) | 起 OAuth 授权链，拿 authorize URL | `oauth_start_failed` |
| 5 | `_get_device_id` (1060) | 拿 `oai-did` | `oauth_authorize_failed` |
| 6 | `_check_sentinel` (1129) | 取 Sentinel/PoW token | 失败仅告警，不中断 |
| 7 | `_submit_signup_form` (1253) | 提交邮箱；`oauth_invalid_state` 时重建会话重试一次 | `signup_failed` |
| 8 | `_register_password` (1428) | 设密码（若已是 OTP 页或已注册账号则跳过） | — |
| 9 | `_send_verification_code` (1888) | 显式请求下发邮箱 OTP | `otp_delivery_failed` |
| 10 | `_get_verification_code` (1942) | **轮询邮箱取码** | `mailbox_otp_fetch_failed` |
| 11 | `_validate_verification_code` (2037) | 提交验证码 | `otp_validation_failed` |
| 12 | `_advance_external_registration_step` (2173) / `_create_user_account` (2305) | 按 `_otp_page_type` 分流 | `external_auth_step_failed` / `account_creation_failed` |
| 13 | 跟随 callback URL | 跳到 chatgpt.com，拿 `__Secure-next-auth.session-token` | `account_created_session_missing` |
| 14 | `GET {CHATGPT_APP}/api/auth/session` | `_extract_chatgpt_session_credentials` 提取 access_token | `account_created_session_missing` |
| 15-17 | 收尾 | `_extract_chatgpt_account_id(access_token)`、refresh_token、id_token、session_token；`result.success = True` | — |

### 3.2 关键细节

**OTP 取码**（步骤 10）：走 `mailbox.wait_for_code(...)`，基于 `before_ids` 基线快照
——只认新邮件。超时预算 600s（`OtpSpec(timeout=600)`）。
拿到码后可选 `otp_submit_delay` 再等一会儿才提交。

**Sentinel**（步骤 6）：`_SentinelTokenGenerator` 打
`https://sentinel.openai.com/backend-api/sentinel/req`；若响应要求 PoW，
本地做 FNV-1a 32 位 proof-of-work 搜索（最多 50 万 nonce）。

**已注册账号复用**：流程里反复出现 `self._is_existing_account`。
检测到邮箱已注册会自动切成登录流程，跳过设密码/建账户，
最终 `result.source = "login"`（否则 `"register"`）。

**这一步只建基础会话**。`run()` 末尾自己的注释：

> This engine only establishes the base web account/session.
> Task-level phone, Codex credential, liveness, and delivery gates may still follow.

并且明确写着 `Codex CLI/RT/Workspace Join 已从 free 注册主流程移除`。

## 4. 浏览器 flow

`config.executor_type` 为 `headless`/`headed` 时走这条。

### 4.1 浏览器底座选择

`ChatGPTBrowserRegister.run()`（`browser_register.py:5380`）：

```
BitBrowser 配置？ → 直接用它（不试 Chromium）
否则 → 先试系统 Chrome (channel="chrome")，失败退 内置 Chromium
     ↗ 任一步抛异常 → 退化到 Camoufox（带 proxy + geoip=True）
```

- Chrome 路径：`--disable-blink-features=AutomationControlled --disable-dev-shm-usage --no-sandbox`，
  视口 1280x720，随机 Chrome UA，`set_default_timeout(90000)`。
- Camoufox 路径：指纹伪装的 Firefox；开跑前会 `patch_playwright_firefox_pageerror_location_bug()`。
- **手机模式不退化**：`register_mode == "phone"` 时异常直接往上抛。

### 4.2 状态机

`_browser_registration_flow`（`browser_register.py:4889-5062`）是个最多 12 步的循环，
每轮算一个状态签名，**同一签名出现超过 2 次就判死**（`注册状态卡住`）。分支顺序固定：

1. **已完成** → `_handle_post_signup_onboarding()`（授权持久化存储、点 Allow/Continue/Skip）
2. **设密码页**（`create_account_password`）→ `_submit_password_via_page()`；重复进入报错
3. **登录密码页**（`login_password`）→ 按已注册账号处理
4. **邮箱 OTP 页** → `otp_callback()` 阻塞取码 → `_submit_otp_via_page()`；
   若 DOM 提交返回 `status==0`，回退到 API `_validate_browser_email_otp()`
5. **about-you 页** → 随机姓名 + 生日，`_submit_about_you_via_page()`；
   **若落到 add_phone 且没配接码 → 抛 `manual_phone_required`**
6. **add_phone 页** → 同上报错，或走 `_handle_add_phone_challenge()`
7. **跟随 `continue_url`**
8. **其他状态 → 直接报错**

完成后 `_fetch_chatgpt_session_from_page()`（2093）拉 `chatgpt.com/api/auth/session`，
**必须同时拿到 `account_id` 和 `access_token`**，否则：

```
RuntimeError("注册完成但 session 缺少 account_id 或 accessToken，账号未入库")
```

### 4.3 关于 Cloudflare / 打码（重要）

**浏览器注册路径里没有任何 Cloudflare / Turnstile / 打码器。**
唯一相关的是 OpenAI 自己的 Sentinel，而且只在 OTP 校验那一步用到。

- `_ChatGPTPlatform` 的 adapter **没有**把 `use_captcha_for_mailbox` 传 True，
  所以 `artifacts.captcha_solver` 根本不建；
- `payment.py` 里那一大堆 Turnstile / reCAPTCHA / hCaptcha 是 **checkout / 短链** 用的，不是注册用的；
- 防封靠浏览器底座本身（Camoufox 指纹 / 真 Chrome + 关自动化特征）+ 拟人化输入延迟；
- `_goto_with_retry` 只对**瞬时网络错误**重试，Cloudflare 拦截页不在名单里 ——
  真撞上就是 `未找到 OpenAI 注册入口邮箱输入框` 或 `未支持的注册状态`。

> 这正好是上一轮加的 `core/cloudflare_clearance.py` 能补的位：
> 配 `ZCJ_CF_CLEARANCE_URL` 后，`core/http_client.py` 会自动带 `cf_clearance`。
> **但浏览器路径的自研 HTTP 不走 `core/http_client.py`，目前还没接上。**

## 5. 注册成功之后

回到 `tasks.py:3056` 之后，流水线逐段推进：

| 阶段 | 行号 | 做什么 |
| --- | --- | --- |
| `account_created` | 3062 | `platform.register()` 返回即 passed |
| `phone_verified` | 3070-3109 | 仅当 `require_phone_verification`；否则 `not_required` |
| `credentials_ready` | 3115-3160 | `_upgrade_protocol_codex_credentials()` 补 Codex RT；`require_codex_rt` 时拿不到就 `CODEX_RT_MISSING` 拒绝记成功 |
| `liveness` | 3168-3200 | `_post_registration_chatgpt_liveness_error()` 检测账号是否真活着 |
| `persisted` | 3201-3222 | **入库边界** `evaluate_persistence(account)`，然后 `save_account(account)` |
| `probation` | 3233-3260 | 手机号号安排持续存活复检（默认开） |
| `payment` | 3353 | 付款/升级 Plus（免费号流程通常不走） |

### 入库边界（上一轮新增）

`core/registration/persistence.py`，标识 `stable-http-200-at-v1`。判定：
结果非空 + 有身份标识 + 状态属于成功集合 + access_token 长度 ≥ 20。

- **始终**把判定结果写进 `persisted` 阶段的 `detail`（可审计）；
- **仅在** `ZCJ_ENFORCE_PERSISTENCE_BOUNDARY=1` 或 `extra["enforce_persistence_boundary"]`
  时才真的拒绝入库。

默认关闭是为了不误伤那些返回字段不同的平台。

## 6. 失败与重试

- **协议流**：每个步骤返回 `error_code`，`run()` 立即 return 不继续；
  `oauth_invalid_state` 是唯一会自动重建会话重试一次的场景。
- **浏览器流**：异常原样向上抛，`_run_sync_checkout_isolated` 用守护线程包着，
  超时抛 `TimeoutError("chatgpt-browser-register timeout after Ns")`（默认 300s）。
- **任务层**：`_save_task_log(platform, email, "failed", error=...)` 落库，
  失败串再被 `core/registration/attribution.py` 归因成稳定分类，
  由 `GET /stats/attribution` 聚合展示。
- **邮箱被远端拒绝**：`mark_registration_failure()` 会淘汰该子地址并切下一条，
  同时**释放数据库层的原子预占**。

## 7. 两条路的产物差异

| 字段 | 协议邮箱 | 浏览器 |
| --- | --- | --- |
| `access_token` / `refresh_token` / `id_token` | ✅ | ✅ |
| `oauth_credential_type`（`codex_oauth` / `chatgpt_web`） | ✅ | ❌ |
| `web_access_token` | ✅ | ❌ |
| `auth_cookies`（JSON 列表） | ✅ | ❌（只有 Cookie 头字符串） |
| `oai_device_id` | ✅ | ❌ |
| `registration_state`（最终页面状态） | ❌ | ✅ |
| `phone_number` / `register_mode` | ❌ | ✅ |
| `workspace_join` 结果 | ❌ | ✅ |

**浏览器独有**：能在同一浏览器里继续跑 post_register 回调
（Workspace Join、短链复用的 PayPal checkout）——协议流根本开不了浏览器，触发不了。

## 8. 想调这个流程时该看哪儿

```
派发规则            core/base_platform.py:179-228
协议注册 17 步      platforms/chatgpt/register.py:3240-3776
协议 adapter        platforms/chatgpt/plugin.py:814-900
浏览器状态机        platforms/chatgpt/browser_register.py:4889-5062
浏览器 adapter      platforms/chatgpt/plugin.py:729-802
手机协议            platforms/chatgpt/protocol_phone.py
OAuth 浏览器        platforms/chatgpt/browser_oauth.py
任务编排            application/tasks.py:2419 / 3056 / 3110-3260
入库边界            core/registration/persistence.py
失败归因            core/registration/attribution.py
```