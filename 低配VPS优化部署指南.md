# 低配 VPS (1C2G) 优化部署指南

> 本文档详细说明如何将本项目部署在 1 核 CPU、2GB 内存的低配 VPS 上，并保持可用性能。

---

## 一、资源消耗分析

### 优化前（默认配置）

| 组件 | 内存占用 | 说明 |
|------|---------|------|
| PostgreSQL 16 | ~300-500MB | 默认 `shared_buffers=128MB`，连接多时更高 |
| Python (uvicorn + pipeline) | ~600-900MB | numpy/scipy/Pillow + BGM 混音 |
| Docker 开销 | ~50MB | 容器运行时 |
| 系统 | ~200MB | Linux 内核 + sshd 等 |
| **合计** | **~1.2-1.7GB** | ⚠️ 2GB 机器可能 OOM |

### 优化后

| 组件 | 内存占用 | 说明 |
|------|---------|------|
| PostgreSQL 16 | ~100-150MB | `shared_buffers=24MB`, `max_connections=15` |
| Python (uvicorn + pipeline) | ~400-700MB | `MALLOC_ARENA_MAX=2` + 连接池 + 降并发 |
| Docker 开销 | ~30MB | 日志压缩 |
| 系统 | ~200MB | |
| Swap | 2GB | 内存溢出保护 |
| **合计** | **~750MB-1.1GB** | ✅ 2GB 机器有余量 |

---

## 二、优化措施清单

### 1. PostgreSQL 低内存配置

**文件**: `docker/postgresql-lowmem.conf`

| 参数 | 默认值 | 优化值 | 效果 |
|------|--------|--------|------|
| `shared_buffers` | 128MB | 24-32MB | 减少 PG 常驻内存 ~100MB |
| `work_mem` | 4MB | 1-2MB | 限制排序/哈希内存 |
| `maintenance_work_mem` | 64MB | 8-16MB | 限制 VACUUM/索引内存 |
| `max_connections` | 100 | 15-20 | 每连接约 5-10MB |
| `max_parallel_workers` | 8 | 1 | 1C 环境禁用并行查询 |

### 2. 数据库连接池

**文件**: `backend/database.py`（Web 层）+ `pipeline/db.py`（Pipeline 层）

- 使用 `psycopg-pool` 复用 TCP 连接
- Web 层连接池大小：`min=1, max=5`
- Pipeline 层连接池大小：`min=1, max=3`
- **效果**：消除每次查询的 TCP 握手 + 认证开销（~5ms/次）
- **回退机制**：若 `psycopg-pool` 未安装，自动回退到直连模式

### 3. Docker 资源限制

**文件**: `docker-compose.lowmem.yml`

```yaml
postgres:
  deploy:
    resources:
      limits:
        cpus: "0.5"      # 限制 PG 最多用半个核
        memory: 300M      # 硬性内存上限
web:
  deploy:
    resources:
      limits:
        cpus: "1.0"      # Web + pipeline 可用满 1 核
        memory: 1400M     # 留 300MB 给系统
```

### 4. Swap 文件

**文件**: `scripts/setup-swap.sh`

- 创建 2GB Swap 文件
- `vm.swappiness=10`（仅内存紧张时使用）
- 写入 `/etc/fstab` 开机自动挂载

### 5. Pipeline 并发降级

**文件**: `pipeline/config.py`

| 参数 | 默认值 | 优化值 | 说明 |
|------|--------|--------|------|
| `DOWNLOAD_WORKERS` | 4 | 2 | 降低并发下载的内存峰值 |
| `DEEPFILTER_WORKERS` | 2 | 1 | DeepFilter 降噪串行执行 |
| BGM `lru_cache` | 8 | 4 | 减少 BGM 缓存内存 ~20MB |

### 6. Dockerfile 多阶段构建

**文件**: `docker/Dockerfile.web`

- **阶段 1 (builder)**：安装编译依赖（gcc、libpq-dev），`pip install --no-compile`
- **阶段 2 (runtime)**：仅拷贝已安装的包 + 运行时依赖
- **效果**：镜像体积从 ~1.2GB 降到 ~800MB（省去 gcc 等编译工具）

### 7. Python 内存优化

**Dockerfile 环境变量**：

```dockerfile
ENV MALLOC_ARENA_MAX=2
```

这是**最关键的优化**。glibc 默认为每个 CPU 核心创建 8 个内存 arena，每个 arena 预分配 384MB。在 1C 机器上：
- 默认：8 arena × 384MB = 最多 3GB 虚拟内存（碎片化后实际占用更高）
- 优化后：2 arena × 384MB = 最多 768MB

**效果**：节省 200-500MB 实际内存使用。

### 8. 日志优化

- Docker 日志：`max-size: 5m, max-file: 2`（从 50m×5 降到 5m×2）
- 日志写入：缓冲区从 20 条增加到 30 条，使用 `executemany` 批量 INSERT
- Uvicorn：`--no-access-log` 关闭访问日志（减少 I/O）

---

## 三、快速部署（首次）

> 完整的首次部署流程：从零开始在 1C2G VPS 上跑起来，全程约 15-20 分钟。

### 前置条件

- 一台 1C2G VPS（已安装 Docker + Docker Compose）
- 本地 Windows 电脑已安装 OpenSSH 或 PuTTY

> 如果 VPS 还没装 Docker，先执行：
> ```bash
> curl -fsSL https://get.docker.com | sh
> sudo systemctl enable docker
> ```

### 步骤 1：推送到 GitHub（本地 Windows）

```cmd
:: 初始化 git 仓库
git init
git add -A
git commit -m "initial commit"
git branch -M main

:: 添加远程仓库并推送
git remote add origin https://github.com/YOUR_USER/audiobook-manager.git
git push -u origin main

:: 或一键脚本：scripts\git-deploy.bat "initial commit"
```

> **认证**：GitHub 需用 [Personal Access Token](https://github.com/settings/tokens)（勾选 `repo`权限）。

### 步骤 2：服务器克隆并部署（SSH 登录）

```bash
# 安装 git
apt install -y git

# 克隆仓库
git clone https://github.com/collinsgraciano/yt_all_in_one.git /opt/audiobook
cd /opt/audiobook

# 创建 .env 配置
cp .env.example .env
nano .env
```

首次部署（构建镜像约 5-10 分钟）：
```bash
bash scripts/git-server-deploy.sh
```

### 步骤 3：配置服务器环境变量（SSH 登录）

首次部署后需要配置 `.env`：

```bash
ssh root@你的服务器IP
cd /opt/audiobook
nano .env
```

修改为：
```ini
DB_MODE=self
POSTGRES_PASSWORD=你的强密码
SECRET_KEY=你的随机密钥
BASE_URL=http://你的服务器IP:8080
APP_PASSWORD=你的登录密码
```

### 步骤 4：启用低配优化并重启（SSH 登录）

```bash
cd /opt/audiobook

# 创建 Swap（仅需一次）
sudo bash scripts/setup-swap.sh

# 低配模式部署（自动加载 docker-compose.lowmem.yml）
bash scripts/lowmem-deploy.sh
```

`lowmem-deploy.sh` 会自动：
1. 检查/创建 Swap
2. 调用 `deploy.sh --lowmem`（附加低配覆盖文件）
3. 构建并启动服务
4. 健康检查

### 步骤 5：验证部署

```bash
# 查看容器资源占用
docker stats

# 查看系统内存
free -h

# 查看 Swap
swapon --show
```

预期输出：
```
CONTAINER           CPU %   MEM USAGE / LIMIT
audiobook_postgres  0.5%    120MiB / 300MiB
audiobook_web       2.1%    450MiB / 1400MiB
```

浏览器访问 `http://你的服务器IP:8080`，看到登录页即部署成功。

---

## 四、日常更新代码

> 部署完成后，每次修改代码只需两步操作，约 **10-30 秒**完成更新。

### 4.1 日常更新（两步）

#### 第一步：开发机推送

```cmd
:: 方式一：一键推送
scripts\git-deploy.bat "修复了某bug"

:: 方式二：手动命令
git add -A && git commit -m "修复了某bug" && git push
```

#### 第二步：服务器拉取并部署

```bash
ssh root@你的服务器IP
cd /opt/audiobook
bash scripts/git-server-deploy.sh
```

脚本自动完成：`git pull` → 智能判断是否重建镜像 → 重启服务 → 健康检查。


### 4.2 智能构建机制

`git-server-deploy.sh` 会自动判断是否需要重建 Docker 镜像：

#### 情况 A：只改了 Python 代码（最常见）

```
[Step 6/6] 构建与重启服务...
  依赖未变更，跳过镜像构建（秒级更新）
  正在重启服务...
  ✓ Web 服务已就绪
```

代码通过 Docker 卷挂载即时生效，**约 5-10 秒**。

#### 情况 B：改了 requirements.txt 或 Dockerfile

```
[Step 6/6] 构建与重启服务...
  检测到 requirements.txt 变更，需要重建镜像
  正在构建镜像（可能需要几分钟）...
```

自动重建镜像后重启，**约 2-5 分钟**。

### 4.3 低配模式下的更新注意事项

> **提示**：`git-server-deploy.sh` 会自动读取 `.env` 中的 `DB_MODE`。
> 如果服务器使用低配模式，部署后需确保用低配配置启动：

```bash
ssh root@你的服务器IP
cd /opt/audiobook
bash scripts/lowmem-deploy.sh
```

或者直接手动指定 compose 文件：

```bash
# 自建数据库 + 低配
docker-compose -f docker-compose.yml -f docker-compose.self-db.yml -f docker-compose.lowmem.yml up -d

# 外部数据库 + 低配
docker-compose -f docker-compose.yml -f docker-compose.external-db.yml -f docker-compose.lowmem.yml up -d
```

### 4.4 快速重启（不更新代码）

如果只修改了 `.env` 配置或需要重启服务：

```bash
cd /opt/audiobook
bash scripts/quick-restart.sh          # 重启所有服务
bash scripts/quick-restart.sh web      # 仅重启 Web 服务
```

### 4.5 回滚到历史版本

> **低配模式注意**：低配模式下回滚后也需手动用低配配置启动：
> ```bash
> docker-compose -f docker-compose.yml -f docker-compose.self-db.yml -f docker-compose.lowmem.yml up -d
> ```

```bash
git log --oneline -10
git checkout abc1234
bash scripts/git-server-deploy.sh
```

### 4.6 完整更新流程图

```
本地修改代码
     │
     ▼
scripts\git-deploy.bat "msg"  ────→  git push to GitHub
     │                                    │
     │                            ssh root@server
     │                            cd /opt/audiobook
     │                            bash scripts/git-server-deploy.sh
     │                                    │
     │                         ┌──────────┴──────────┐
     │                         │                     │
     │                   代码变了?            依赖变了?
     │                         │                     │
     │                    秒级重启              重建镜像
     │                    (5-10秒)             (2-5分钟)
     │                         │                     │
     │                         └──────────┬──────────┘
     │                                    │
     ▼                                    ▼
低配模式? ──Yes──→ docker-compose ... lowmem.yml up -d
     │
     No
     │
     ▼
  完成！访问 http://服务器IP:8080
```

---

## 五、性能调优建议

### 5.1 根据实际使用调整

| 场景 | 建议调整 |
|------|---------|
| 书籍数量少 (<100) | 默认配置即可 |
| 书籍数量多 (>500) | 考虑升级到 2C4G |
| 频繁运行 pipeline | 确保 Swap 已创建 |
| 仅做管理不跑 pipeline | 可进一步降低 web 内存限制到 800M |

### 5.2 监控 OOM

```bash
# 检查是否有 OOM 事件
dmesg | grep -i "out of memory"

# 检查容器是否被 OOM Kill
docker inspect audiobook_web | grep OOMKilled
```

如果频繁 OOM：
1. 确认 Swap 已创建：`swapon --show`
2. 降低 `DEEPFILTER_WORKERS` 到 0（禁用降噪）
3. 降低 `VIDEO_RESOLUTION` 到 `720p`
4. 考虑禁用 BGM 混音：`ENABLE_BGM_MIX=false`

### 5.3 Pipeline 运行时覆盖

在 Web 界面的「任务创建」页面，可以针对单个任务调整配置覆盖：

```json
{
  "DOWNLOAD_WORKERS": 2,
  "DEEPFILTER_WORKERS": 1,
  "VIDEO_RESOLUTION": "720p",
  "ENABLE_BGM_MIX": true
}
```

---

## 六、各优化文件的对应关系

| 优化项 | 文件 | 说明 |
|--------|------|------|
| PG 低内存配置 | `docker/postgresql-lowmem.conf` | 挂载到 PG 容器 |
| Web 层连接池 | `backend/database.py` | 自动使用，无需调用方修改 |
| Pipeline 层连接池 | `pipeline/db.py` | 惰性初始化，自动回退直连 |
| Docker 资源限制 | `docker-compose.lowmem.yml` | 覆盖文件 |
| Swap 脚本 | `scripts/setup-swap.sh` | 一次性执行 |
| 部署脚本 | `scripts/lowmem-deploy.sh` | 一键部署 |
| Dockerfile 优化 | `docker/Dockerfile.web` | 多阶段构建 |
| Pipeline 参数 | `pipeline/config.py` | 默认降并发 |
| BGM 缓存 | `pipeline/bgm.py` | `lru_cache` 降到 4 |
| 日志优化 | `backend/log_interceptor.py` | 批量写入 |

---

## 七、不推荐的进一步优化

以下优化虽然能进一步降低资源，但会显著影响功能，**不建议**在 1C2G 上尝试：

| 优化 | 影响 |
|------|------|
| 禁用 DeepFilter 降噪 | 音质下降 |
| 禁用 BGM 混音 | 无背景音乐 |
| 禁用 AI 封面生成 | 需手动上传封面 |
| 降级到 480p 视频 | YouTube 画质太低 |
| 使用 SQLite 替代 PG | 不支持并发写入 |
