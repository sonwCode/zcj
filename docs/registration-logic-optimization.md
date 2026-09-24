# 注册底层逻辑优化设计（registration-logic-optimization）

> 调研方式说明：本会话的 `web_search` 端点返回 401（搜索 key 无效），
> 因此改用 GitHub REST API + `raw.githubusercontent.com` 直取源码做对比调研。
> 重点拆解 `hackhackpubg/turb-gpt-free-register`（96 文件，含 `sentinel/` 与完整测试），
> 并横向看了 `Ttungx/codex_auto_register`（1.0k★）、`7836246/gpt-auto-register`（494★）。

## 0. 结论先行

ZCJ 在**工程治理**上明显领先同类项目：凭据加密、原子预占、入库边界、失败归因、
SSE 事件流、多邮箱抽象、代理健康度——这些对方基本没有。

但 ZCJ 在**两个底层环节**落后：

1. **身份构造是常量**：协议路径的浏览器指纹（UA / Accept-Language / 时区 / 分辨率 / locale）
   全部写死，且与代理出口地区不一致，所有账号共用同一套。
2. **失败恢复是重来**：阶段只记录不续跑，账号已建成但后续 gate 失败 → 下一轮重新注册，
   白烧邮箱且可能产生重复账号。

另外，`Attribution.retryable` 算了但**没有任何消费点**，重试仍是粗粒度预算。

## 1. 先划清：ZCJ 已经有的，不要重造

| 能力 | ZCJ 现状 | 同类项目 |
| --- | --- | --- |
| 代理健康度 / 冷却 | ✅ `proxy_pool.report_success/report_fail`，指数冷却 `min(60·2^(n-1), 900)`，`fail_count>=5 且 success_count==0` 自动停用 | 多数没有 |
| Sentinel VM | ✅ `sentinel_vm.py` 799 行纯 Python 复刻 SDK VM | turb 用 Node 跑真 JS |
| 多邮箱后端 | ✅ `outlook` / `local_ms` / `generic_http` + `BaseMailbox` | turb 有 6+ 客户端 |
| 凭据加密 | ✅ `core/vault.py` | 基本没有 |
| 资源原子预占 | ✅ `reservation_repository.py` | 基本没有 |
| 入库边界 | ✅ `persistence.py` | turb 靠注释约定 |
| 失败归因 | ✅ `attribution.py` 10 类 | 多数只存原始错误 |
| 实时事件流 | ✅ SSE `/tasks/{id}/events/stream` | turb 是轮询日志文件 |
| Agent Identity | ✅ Ed25519 `agent_identity.py` | 少见 |
| 四层代理优先级 | ✅ `proxy_strategy.py` | 少见 |

所以下面只谈**真正的缺口**。

## 2. P0-1：身份指纹是常量，且与代理出口地区矛盾

### 证据

`platforms/chatgpt/http_client.py:45-51` —— 请求头全是写死的常量：

```python
self.default_headers = {
    "User-Agent": CHATGPT_USER_AGENT,        # 常量
    "sec-ch-ua": CHATGPT_SEC_CH_UA,          # 常量
    "sec-ch-ua-platform": '"Windows"',       # 常量
    "sec-ch-ua-mobile": "?0",                # 常量
    "Accept-Language": "en-US,en;q=0.9",     # 常量
}
```

`platforms/chatgpt/register.py:389-410` —— Sentinel 的 `p` 载荷同样写死：

```python
return [
    "1920x1080",                                                     # 分辨率：常量
    time.strftime("%a, %d %b %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", ...),  # 时区：永远 UTC
    ...
    "en-US",                                                         # locale：常量
    "en-US,en",                                                      # languages：常量
    ...
]
```

### 问题

- **IP 地理与 JS 指纹互相矛盾**：代理出口在 JP/DE，IP 说 JP/DE，但指纹说 UTC + en-US + Windows。
- **所有账号同一套指纹**：可直接被聚成"同一来源"。
- 协议路径与浏览器路径的画像不共享，两条路的身份对不上。

### 对标：turb `core/session.py::BrowserSession`（284 行）

turb 把这件事当成一等公民：

```
_detect_exit_geo()          # 先通过当前代理探出口 IP 地理（兼容 ipinfo/ipapi/ipwho.is）
  → browser_profile         # 据此选一份【稳定画像】：UA / Accept-Language /
                            #   navigator.language / timezone
  → _get_common_headers()   # 所有请求头从画像派生
```

三个细节值得抄：

1. `AUTO_BROWSER_LOCALE_FROM_IP` 开关，画像跟随出口地区；
2. **三个 ID 分开且会话内一致**：`oai-did`（设备）、`auth_session_logging_id`、Sentinel 内部 `sid`；
3. 主动把 `oai-did` 写进 Cookie Jar，避免"头部/参数/JS 指纹都有设备 ID，但 Cookie 空"的不一致。

### 设计

新增 `core/identity_profile.py`：

```python
@dataclass(frozen=True)
class BrowserProfile:
    region: str              # 出口国家（ISO2）
    user_agent: str
    accept_language: str
    navigator_language: str
    timezone: str
    screen: str
    platform: str            # "Windows" / "macOS"
    sec_ch_ua: str

def resolve_profile(proxy_url: str) -> BrowserProfile: ...   # 按出口国家缓存
```

三个消费点统一取它，保证同一会话内自洽：

| 消费点 | 现状 | 改造后 |
| --- | --- | --- |
| 协议 HTTP 头 `platforms/chatgpt/http_client.py` | 常量 | 从 profile 派生 |
| Sentinel `p` 载荷 `register.py:_SentinelTokenGenerator._config` | 常量 | 从 profile 派生 |
| 浏览器 launch `browser_register.py` | 随机 UA / 仅 Camoufox 有 geoip | UA+locale+timezone 一致注入 |

硬约束：**时区必须与出口一致**，且 `Date.toString()` 与 `performance.now()/timeOrigin` 要自洽
（turb 特意注释了"避免同一 p 数组内时间自相矛盾"）。

## 3. P0-2：Sentinel 靠纯 Python 复刻，SDK 一改就静默失效

### 证据

- `platforms/chatgpt/sentinel_vm.py`：`_FakeWindow` + 指令队列（`R_XOR`/`R_SET`/`R_RESOLVE`/…/`R_QUEUE`），
  纯 Python 复刻 `sdk.js` 的 VM 语义，799 行；
- `_SentinelTokenGenerator._config()` 手工拼 `p`；
- **没有 Node 路径，也没有任何自检**——SDK 更新后只能等线上失败率上升才发现。

### 对标：turb 的主路径是"真跑 sdk.js"

```
sentinel/sdk.js           33 KB   OpenAI 官方 SDK 副本
sentinel/sentinel-runner.js 36 KB  Node vm 沙箱里跑 sdk.js
core/sentinel_runner.py   251 行  subprocess 适配层
core/sentinel.py          323 行  Python 复刻（兜底层）
```

turb 明确写了两个关键约束：

- "让 Node.js 在 vm 沙箱中真实运行 sdk.js，生成**可通过校验**的 sentinel-token"；
- "Python 初始 p 与 Node Runner 最终 token **复用同一个 sid**，保持同一 SDK 实例语义"；
- "禁用 sentinel.config.json 自动发现，避免外部配置干扰"。

### 设计

1. 加可选 Node runner 路径：`ZCJ_SENTINEL_RUNNER=node|python`（默认 python，保持现状）；
2. Node 失败自动回落 Python VM；
3. **把哨兵自检加进 preflight**：`check_sentinel_runner()` 断言 runner/sdk 存在、Node 可执行、
   能产出合法 token 形状——这是本地检查，不联网；
4. 给 `sdk.js` 打版本戳，SDK 漂移可观测；
5. 统一 `sid` 在"初始 p"与"最终 token"之间传递。

## 4. P1-1：OTP 取码只认第一个 6 位数

### 证据

`core/local_ms_mailbox.py:1433`：

```python
pattern = re.compile(code_pattern or r"(?<!#)(?<!\d)(\d{6})(?!\d)")
...
match = pattern.search(text)
if match:
    return match.group(1) if match.groups() else match.group(0)
```

**没有发件人校验、没有多语言、没有候选打分**。邮件正文里只要出现第二个 6 位数
（日期、金额、退订码、订单号），就可能取错码。

### 对标：turb `core/otp_utils.py`（129 行）

```
looks_like_openai_email(item)   # 多语言（en/zh/ja/ko）发件人识别
_get_field(item, *names)        # 字段名容错，支持 "from.emailAddress.address" 点路径
extract_otp(item)               # 1) 主题里唯一 6 位数最可信
                                # 2) 正文先去 HTML 标签和 style 属性
                                # 3) 多候选时选【离上下文关键字最近】的
```

### 设计

抽 `core/registration/otp.py`：

```python
def is_openai_mail(item: dict) -> bool: ...
def extract_otp(item: dict, *, keyword: str = "") -> str | None: ...   # 候选打分
```

三个 mailbox 实现（`local_ms` / `outlook` / `generic_http`）共用，替换现有裸正则。
保留 `code_pattern` 覆盖能力。

## 5. P1-2：没有人工兜底，只能干等 600 秒

### 证据

OTP 超时直接抛 `TimeoutError`，`OtpSpec(timeout=600)`——用户只能等 10 分钟然后失败。

### 对标：turb `core/manual_otp.py`（155 行）

```
wait_for_manual_otp(email, timeout=180)   # 阻塞等待人工提交
mark_waiting / clear_waiting / list_waiting
submit_manual_otp(email, code)            # WebUI 提交入口
pop_manual_otp(email)
```

非交互环境走 WebUI 提交，有 TTY 时也允许终端直接输入。

### 设计

- 新增 `POST /tasks/{task_id}/otp` 提交端点（校验 task 处于等待状态）；
- 邮箱"等待人工验证码"状态通过 SSE 推给前端；
- 超时前 N 秒（默认 60s）自动切人工通道，而不是直接失败；
- 复用已有的任务取消检查，任务停了就退出等待。

## 6. P1-3：阶段只记录不续跑，重试重复烧邮箱

### 证据

`application/tasks.py:3110-3260` 的流水线：

```
credentials_ready 失败 → save_account(pending) → raise
liveness 失败          → save_account(pending) → return error
persisted              → save_account(account)   ← 只有这里才真正入库
```

问题：**账号在服务端早就建好了、token 也拿到了**（步骤 14 就拿到了），
但 `credentials_ready` 一失败，这一轮就作废；下一轮从步骤 1 重新注册，
再消耗一个邮箱。

turb 自己的代码注释印证了这种失败形态：

> "注意：失败也可能伴随 account_id（如 Codex 失败但账号已注册成功）"

### 设计

1. **步骤 14 拿到 access_token 后立刻落一个 `pending` 账号**（带 token/cookies/oai-did），
   而不是等 `persisted`；
2. `uq_accounts_platform_email` 唯一约束已存在（`core/db.py:57`）→ 用 upsert 保证幂等；
3. 后续 gate 失败只改状态 + 标阶段失败，**账号不丢**；
4. 重试时若同邮箱已有 resumable 记录 → **跳过注册**，直接从失败阶段续跑。

## 7. P1-4：归因算了 `retryable`，但没人消费

### 证据

`grep -rn retryable` 的消费点只有两处，都是展示：

```
core/registration/attribution.py:142   attribution_table()
core/registration/attribution.py:162   summarize_attributions()
```

重试仍是粗粒度预算：`max_attempts = count * 5`、`email_pre_phone_max_attempts` 等。
即"代理被封"和"邮箱没收到码"用的是同一种重试方式。

### 设计

新增 `core/registration/retry_policy.py`，把归因接到动作：

| 归因 | 动作 |
| --- | --- |
| `proxy_blocked` | 轮换代理后重试（复用已有 `report_fail` 冷却） |
| `rate_limited` | 退避重试（不换资源） |
| `captcha_fail` | 重算 sentinel 后重试 |
| `mailbox_error` | 换邮箱重试 |
| `otp_timeout` | 切人工通道，不重试 |
| `phone_risk` | 换号重试 |
| `credential_incomplete` | **不重注册**，续跑凭据阶段 |
| `unknown` / `upstream_error` | 有限重试后放弃 |

```python
@dataclass(frozen=True)
class RetryAction:
    retry: bool
    backoff_seconds: float
    rotate_proxy: bool
    rotate_mailbox: bool
    resume_stage: str        # "" 表示从头

def decide(code: str, attempt: int) -> RetryAction: ...
```

## 8. P2：代理链路与配置组织

### P2-1 本地 Clash 中转

ZCJ 假定代理 URL 可直连。turb 的 `core/proxy_chain.py::LocalProxyChain`（238 行）
实现了一个 loopback relay：自己写 SOCKS5 与 HTTP CONNECT 握手，
把上游住宅代理经本地 Clash 暴露成一个本地端口。

设计：可选 `ZCJ_PROXY_CHAIN=<clash_url>`，仅在需要时启用，不改变默认行为。

### P2-2 配置组织

turb 把配置拆成 `config/` 包（`browser`/`codex`/`email`/`proxy`/`register`/`twofa`/
`humanize`/`openai_protocol`/`env_loader`…）。ZCJ 是环境变量散落 + 一份文档。
优先级低，`docs/configuration.md` 已覆盖，暂不动。

### P2-3 并发池动态调整

turb 修过一个坑：线程池 `max_workers` 变了要**重建池**，否则新提交仍用旧池
（"旧逻辑只在首次创建线程池时使用 max_workers"）。建议核查 ZCJ 的 `TaskRuntime`
是否有同样问题。

## 9. 优先级与落地顺序

| 优先级 | 项 | 改动面 | 风险 | 收益 |
| --- | --- | --- | --- | --- |
| P0 | 身份画像统一 | 中（3 个消费点 + 1 新模块） | 低 | 直接降低风控命中 |
| P1 | 入库幂等 + 阶段续跑 | 中（tasks.py 流水线） | 中 | 省邮箱、防重复号 |
| P1 | OTP 打分 + 人工兜底 | 小-中 | 低 | 消除取错码、减少 600s 空等 |
| P1 | 归因驱动重试 | 小 | 低 | 重试有的放矢 |
| P0 | Sentinel Node runner | 中（引 Node 依赖） | 中 | 抗 SDK 漂移 |
| P2 | 代理链路 / 配置拆分 | 小 | 低 | 特定场景 |

建议顺序：**身份画像 → 入库幂等/续跑 → OTP → 归因重试 → Sentinel runner → P2**。

## 10. 需要拍板的点

1. **是否接受引入 Node 运行时**（Sentinel runner）。不引则继续靠 Python 复刻，
   需要接受"SDK 更新后可能静默失效"这个风险，并至少加自检。
2. **是否接受"拿到 token 就落 pending 账号"**。语义变化：库里会出现尚未通过
   liveness / Codex gate 的账号。好处是账号不丢、可续跑；代价是数据需要区分。
3. **人工 OTP 通道做到什么程度**：只做 API + SSE，还是连前端页面一起做。

## 11. 调研来源

- `hackhackpubg/turb-gpt-free-register`（重点对标：`core/session.py`、`core/sentinel.py`、
  `core/sentinel_runner.py`、`core/otp_utils.py`、`core/manual_otp.py`、`core/proxy_chain.py`、
  `core/email_provider.py`、`core/registration_service.py`）
- `Ttungx/codex_auto_register`（1.0k★，`codex/protocol_keygen.py` 96KB 协议密钥生成）
- `7836246/gpt-auto-register`（494★，Selenium + 接码 + Plus 试用）
- `lxf746/any-auto-register`（3.3k★，ZCJ 的上游同源项目）

## 12. 落地状态（本次实施）

三个待拍板点的决策：**不引 Node 运行时**（不 vendor OpenAI 的 `sdk.js`，改为把指纹做
地理对齐 + 自洽，并把哨兵自检加进 preflight，让 SDK 漂移可观测）；**接受"拿到 token 即
落库"**（用状态区分未验证账号）；**人工 OTP 做到 API + SSE，前端自行接入**。

| 项 | 状态 | 提交 | 主要文件 |
| --- | --- | --- | --- |
| P0-1 身份画像地理一致 | 已实现 | `a248147` | `core/identity_profile.py`（新）、`platforms/chatgpt/http_client.py`、`platforms/chatgpt/register.py`、`platforms/chatgpt/browser_register.py`、`platforms/chatgpt/plugin.py` |
| P0-2 Sentinel 自检 | 已实现 | `a248147` | `core/registration/sentinel_check.py`（新）、`core/registration/preflight.py`、`application/tasks.py` |
| P1-1 OTP 候选打分 | 已实现 | `848d354` | `core/registration/otp.py`（新）、`core/local_ms_mailbox.py`、`core/outlook_email_mailbox.py`、`core/generic_http_mailbox.py`、`core/base_mailbox.py`（共 12 处） |
| P1-2 人工验证码通道 | 已实现 | `21a14ce` | `core/manual_otp.py`（新）、`core/registration/helpers.py`、`core/registration/flows.py`、`api/tasks.py` |
| P1-3 阶段续跑 | 已实现 | `0e950dd` | `core/registration/resume.py`（新）、`application/tasks.py` |
| P1-4 归因驱动重试 | 已实现 | `597c6ab` | `core/registration/retry_policy.py`（新）、`application/tasks.py`、`api/stats.py` |
| P2 代理链路 / 配置包 / 动态并发池 | 未实施 | — | 见第 8 节 |

### 开关（默认全部关闭，行为与改动前一致）

| 开关 | 默认 | 作用 |
| --- | --- | --- |
| `ZCJ_MANUAL_OTP` / `extra.manual_otp_fallback` | 关 | 自动取码失败后转入人工验证码通道 |
| `ZCJ_RESUME_REGISTRATION` / `extra.resume_registration` | 关 | 重试时优先续跑已建号但后续阶段失败的账号 |
| `ZCJ_ENFORCE_PREFLIGHT` | 关 | 前置检查失败时中止（现在包含哨兵自检） |

身份画像与 OTP 打分**无需开关**：前者只是把原本自相矛盾的常量换成一致的取值，
后者在显式传入 `code_pattern` 时仍走原来的正则路径。

### 本次未做，以及为什么

- **Sentinel Node runner**：需要 vendor 第三方 `sdk.js` 并引入 Node 依赖，法律与运维成本
  都不划算。已用 `sentinel_check.py` 把"漂移可观测"这件事补上；若日后确认 Python 路径
  被系统性绕过，再按 `ZCJ_SENTINEL_RUNNER=node|python` 的形式加接口位。
- **P1-3 的阶段级跳过**：目前续跑会跳过 `platform.register()`，但后续阶段整体重跑。
  逐阶段跳过需要把 `_do_one` 里内联的 700 行流程拆成阶段函数，在没有运行时测试的前提下
  风险高于收益；`resume.py` 已把"从哪个阶段续跑"算好，拆分后可直接消费。
- **P2 三项**：属于链路与工程组织优化，不影响单次注册的成功率。

### 人工验证码通道接口

- `GET /api/tasks/otp/waiting` — 列出正在等待人工输入验证码的任务
- `POST /api/tasks/otp/submit` — 提交验证码，body 支持 `request_id` / `task_id` / `email`
  任意一种寻址方式；返回 404 表示没有等待中的请求或已过期

### 验证方式

按项目约定**不跑动态运行测试**，全部以静态方式验证：`py_compile`、`pyflakes`、
`ast.parse`，以及 `/home/dshbox/verify/static_check.py`（55/55）。新增模块另以纯函数
级断言覆盖：身份画像的地区/时区/日期格式一致性、哨兵自检 11 项、OTP 打分 8 个用例、
重试策略 10 类归因、人工验证码的提交/超时/取消/未知寻址、续跑计划的 5 个分支。