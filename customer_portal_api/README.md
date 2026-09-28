# Customer Portal API

独立的 C 端/管理端后端服务，复用当前仓库已有的平台注册内核、平台元数据、任务运行时、账号资产、代理和系统能力。

## 已实现能力

- 认证接口：登录、刷新 token、登出、当前用户
- 用户端接口：
  - `GET /api/app/platforms`
  - `GET /api/app/config/options`
  - `GET /api/app/products`
  - `POST /api/app/tasks/register`
  - `GET /api/app/tasks`
  - `GET /api/app/tasks/{task_id}`
  - `GET /api/app/tasks/{task_id}/events`
  - `GET /api/app/tasks/{task_id}/logs/stream`
  - `GET /api/app/orders`
  - `POST /api/app/orders`
  - `GET /api/app/orders/{order_no}`
  - `POST /api/app/payments/{order_no}/submit`
  - `GET /api/app/subscriptions`
  - `GET /api/app/profile`
  - `PATCH /api/app/profile`
- 管理端接口：
  - 用户、角色、权限、平台授权、商品目录
  - 平台、配置、注册任务、任务查询、任务日志
  - 账号、平台动作、代理、Solver 状态
- 支付接口：
  - `POST /api/payment/callback/{channel_code}`

## 目录

```text
customer_portal_api/
├── app/
│   ├── routers/
│   ├── services/
│   ├── bootstrap.py
│   ├── config.py
│   ├── db.py
│   ├── deps.py
│   ├── models.py
│   └── security.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── main.py
```

## 本地启动

### 1. 安装依赖

在仓库根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

如果你只想按照新项目路径安装，也可以：

```bash
pip install -r customer_portal_api/requirements.txt
```

### 2. 配置环境变量

复制环境变量模板：

```bash
cp customer_portal_api/.env.example customer_portal_api/.env
```

常用变量：

- `PORTAL_JWT_SECRET`：留空则自动生成随机密钥并持久化到数据库同目录的
  `.portal_jwt_secret`；多实例部署必须显式设置同一个值。
- `PORTAL_ADMIN_USERNAME` / `PORTAL_ADMIN_PASSWORD` / `PORTAL_ADMIN_EMAIL`：引导管理员账号。
- `PORTAL_DATABASE_URL`：门户自己的库，默认 `customer_portal_api/customer_portal.db`。
  想让门户与主服务共用同一个库，就把它显式指到主服务的库文件上。
- `PORTAL_CORS_ORIGINS`、`PORTAL_PAYMENT_SECRET_<渠道>`：见 `.env.example` 内注释。

完整的 `PORTAL_*` 变量表见 `docs/configuration.md` 第 10 节。

### 3. 启动服务

在仓库根目录执行：

```bash
source .venv/bin/activate
export $(grep -v '^#' customer_portal_api/.env | xargs)
python -m uvicorn customer_portal_api.main:app --host 0.0.0.0 --port 8100 --reload
```

接口文档：

- Swagger UI: `http://127.0.0.1:8100/docs`
- OpenAPI JSON: `http://127.0.0.1:8100/openapi.json`

首次启动会自动写入引导管理员账号（用户名默认 `admin`）。

**不存在出厂密码**：未设置 `PORTAL_ADMIN_PASSWORD` 时会生成一次性随机口令并
打印到启动日志，请从日志中获取；旧默认值 `admin123456` 已被主动拒绝（见 `tests/test_customer_portal_security.py`）。

## Docker 部署

在仓库根目录执行：

```bash
docker compose -f customer_portal_api/docker-compose.yml up --build
```

服务默认监听：

- `http://127.0.0.1:8100`

## 设计说明

- 新项目复用当前仓库已有的平台注册和任务执行内核，不重新实现平台插件逻辑
- 新项目自己的用户、刷新 token、平台授权、订单、订阅、任务归属表默认落在门户独立的 `customer_portal_api/customer_portal.db`；若要和现有业务表共用同一个 SQLite 库，需把 `PORTAL_DATABASE_URL` 显式指向主服务的库文件
- 用户端注册接口会创建真实注册任务，并通过任务归属表限制用户只能看到自己的任务
- 支付链路已包含商品种子、下单、提交支付、支付回调、订阅开通和平台注册权限开通
