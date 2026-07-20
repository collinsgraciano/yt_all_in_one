---
title: Audiobook Unified Worker
emoji: 📚
colorFrom: indigo
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---

# Audiobook Unified Worker

HF Space 远程执行器 — 流水线任务 + 测试实验合一，双槽位隔离资源。

## 槽位说明

| 槽位 | 用途 | 模式 |
|------|------|------|
| pipeline_slots | 重型流水线任务（TG下载→混音→AI→封装→上传） | 队列认领 |
| test_slots | 轻量测试实验（AI/上传/TG下载/BGM混音） | 同步执行 |

双槽位独立计数，互不阻塞：流水线任务运行时仍可接受测试请求。

## API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查（VPS 调度器调用） |
| `/status` | GET | 详细状态（槽位、当前任务、进度） |
| `/process` | POST | 触发认领流水线任务 |
| `/test/*` | POST | 测试实验接口 |

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `VPS_RELAY_URL` | - | VPS 中继地址（拉取配置、回调结果） |
| `PIPELINE_SLOTS` | `1` | 流水线并发槽位数 |
| `TEST_SLOTS` | `1` | 测试并发槽位数 |
| `OUTPUT_ROOT` | `/tmp/output` | 临时输出目录 |
| `MUSIC_DIR` | `/data/music` | BGM 音乐缓存目录 |
| `MUSIC_ZIP_URL` | - | BGM 音乐 zip 包下载地址 |

## 运行原理

1. 启动时从 VPS 中继拉取配置（`/api/pipeline-config`）
2. 自动下载 BGM 音乐到 `/data/music`（HF Datasets）
3. 监听 VPS 调度器的 `/process` 触发，认领 `hf_jobs` 队列任务
4. 任务完成后回调 VPS 中继 `/api/callback`
5. YouTube 上传通过 VPS 分发的短期 access_token 直连 API
