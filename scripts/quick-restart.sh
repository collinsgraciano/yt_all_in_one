#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  快速重启服务 — 不重建镜像，仅重启容器（秒级完成）
#  在服务器上执行：bash quick-restart.sh [服务名]
# ═══════════════════════════════════════════════════════════════
#  用法：
#    bash quick-restart.sh          # 重启所有服务
#    bash quick-restart.sh web      # 仅重启 web
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

SERVICE="${1:-}"

echo "─── 快速重启服务 ───"

if [ -z "$SERVICE" ]; then
    echo "重启所有服务..."
    docker-compose restart
else
    echo "重启服务: $SERVICE"
    docker-compose restart "$SERVICE"
fi

echo ""
echo "─── 服务状态 ───"
docker-compose ps
echo ""
echo "完成！"
