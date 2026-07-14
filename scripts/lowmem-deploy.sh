#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# 低配 VPS (1C2G) 一键部署脚本
# ═══════════════════════════════════════════════════════════════
# 自动识别 .env 中的 DB_MODE 选择数据库模式
# 用法：bash scripts/lowmem-deploy.sh
# ═══════════════════════════════════════════════════════════════

set -e

cd "$(dirname "$0")/.."

echo "═══════════════════════════════════════════════════"
echo "  低配 VPS (1C2G) 部署"
echo "═══════════════════════════════════════════════════"

# 1. 检查并创建 Swap
echo ""
echo "［1/2］检查 Swap..."
if [ ! -f /swapfile ]; then
    echo "  Swap 不存在，正在创建..."
    sudo bash scripts/setup-swap.sh
else
    echo "  ✅ Swap 已存在"
    swapon --show
fi

# 2. 调用智能部署脚本（带 --lowmem 参数）
echo ""
echo "［2/2］启动智能部署..."
bash scripts/deploy.sh --lowmem

echo ""
echo "  查看资源: docker stats"
echo ""
