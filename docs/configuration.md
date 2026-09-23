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

任务级覆盖：`extra["enforce_preflight"]`。

## 7. 应用与门户

| 变量 | 说明 |
| --- | --- |
| `APP_PASSWORD` | 主应用密码；未设置时启动即失败（fail-fast） |
| `ZCJ_PORTAL_JWT_SECRET` | 门户 JWT 密钥 |
| `ZCJ_PORTAL_ADMIN_PASSWORD` | 门户管理员密码（首次启动引导用） |
| `ZCJ_PORTAL_PAYMENT_HMAC_SECRET` | 支付回调 HMAC 密钥 |
| `ZCJ_CORS_ORIGINS` | 逗号分隔；为空时不启用 CORS |

`docker-compose` 中 `APP_PASSWORD` 使用 `${ZCJ_APP_PASSWORD:?...}`，缺失即拒绝启动。