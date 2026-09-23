# 遥测与运行时（telemetry-and-runtime）

## 1. 任务运行时

任务是数据库持久化的实体，由 `application/tasks.py` 中的 `TaskRuntime` 调度。
按用途划分车道（lane），互不阻塞：

```
TASK_LANE_MAIN / REGISTER / ACCOUNT_CHECK / ACCOUNT_ACTION / PLATFORM_ACTION / PAYMENT
```

任务事件写入 `task_events` 表（`TaskEventModel`：`id`、`task_id`、`type`、`level`、
`message`、`detail_json`、`created_at`）。`id` 自增，可直接作为事件游标。

## 2. 任务日志与实时事件

| 端点 | 说明 |
| --- | --- |
| `GET /tasks/logs` | 分页查询注册日志 |
| `GET /tasks/{task_id}/events` | 轮询增量事件（`after_id` 游标） |
| `GET /tasks/{task_id}/events/stream` | SSE 实时事件流 |

SSE 流的行为：

- 先回放 `after_id` 之后的历史事件，再持续尾随新事件；
- 每条消息带 `id:`（游标）与 `event:`（事件类型），客户端断线可用 `after_id` 续传；
- 空闲超过 15 秒发送 `: ping` 心跳，防止中间代理断开；
- 任务进入终态（`succeeded`/`failed`/`cancelled`）且无新事件时发送 `event: done` 并结束；
- 最长 300 秒，超时发送 `event: timeout`，客户端可重连。

实现是**同步生成器**：FastAPI 会把它放到工作线程执行，短暂 `sleep` 不阻塞事件循环，
也不需要新增依赖。

## 3. 失败归因看板

| 端点 | 说明 |
| --- | --- |
| `GET /stats` | 成功率概览 |
| `GET /stats/errors` | 原始错误串分组 |
| `GET /stats/attribution?days=7&platform=` | 归因分类 + 占比 |

归因把“代理被风控 / 邮箱异常 / 二次验证 / 限流 / 上游 5xx”等区分开，
避免只看一个笼统的失败总数。每个分类返回 `count`、`share` 与最多 3 条样本。

## 4. 运行诊断

`GET /stats/diagnostics` 返回只读快照：

```json
{
  "vault": { "enabled": true, "backend": "keyfile" },
  "cloudflare_clearance": { "enabled": false, "provider": "none" },
  "persistence_boundary": { "boundary": "stable-http-200-at-v1" },
  "preflight": { "ok": true, "checks": [] }
}
```

该端点不访问网络，可在生产随时调用。

## 5. 离线库存诊断

`scripts/registration_inventory.py` 是纯只读脚本：**不联网、不注册、不清理**。

```bash
python scripts/registration_inventory.py
python scripts/registration_inventory.py --days 14 --limit 50
python scripts/registration_inventory.py --json
```

输出：存储后端、凭据加密状态、各表计数与平台分布、注册流水线阶段状态、
失败归因分桶、资源预占快照、CF 清关状态、前置检查结果。

## 6. 日志分类

`core/registration_logging.py::classify_registration_log()` 给每条日志标注
`log_view`（`summary` / `diagnostic`）与 `log_stage`
（`result` / `postprocess` / `verification` / `account` / `authorization` /
`identity` / `setup` / `flow`），前端可据此折叠噪声、突出结论。