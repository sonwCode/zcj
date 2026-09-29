#!/bin/bash
set -e

COMMIT="ce30584"
BACKUP_DIR="/opt/zcj-register-backups"
DEPLOY_DIR="/opt/zcj-register"
TIMESTAMP=$(date +%Y%m%dT%H%M%SZ)

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "部署 commit ${COMMIT} @ ${TIMESTAMP}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 备份当前版本
echo "[1/6] 备份当前版本..."
if [ -d "${DEPLOY_DIR}" ]; then
  CURRENT_COMMIT=$(cd "${DEPLOY_DIR}" && git rev-parse --short HEAD 2>/dev/null || echo "unknown")
  sudo mkdir -p "${BACKUP_DIR}"
  sudo cp -a "${DEPLOY_DIR}" "${BACKUP_DIR}/${CURRENT_COMMIT}-${TIMESTAMP}"
  echo "  ✓ 备份到 ${BACKUP_DIR}/${CURRENT_COMMIT}-${TIMESTAMP}"
fi

# 停止服务
echo "[2/6] 停止服务..."
sudo systemctl stop zcj-register || true
echo "  ✓ 服务已停止"

# 更新代码
echo "[3/6] 更新代码到 ${COMMIT}..."
cd "${DEPLOY_DIR}"
sudo git fetch origin
sudo git checkout ${COMMIT}
echo "  ✓ 代码已更新"

# 保护数据目录
echo "[4/6] 保护数据和配置..."
sudo chmod -R 755 "${DEPLOY_DIR}/data" 2>/dev/null || true
sudo chmod -R 755 "${DEPLOY_DIR}/.env" 2>/dev/null || true
sudo chmod -R 755 "${DEPLOY_DIR}/.runtime" 2>/dev/null || true
sudo chmod -R 755 "${DEPLOY_DIR}/mihomo" 2>/dev/null || true
echo "  ✓ 数据目录已保护"

# 安装依赖（如果需要）
echo "[5/6] 检查依赖..."
if [ -f "requirements.txt" ]; then
  sudo /opt/zcj-register/.venv/bin/pip install -q -r requirements.txt 2>/dev/null || echo "  ⚠ 依赖安装跳过"
fi
echo "  ✓ 依赖检查完成"

# 启动服务
echo "[6/6] 启动服务..."
sudo systemctl start zcj-register
sleep 3
sudo systemctl status zcj-register --no-pager -l || true
echo "  ✓ 服务已启动"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✓ 部署完成"
echo "  Commit: ${COMMIT}"
echo "  服务地址: https://register.feixueapi.xyz"
echo "  健康检查: https://register.feixueapi.xyz/api/health"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
