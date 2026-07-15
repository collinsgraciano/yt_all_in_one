#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# 容器启动入口 — 持久化资源初始化（幂等）
# ═══════════════════════════════════════════════════════════════
# 作用：
#   1. 将构建时下载到 /opt/deepfilter/ 的 DeepFilter 二进制
#      拷贝到持久卷 /data/output/.deepfilter/（仅首次）
#   2. 预下载 BGM 音乐到 /data/music/（仅首次，已有则跳过）
# ═══════════════════════════════════════════════════════════════
set -e

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🎵 有声书 YouTube 管理系统 — 容器启动"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ─── 1. DeepFilter 降噪二进制 ───
DEEPFILTER_BIN="deep-filter-0.5.6-x86_64-unknown-linux-musl"
DEEPFILTER_VOL="/data/output/.deepfilter"
DEEPFILTER_BAKED="/opt/deepfilter"

mkdir -p "$DEEPFILTER_VOL"

# 卷上不存在 → 从镜像内置位置拷贝
if [ ! -f "$DEEPFILTER_VOL/$DEEPFILTER_BIN" ]; then
    if [ -f "$DEEPFILTER_BAKED/$DEEPFILTER_BIN" ]; then
        echo "📦 从镜像拷贝 DeepFilter 到持久卷..."
        cp "$DEEPFILTER_BAKED/$DEEPFILTER_BIN" "$DEEPFILTER_VOL/$DEEPFILTER_BIN"
        cp "$DEEPFILTER_BAKED/$DEEPFILTER_BIN.bak" "$DEEPFILTER_VOL/$DEEPFILTER_BIN.bak" 2>/dev/null || true
        chmod +x "$DEEPFILTER_VOL/$DEEPFILTER_BIN"
        echo "✅ DeepFilter 已拷贝到 $DEEPFILTER_VOL"
    elif [ -f "$DEEPFILTER_BAKED/$DEEPFILTER_BIN.bak" ]; then
        echo "📦 从镜像备份恢复 DeepFilter..."
        cp "$DEEPFILTER_BAKED/$DEEPFILTER_BIN.bak" "$DEEPFILTER_VOL/$DEEPFILTER_BIN"
        chmod +x "$DEEPFILTER_VOL/$DEEPFILTER_BIN"
        echo "✅ DeepFilter 已恢复"
    else
        echo "⚠️ DeepFilter 未预下载，将在首次 pipeline 运行时自动下载"
    fi
else
    echo "✅ DeepFilter 在持久卷上已就绪"
fi

# ─── 2. BGM 音乐库 ───
MUSIC_DIR="${MUSIC_DIR:-/data/music}"
mkdir -p "$MUSIC_DIR"

# 检查是否已有音频文件
EXISTING_COUNT=$(find "$MUSIC_DIR" -type f \( -iname "*.mp3" -o -iname "*.wav" -o -iname "*.flac" -o -iname "*.m4a" -o -iname "*.ogg" -o -iname "*.aac" -o -iname "*.wma" \) 2>/dev/null | wc -l)

if [ "$EXISTING_COUNT" -gt 0 ]; then
    echo "🎵 音乐库已有 ${EXISTING_COUNT} 个音频文件，跳过预下载"
else
    echo "🎵 音乐库为空，将在首次 pipeline 运行时自动下载（持久化到 $MUSIC_DIR）"
    echo "   提示：可在 Web 管理面板 → 全局设置 中配置 HF_DATASET_ZIP_URLS"
fi

# ─── 3. 确保输出目录存在 ───
OUTPUT_DIR="${OUTPUT_ROOT:-/data/output}"
mkdir -p "$OUTPUT_DIR"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ 持久化资源初始化完成，启动 Web 服务..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 执行 CMD（uvicorn）
exec "$@"