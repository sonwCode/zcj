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
sudo cp -r "${APP_DIR}/frontend/dist" "${BACKUP_DIR}/" 2>/dev/null || true
sudo cp -r "${APP_DIR}/api" "${BACKUP_DIR}/" 2>/dev/null || true
sudo cp -r "${APP_DIR}/core" "${BACKUP_DIR}/" 2>/dev/null || true
sudo cp -r "${APP_DIR}/application" "${BACKUP_DIR}/" 2>/dev/null || true
sudo cp -r "${APP_DIR}/infrastructure" "${BACKUP_DIR}/" 2>/dev/null || true
echo "✓ 备份完成"

echo "等待文件同步..."
# rsync 会在服务器端执行，文件由 SSH 传输
