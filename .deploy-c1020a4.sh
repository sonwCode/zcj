#!/usr/bin/env bash
set -euo pipefail

HOST=main-server
TGT=/opt/zcj-register
NEXT=/opt/zcj-register.next
ARCHIVE=/tmp/zcj-fused-c1020a4.tar.gz
BACKUPS=/opt/zcj-register-backups

echo "[1/8] 打包本地代码..."
cd /home/hatch/dsh-harness/home/ds/zcj
tar czf "${ARCHIVE}"   --exclude='.git'   --exclude='node_modules'   --exclude='__pycache__'   --exclude='*.pyc'   --exclude='.pytest_cache'   .

echo "[2/8] 上传到云端..."
scp "${ARCHIVE}" ${HOST}:/tmp/

echo "[3/8] 在云端解压..."
ssh ${HOST} "
  sudo rm -rf ${NEXT}
  sudo mkdir -p ${NEXT}
  cd /tmp && sudo tar xzf ${ARCHIVE} -C ${NEXT}
"

echo "[4/8] 保护云端现有数据和配置..."
ssh ${HOST} "
  if [ -d ${TGT}/data ]; then
    sudo cp -a ${TGT}/data ${NEXT}/
  fi
  if [ -f ${TGT}/.env ]; then
    sudo cp ${TGT}/.env ${NEXT}/
  fi
  if [ -d ${TGT}/.runtime ]; then
    sudo cp -a ${TGT}/.runtime ${NEXT}/
  fi
  if [ -d ${TGT}/mihomo ]; then
    sudo cp -a ${TGT}/mihomo ${NEXT}/
  fi
"

echo "[5/8] 停止服务..."
ssh ${HOST} "sudo systemctl stop zcj-register"

echo "[6/8] 备份旧版本..."
ssh ${HOST} "
  sudo mkdir -p ${BACKUPS}
  BACKUP_NAME=c1020a4-\$(date -u +%Y%m%dT%H%M%SZ)
  if [ -d ${TGT} ]; then
    sudo mv ${TGT} ${BACKUPS}/\${BACKUP_NAME}
  fi
"

echo "[7/8] 切换到新版本..."
ssh ${HOST} "sudo mv ${NEXT} ${TGT}"

echo "[8/8] 启动服务..."
ssh ${HOST} "sudo systemctl start zcj-register && sleep 3 && sudo systemctl status zcj-register --no-pager"

echo ""
echo "✓ 部署完成！commit: c1020a4"
echo "  验证: https://register.feixueapi.xyz/api/health"
