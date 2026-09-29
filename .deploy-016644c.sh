#!/bin/bash
set -e

DEPLOY_COMMIT="016644c"
BACKUP_NAME="016644c-$(date +%Y%m%dT%H%M%S)Z"
BACKUP_DIR="/opt/zcj-register-backups/${BACKUP_NAME}"
APP_DIR="/opt/zcj-register"

echo "========================================"
echo "部署 Commit: ${DEPLOY_COMMIT}"
echo "备份目录: ${BACKUP_DIR}"
echo "========================================"
echo ""

# 停止服务
echo "停止服务..."
sudo systemctl stop zcj-register

# 创建备份
echo "创建备份到 ${BACKUP_DIR}..."
sudo mkdir -p "${BACKUP_DIR}"
sudo cp -r "${APP_DIR}"/* "${BACKUP_DIR}/" || true
echo "✓ 备份完成"

# 保护关键目录（不会被 git 覆盖）
echo "保护数据目录..."
cd "${APP_DIR}"
# data/, .env, .runtime/, mihomo/ 由 .gitignore 保护，不会被 pull 影响

# 拉取最新代码
echo "拉取代码..."
sudo git fetch origin
sudo git reset --hard "${DEPLOY_COMMIT}"
echo "✓ 代码已更新到 ${DEPLOY_COMMIT}"

# 构建前端
echo "构建前端..."
cd "${APP_DIR}/frontend"
sudo pnpm install --frozen-lockfile
sudo pnpm run build
echo "✓ 前端构建完成"

# 启动服务
echo "启动服务..."
sudo systemctl start zcj-register

# 等待服务启动
echo "等待服务启动..."
sleep 5

# 检查服务状态
echo "检查服务状态..."
sudo systemctl status zcj-register --no-pager -l | head -20

echo ""
echo "========================================"
echo "部署完成"
echo "========================================"
echo "健康检查: https://register.feixueapi.xyz/api/health"
echo "前端: https://register.feixueapi.xyz"
echo ""
echo "验证步骤:"
echo "1. 登录系统"
echo "2. 进入 设置 -> 代理 tab"
echo "3. 确认看到两个子 Tab:"
echo "   - 代理 URL 管理"
echo "   - 全部节点管理"
echo "========================================"
