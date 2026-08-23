# Oreateai 平台插件

## 功能概述

AI视频生成平台，注册送80积分。核心边界：
- ✅ 自动收信、自动提取验证链接
- ⚠️ 只在 allowlist 域名内自动访问验证链接
- ❌ 不做批量注册、不绕过平台风控

## 文件结构

```
platforms/oreateai/
├── __init__.py              # 模块导出
├── core.py                  # HTTP 注册核心逻辑
├── helper.py                # 辅助函数（打码、脱敏）
├── plugin.py                # 平台插件定义
├── protocol_mailbox.py      # 协议邮箱注册 Worker
├── browser_register.py      # 浏览器注册（预留）
├── browser_oauth.py         # OAuth 登录（预留）
└── README.md                # 本文档
```

## 使用的邮箱服务

项目已有 `outlook_email_mailbox.py`，支持：
- 地址：`https://mail-api.example.com/`
- API：`GET /api/external/emails` 获取邮件
- 自动提取验证链接

## 核心流程

```
1. 填写邮箱密码 → 注册页面
2. 调用 /api/external/emails → 获取验证邮件
3. 提取邮件中的 verify 链接
4. 检查域名白名单
   ├── 白名单内 → 自动访问
   └── 非白名单 → 人工确认
5. 返回账号信息（积分、token）
```

## 安全边界

### ✅ 自动执行
- 收信（轮询邮件列表）
- 提取验证链接
- 自动访问白名单域名的验证链接

### ⚠️ 需要确认
- 非白名单域名的验证链接
- 批量操作

### ❌ 禁止执行
- 批量注册
- 绕过平台风控
- 泄露 API Key、Token、密码

## 白名单域名

```python
ALLOWED_VERIFY_DOMAINS = [
    "oreateai.com",
    "www.oreateai.com",
]
```

## 待抓包确认

1. **注册 API 请求格式**
   - URL: `/api/auth/register`
   - 字段: email, password, csrf_token

2. **验证确认流程**
   - 确认链接格式
   - 积分到账时机

3. **积分查询 API**
   - URL
   - 返回数据结构

4. **视频生成 API**
   - URL
   - 参数
   - 返回格式

## 使用示例

### 1. 注册一个账号

```python
from platforms.oreateai import OreateaiPlatform
from core.outlook_email_mailbox import OutlookEmailMailbox
from core.base_platform import RegisterConfig

# 配置邮箱服务
mailbox = OutlookEmailMailbox(
    api_url="https://mail-api.example.com/",
    api_key="your-api-key"
)

# 创建平台
platform = OreateaiPlatform(
    config=RegisterConfig(executor_type="protocol"),
    mailbox=mailbox
)

# 注册
account = platform.register(
    email="your-email@domain.com",
    password="your-password"
)

print(f"积分: {account.extra.get('credits')}")
```

### 2. 人工确认验证链接

```python
# 如果验证链接不在白名单，会返回 pending_confirmation=True
if account.extra.get("pending_confirmation"):
    link = account.extra.get("verification_link")
    print(f"请手动访问: {link}")
```

### 3. 查询积分

```python
quota = platform.get_quota(account)
print(f"剩余积分: {quota.get('credits')}")
```

## 配置项

### 环境变量

```bash
# outlookEmail 配置
OUTLOOK_EMAIL_API_URL=https://mail-api.example.com/
OUTLOOK_EMAIL_API_KEY=your-api-key

# 代理（可选）
PROXY=http://proxy:port
```

### 白名单配置

编辑 `plugin.py` 修改 `ALLOWED_VERIFY_DOMAINS`：

```python
ALLOWED_VERIFY_DOMAINS = [
    "oreateai.com",
    "www.oreateai.com",
    # 添加其他可信域名
]
```

## 日志输出示例

```
[Oreateai] 1. 访问注册页面...
[Oreateai]    页面状态: 200
[Oreateai]    CSRF: abc123...
[Oreateai] 2. 提交注册: test@example.com
[Oreateai]    API 响应: 200
[Oreateai] 3. 等待验证邮件...
[Oreateai]    获取到验证链接: https://www.oreateai.com/verify?token=***...
[Oreateai]    白名单域名，自动确认
[Oreateai] 4. 确认邮箱...
[Oreateai]    响应: 200
[Oreateai] ✓ 注册成功! 积分: 80
```

## 故障排除

### 问题：无法获取验证链接

1. 检查 outlookEmail API 是否可用
2. 检查邮箱是否正确
3. 检查邮件是否已发送
4. 检查关键词过滤是否正确

### 问题：白名单域名被拒绝

确认验证链接的域名是否在 `ALLOWED_VERIFY_DOMAINS` 内。

### 问题：注册失败

1. 检查网络连接
2. 检查代理设置
3. 尝试手动注册确认是否被风控

## 状态机

```
WAIT_EMAIL      → 等待验证邮件
LINK_FOUND      → 找到验证链接
NEED_CONFIRM    → 等待人工确认（非白名单）
VERIFYING       → 正在确认
VERIFIED        → 验证成功，获得积分
EXPIRED         → 链接过期
FAILED          → 失败
```
