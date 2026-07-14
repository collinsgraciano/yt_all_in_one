#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# 创建 2GB Swap 文件 — 为 1C2G VPS 提供内存溢出保护
# ═══════════════════════════════════════════════════════════════
# 用法：sudo bash scripts/setup-swap.sh
# ═══════════════════════════════════════════════════════════════

set -e

SWAP_SIZE="2G"
SWAP_FILE="/swapfile"

# 检查是否已存在 swap
if [ -f "$SWAP_FILE" ]; then
    echo "⚠️  Swap 文件已存在: $SWAP_FILE"
    swapon --show
    exit 0
fi

echo "🔧 创建 ${SWAP_SIZE} Swap 文件..."

# 创建 swap 文件
fallocate -l "$SWAP_SIZE" "$SWAP_FILE" 2>/dev/null || {
    echo "fallocate 不可用，使用 dd..."
    dd if=/dev/zero of="$SWAP_FILE" bs=1M count=2048
}

# 设置权限
chmod 600 "$SWAP_FILE"

# 格式化为 swap
mkswap "$SWAP_FILE"

# 启用 swap
swapon "$SWAP_FILE"

# 写入 fstab 实现开机自动挂载
if ! grep -q "$SWAP_FILE" /etc/fstab; then
    echo "$SWAP_FILE none swap sw 0 0" >> /etc/fstab
    echo "✅ 已写入 /etc/fstab（开机自动挂载）"
fi

# 调整 swappiness（降低优先级，仅在内存紧张时使用）
sysctl vm.swappiness=10
if ! grep -q "vm.swappiness" /etc/sysctl.conf; then
    echo "vm.swappiness=10" >> /etc/sysctl.conf
fi

echo ""
echo "✅ Swap 配置完成！"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
swapon --show
free -h
