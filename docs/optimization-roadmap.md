# 优化落地说明（optimization-roadmap）

本轮优化参考了五个开源注册机项目，逐项判断“值得吸收 / 值得拒绝”，
所有改动默认不改变既有行为，通过环境变量显式开启。

## 1. chatgpt2api（约 6.4k star）

| 参考点 | 落地情况 |
| --- | --- |
| 可插拔存储后端 | ✅ `core/storage.py` 抽象方言与连接池；SQLite 专用迁移按方言隔离 |
| 四层代理优先级 | ✅ `core/proxy_strategy.py`，默认顺序向后兼容，严格顺序可选 |
| Cloudflare 清关层 | ✅ `core/cloudflare_clearance.py`（FlareSolverr 兼容）+ `http_client` 接入 |

未照搬的部分：不引入 WARP/Privoxy 强制链路。清关层是**可选端点**，
未配置时零开销；强制链路会改变所有出网流量，风险大于收益。

## 2. gptfree-register（约 124 star）

| 参考点 | 落地情况 |
| --- | --- |
| 凭据加密 Vault | ✅ `core/vault.py`，透明列加密 + 代理 URL 盲索引 |
| 原子预占 | ✅ `infrastructure/reservation_repository.py` + 邮箱池接入 |
| Agent Identity（Ed25519） | ✅ 已存在（`platforms/chatgpt/agent_identity.py`），本轮确认并记录 |

## 3. GPT-Register-Tool（约 490 star）

| 参考点 | 落地情况 |
| --- | --- |
| 显式入库边界 | ✅ `core/registration/persistence.py`，默认只记录、可选强制 |
| 架构/配置/遥测文档 | ✅ `docs/registration-architecture.md`、`configuration.md`、`telemetry-and-runtime.md` |
| 离线只读清点脚本 | ✅ `scripts/registration_inventory.py` |
| 前置版本检查 | ✅ `core/registration/preflight.py`，默认只记录、可选强制 |

## 4. 上游 any-auto-register

| 参考点 | 落地情况 |
| --- | --- |
| 成功率看板 + 错误归因 | ✅ `/stats`、`/stats/errors`、新增 `/stats/attribution` |
| SSE 实时步骤日志 | ✅ `/tasks/{task_id}/events/stream` |
| 多邮箱通道 / 多执行模式 / 多接码 / 多打码 | 已具备，未重复建设 |

## 5. openai-cpa（约 1.4k star）——反例

该项目把鉴权绑定到 Telegram 群组：非群成员一律 HTTP 403，且进程会“自动关停”。
这是**反面模式**，本轮**明确不采纳**：

- ZCJ 的 `customer_portal_api` 使用自带 JWT 鉴权，不依赖任何外部聊天群组；
- 不会因为外部服务不可用而自动关停主进程；
- 该边界已写入 `SECURITY.md`，后续评审不得引入外部群组作为鉴权依赖。

## 6. 仍未处理（后续工作）

- `platforms/chatgpt/*` 业务逻辑体量很大（`payment.py` 9k+ 行、`browser_register.py` 5.5k+ 行），
  尚未逐行审计；
- `application/tasks.py` 约 4.9k 行，仍可继续按职责拆分；
- 前端组件级交互缺陷未系统排查；
- SMS 池尚未接入原子预占（当前仅有黑名单与前端文本池）。