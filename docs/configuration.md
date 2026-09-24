# 配置（configuration）

所有配置都通过环境变量提供，默认值保证“开箱即用且行为不变”。
新增开关一律**默认关闭**，需要显式打开才会改变行为。

## 1. 数据库 / 存储后端

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ACCOUNT_MANAGER_DATABASE_URL` | `sqlite:///<项目根>/account_manager.db` | 主库连接串 |
| `PORTAL_DATABASE_URL` | 门户自带默认 | 客户门户独立库 |

后端选择由 `core/storage.py` 统一判定：

```
# 默认（单机）
(不设置)

# PostgreSQL（多节点）
ACCOUNT_MANAGER_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/zcj
```

- SQLite：`WAL` + `synchronous=NORMAL` + `busy_timeout=10000`，并执行历史表结构迁移；
- PostgreSQL：使用连接池（`pool_pre_ping` / `pool_size=10` / `pool_recycle=1800`），
  **跳过** SQLite 专用迁移；其余建表与补列使用可移植 DDL。
- 需要安装 `psycopg` 才能连接 PostgreSQL。

## 2. 凭据加密（Vault）

`core/vault.py` 用 PyNaCl `SecretBox` 对敏感列做透明加解密，密文前缀 `enc:v1:`。

| 变量 | 说明 |
| --- | --- |
| `ZCJ_VAULT_KEY` | 直接提供 32 字节密钥（base64 或 hex） |
| `ZCJ_VAULT_KEY_FILE` | 从文件读取密钥 |
| `ZCJ_VAULT_DISABLED` | 设为 `1` 时退回明文（仅用于迁移/排障） |

未提供密钥时会在数据库同目录生成 `.zcj_vault_key`（权限 `0600`）。

覆盖的列：账号密码、账号凭据、供应商账号/设置/资源标识、代理 URL。
代理 URL 需要按等值查询，因此另存 `url_index` 盲索引（HMAC），查询走索引。

兼容性：没有 `enc:v1:` 前缀的历史明文原样返回，无需一次性迁移；
`_backfill_proxy_url_index()` 会为新旧代理行补齐盲索引。

## 3. 代理分层

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ZCJ_PROXY_TIER_ORDER` | `account,explicit,stable_runtime,pool,legacy_global` | 代理层优先级 |
| `ZCJ_GLOBAL_PROXY` | 空 | 旧版全局代理出口 |

配置项 `proxy_global_url` 与 `ZCJ_GLOBAL_PROXY` 等价。
账号自身代理来自任务 `extra["account_proxy"]`。

## 4. Cloudflare 清关

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ZCJ_CF_CLEARANCE_URL` | 空（禁用） | FlareSolverr 兼容端点 |
| `ZCJ_CF_CLEARANCE_TTL` | `600` | `cf_clearance` 缓存秒数 |

配置项 `cf_clearance_url` / `cf_clearance_ttl` 与之等价。
未配置端点时 `apply_clearance()` 是空操作，`core/http_client.py` 不产生任何额外请求。

## 5. 入库边界

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ZCJ_ENFORCE_PERSISTENCE_BOUNDARY` | 关闭 | 开启后不满足边界即拒绝入库 |

任务级覆盖：`extra["enforce_persistence_boundary"]`。

## 6. 前置检查

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ZCJ_ENFORCE_PREFLIGHT` | 关闭 | 开启后本地依赖检查失败即终止任务 |
| `ZCJ_PREFLIGHT` | 开启 | 容器启动前跑一次 `scripts/cloud_preflight.py`（不阻塞启动） |

任务级覆盖：`extra["enforce_preflight"]`。

`run_preflight()` 的 `sentinel` 项默认关闭，ChatGPT 注册任务会显式打开；
`/api/diagnostics` 同样带上它——否则哨兵失效时该端点仍会报“前置检查通过”。

## 7. 应用与门户

| 变量 | 说明 |
| --- | --- |
| `APP_PASSWORD` | 主应用密码；未设置时启动即失败（fail-fast） |
| `APP_CORS_ORIGINS` | 逗号分隔；为空时不启用 CORS（仅前后端分离调试时需要） |
| `APP_GIT_SHA` / `APP_BUILD_TIME` | 可选，写入构建信息接口 |
| `ZCJ_ALLOW_INSECURE` | 设为 `1` 允许无 `APP_PASSWORD` 启动（仅限内网调试） |
| `ZCJ_MEMORY_LIMIT` | compose 内存上限，默认 `4g` |
| `ZCJ_XVFB_SCREEN` | 虚拟屏分辨率，默认 `1920x1080x24` |
| `VNC_ENABLED` | 设为 `1` 打开 x11vnc（默认关） |
| `VNC_PASSWORD` | VNC 密码；启用 VNC 但不设则启动失败 |
| `VNC_BIND` | websockify 绑定地址，默认 `127.0.0.1` |
| `ZCJ_ALLOW_INSECURE_VNC` | 设为 `1` 允许无密码 VNC（仅限本机调试） |
| `UVICORN_WORKERS` | 必须为 `1`；多 worker 会重复派发任务，扩容请加容器 |

`docker-compose` 中 `APP_PASSWORD` 使用 `${ZCJ_APP_PASSWORD:?...}`，缺失即拒绝启动。

## 8. 注册行为

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ZCJ_CHATGPT_IMPERSONATE` | `chrome142` | 指定 curl_cffi 指纹目标，`auto` 按权重随机 |
| `ZCJ_CHATGPT_BROWSER_FAMILY` | 空（Chrome） | 限定浏览器家族：`chrome`/`safari`/`firefox`/`safari_ios` |
| `ZCJ_MANUAL_OTP` | 关闭 | 邮箱收不到码时允许人工从 API 注入验证码 |
| `ZCJ_RESUME_REGISTRATION` | 关闭 | 续跑已创建但未完成注册的账号，而不是重新注册 |

身份画像（`core/identity_profile.py`）默认开启：地区、时区、语言、UA、client hints
与屏幕参数互相一致，避免"美国代理 + 上海时区"这类自相矛盾的载荷。
指纹值全部来自本机 curl_cffi 实测，`scripts/probe_curl_cffi_headers.py` 可复现比对。

## 9. 任务事件表

`task_events` 是注册日志的落库表。一次注册上百条，因此既要有保留策略，也要合批写。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ZCJ_TASK_EVENT_RETENTION_DAYS` | `14` | 超过天数的历史事件被清理；`0` 关闭按时间清理 |
| `ZCJ_TASK_EVENT_MAX_ROWS` | `200000` | 行数上限，超出按最旧优先删除；`0` 关闭 |
| `ZCJ_RETENTION_VACUUM` | 关闭 | 清理后执行 `VACUUM` 回收磁盘（需要额外时间和空间） |
| `ZCJ_TASK_EVENT_BUFFER` | 开启 | 合批写入；`0` 回到逐条 commit |
| `ZCJ_TASK_EVENT_FLUSH_SECONDS` | `0.5` | 后台落盘间隔 |
| `ZCJ_TASK_EVENT_BUFFER_SIZE` | `500` | 缓冲区达到该行数立即落盘 |

清理由调度器每轮调用 `run_retention_cycle()`；正在运行的任务的事件永不删除。
合批写入的正确性依赖**读前先 flush**：轮询接口与 SSE 流都会先落盘再查询，
因此客户端不会看到滞后的日志。实测 8 线程下合批比逐条快约 15 倍。

## 10. 客户门户

门户是独立子应用（`customer_portal_api/`），**使用 `PORTAL_` 前缀**，
不是 `ZCJ_`。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORTAL_DATABASE_URL` | 门户自带默认 | 门户独立库 |
| `PORTAL_JWT_SECRET` | 自动生成并持久化 | 未配置时打印告警 |
| `PORTAL_ADMIN_PASSWORD` | 无 | 首次启动引导管理员 |
| `PORTAL_ADMIN_USERNAME` | `admin` | 引导管理员用户名 |
| `PORTAL_ADMIN_EMAIL` | `admin@example.com` | 引导管理员邮箱 |
| `PORTAL_APP_NAME` | `Customer Portal API` | 门户名称 |
| `PORTAL_ACCESS_TOKEN_TTL_SECONDS` | `7200` | 访问令牌有效期 |
| `PORTAL_REFRESH_TOKEN_TTL_SECONDS` | `2592000`（30 天） | 刷新令牌有效期 |
| `PORTAL_CORS_ORIGINS` | 空 | 门户 CORS 白名单 |
| `PORTAL_PAYMENT_SECRET_<CHANNEL>` | 无 | 按渠道的支付回调密钥，如 `PORTAL_PAYMENT_SECRET_TESTCHAN` |

