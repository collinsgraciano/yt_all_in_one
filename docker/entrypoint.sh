#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# 容器启动入口 — 持久化资源初始化（幂等）
# ═══════════════════════════════════════════════════════════════
# 作用：
#   1. 从宿主机挂载目录 /opt/deepfilter/（= ./data/deepfilter/）
#      拷贝 DeepFilter 二进制到运行卷 /data/output/.deepfilter/（仅首次）
#      若宿主机也没有则自动下载到宿主机持久目录（一次下载永久保存）
#   2. BGM 音乐库 → /data/music/（宿主机挂载，一次下载永久保存）
# ═══════════════════════════════════════════════════════════════
set -e

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🎵 有声书 YouTube 管理系统 — 容器启动"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ─── 1. DeepFilter 降噪二进制 ───
# /opt/deepfilter/ 是宿主机挂载的持久目录(./data/deepfilter/)
# 下载一次后永久保存，重建镜像不需要重新下载
DEEPFILTER_BIN="deep-filter-0.5.6-x86_64-unknown-linux-musl"
DEEPFILTER_VOL="/data/output/.deepfilter"
DEEPFILTER_HOST="/opt/deepfilter"
DEEPFILTER_URL="https://github.com/Rikorose/DeepFilterNet/releases/download/v0.5.6/deep-filter-0.5.6-x86_64-unknown-linux-musl"

mkdir -p "$DEEPFILTER_VOL" "$DEEPFILTER_HOST"

# 运行卷上不存在 → 从宿主机持久目录拷贝
if [ ! -f "$DEEPFILTER_VOL/$DEEPFILTER_BIN" ]; then
    if [ -f "$DEEPFILTER_HOST/$DEEPFILTER_BIN" ]; then
        echo "📦 从宿主机缓存拷贝 DeepFilter 到运行卷..."
        cp "$DEEPFILTER_HOST/$DEEPFILTER_BIN" "$DEEPFILTER_VOL/$DEEPFILTER_BIN"
        chmod +x "$DEEPFILTER_VOL/$DEEPFILTER_BIN"
        echo "✅ DeepFilter 已拷贝到 $DEEPFILTER_VOL"
    else
        # 宿主机也没有 → 下载到宿主机持久目录（一次下载，永久保存）
        echo "⚠️ DeepFilter 不在缓存中，正在下载到宿主机持久目录..."
        if wget --tries=5 --timeout=30 --retry-connrefused \
            "$DEEPFILTER_URL" \
            -O "$DEEPFILTER_HOST/$DEEPFILTER_BIN"; then
            chmod +x "$DEEPFILTER_HOST/$DEEPFILTER_BIN"
            cp "$DEEPFILTER_HOST/$DEEPFILTER_BIN" "$DEEPFILTER_VOL/$DEEPFILTER_BIN"
            chmod +x "$DEEPFILTER_VOL/$DEEPFILTER_BIN"
            echo "✅ DeepFilter 已下载并拷贝到运行卷"
        else
            echo "⚠️ DeepFilter 下载失败，将在首次 pipeline 运行时自动重试"
        fi
    fi
else
    echo "✅ DeepFilter 在运行卷上已就绪"
fi

# ─── 2. BGM 音乐库 ───
# /data/music/ 是宿主机挂载的持久目录(./data/music/)
# 部署脚本预下载，容器启动时检查是否有音频，没有则回退下载
MUSIC_DIR="${MUSIC_DIR:-/data/music}"
MUSIC_ZIP_URL="https://huggingface.co/datasets/oooooo1323/cm/resolve/main/Parisian%20Breeze.zip"
mkdir -p "$MUSIC_DIR"

# 检查是否已有音频文件
EXISTING_COUNT=$(find "$MUSIC_DIR" -type f \( -iname "*.mp3" -o -iname "*.wav" -o -iname "*.flac" -o -iname "*.m4a" -o -iname "*.ogg" -o -iname "*.aac" -o -iname "*.wma" \) 2>/dev/null | wc -l)

if [ "$EXISTING_COUNT" -gt 0 ]; then
    echo "🎵 音乐库已有 ${EXISTING_COUNT} 个音频文件"
else
    echo "🎵 音乐库为空，正在下载 BGM 音乐包..."
    MUSIC_ARCHIVE="/tmp/Parisian_Breeze.zip"
    if wget --tries=5 --timeout=60 --retry-connrefused \
        "$MUSIC_ZIP_URL" -O "$MUSIC_ARCHIVE"; then
        echo "  > 解压音乐包..."
        python3 -c "
import zipfile, sys
with zipfile.ZipFile(sys.argv[1]) as z:
    z.extractall(sys.argv[2])
" "$MUSIC_ARCHIVE" "$MUSIC_DIR"
        rm -f "$MUSIC_ARCHIVE"
        NEW_COUNT=$(find "$MUSIC_DIR" -type f \( -iname "*.mp3" -o -iname "*.wav" -o -iname "*.flac" -o -iname "*.m4a" -o -iname "*.ogg" -o -iname "*.aac" -o -iname "*.wma" \) 2>/dev/null | wc -l)
        echo "✅ BGM 音乐已下载，共 ${NEW_COUNT} 个音频文件"
    else
        echo "⚠️ BGM 音乐下载失败，混音阶段将跳过"
    fi
fi

# ─── 3. 确保输出目录存在 ───
OUTPUT_DIR="${OUTPUT_ROOT:-/data/output}"
mkdir -p "$OUTPUT_DIR"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ 持久化资源初始化完成，启动 Web 服务..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 执行 CMD（uvicorn）
exec "$@"