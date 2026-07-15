# 🖥️ VPS 部署完整教程

> 从购买 VPS 到线上访问，每一步都有详细操作说明。跟着做就行。

---

## 目录

- [第一章：购买 VPS](#第一章购买-vps)
- [第二章：VPS 初始安全配置](#第二章vps-初始安全配置)
- [第三章：安装 Docker 环境](#第三章安装-docker-环境)
- [第四章：本地 SSH 密码配置](#第四章本地-ssh-密码配置)
- [第五章：初始化 Git 仓库并推送到 GitHub](#第五章初始化-git-仓库并推送到-github)
- [第六章：服务器首次部署](#第六章服务器首次部署)
- [第七章：配置服务器环境变量](#第七章配置服务器环境变量)
- [第八章：Web 密码登录与直接访问](#第八章web-密码登录与直接访问)
- [第九章：配置 Google OAuth 回调地址](#第九章配置-google-oauth-回调地址)
- [第十章：GitHub 部署与日常更新](#第十章github-部署与日常更新)
- [第十一章：手动从 GitHub 下载部署和更新](#第十一章手动从-github-下载部署和更新)
- [第十二章：服务器日常运维](#第十二章服务器日常运维)
- [第十三章：数据备份与恢复](#第十三章数据备份与恢复)
- [第十四章：故障排查](#第十四章故障排查)
- [附录 A：完整命令速查表](#附录-a完整命令速查表)
- [附录 B：架构图](#附录-b架构图)

---

## 第一章：购买 VPS

### 1.1 推荐配置

本项目需要运行 PostgreSQL 和 FastAPI Web 两个容器（任务通过 Python 后台线程执行，无需 Celery/Redis），Web 容器会执行音视频处理（ffmpeg），所以对配置有一定要求：

| 资源 | 最低配置 | 推荐配置 | 说明 |
|------|---------|---------|------|
| CPU | 2 核 | 4 核 | 音频处理需要算力 |
| 内存 | 2 GB | 4 GB | 两个容器 + ffmpeg 编码 |
| 硬盘 | 40 GB SSD | 80 GB SSD | 音频输出文件会占用空间 |
| 带宽 | 5 Mbps | 10+ Mbps | 上传视频到 YouTube |
| 系统 | Ubuntu 22.04 LTS | Ubuntu 22.04 LTS | 本教程以此为准 |

### 1.2 推荐 VPS 供应商

| 供应商 | 特点 | 适合场景 |
|--------|------|---------|
| **搬瓦工 (BandwagonHost)** | CN2 GIA 线路，国内访问快 | 面向国内用户管理 |
| **Vultr** | 按小时计费，全球节点 | 灵活测试 |
| **DigitalOcean** | 稳定，文档丰富 | 长期稳定运行 |
| **腾讯云轻量** | 国内访问极快，需备案 | 国内用户首选 |
| **阿里云 ECS** | 国内云，生态完善 | 企业级部署 |

> **注意**：如果你的 YouTube 频道面向海外观众，建议选择海外节点（美国/日本/新加坡），上传 YouTube 速度更快。如果只是管理用途（你自己在国内访问后台），选 CN2 线路的海外节点即可，兼顾国内访问速度和 YouTube 上传速度。

### 1.3 购买后获得的信息

购买完成后，供应商会给你以下信息：

```
IP 地址:   123.45.67.89       ← 你的服务器公网 IP
用户名:    root               ← 默认管理员账号
密码:      xxxxxxxx           ← 初始密码（或 SSH 密钥）
```

**请记下这三个信息，后面要用。**

---

## 第二章：VPS 初始安全配置

新买的 VPS 直接暴露在公网上，必须先做安全加固。

### 2.1 首次登录 VPS

在你的 Windows 电脑上打开命令提示符（CMD）：

```cmd
ssh root@123.45.67.89
```

输入供应商给的初始密码（粘贴时屏幕不会显示字符，这是正常的），首次连接会提示是否信任主机，输入 `yes` 回车。

### 2.2 更新系统

登录成功后，首先更新所有软件包：

```bash
apt update && apt upgrade -y
```

这个过程可能需要 1-2 分钟，等待完成。

### 2.3 修改 root 密码

```bash
passwd root
```

输入一个强密码（建议 16 位以上，包含大小写字母、数字、特殊符号），然后再次输入确认。

> **重要**：这个密码请务必保存好，是最后的恢复手段。

### 2.4 配置防火墙（UFW）

只开放需要的端口：

```bash
# 安装防火墙
apt install -y ufw

# 允许 SSH 连接（必须先放行，否则会被锁在外面！）
ufw allow 22/tcp

# 允许 Web 服务端口（直接 IP 访问用）
ufw allow 8080/tcp

# 允许 PostgreSQL 端口（如需远程访问数据库）
ufw allow 5432/tcp

# 启用防火墙
ufw enable
```

提示 `Command may disrupt existing ssh connections. Proceed with operation?` 时输入 `y`。

验证规则：
```bash
ufw status verbose
```

输出应该是：
```
Status: active
Logging: on (low)
Default: deny (incoming), allow (outgoing), disabled (routed)
New profiles: skip

To                         Action      From
--                         ------      ----
22/tcp                     ALLOW IN    Anywhere
8080/tcp                   ALLOW IN    Anywhere
5432/tcp                   ALLOW IN    Anywhere
22/tcp (v6)                ALLOW IN    Anywhere (v6)
8080/tcp (v6)              ALLOW IN    Anywhere (v6)
5432/tcp (v6)              ALLOW IN    Anywhere (v6)
```

> **注意**：不要开放 5432（PostgreSQL）端口！数据库只在 Docker 内部网络通信，不需要对外暴露。我们后面还会修改 `docker-compose.yml` 把端口绑定到 127.0.0.1。

### 2.5 安装 Fail2Ban（防暴力破解）

```bash
apt install -y fail2ban

# 启动并设为开机自启
systemctl enable fail2ban
systemctl start fail2ban

# 查看状态
systemctl status fail2ban
```

Fail2Ban 会自动封禁多次 SSH 登录失败的 IP，防止暴力破解。

### 2.6 配置 SSH 安全策略（可选但推荐）

编辑 SSH 配置文件：

```bash
nano /etc/ssh/sshd_config
```

找到并修改以下行（去掉 `#` 注释并修改值）：

```
# 允许 root 用密码登录
PermitRootLogin yes

# 允许密码登录
PasswordAuthentication yes

# 登录超时时间
ClientAliveInterval 300
ClientAliveCountMax 2
```

> **提示**：本项目使用密码方式连接服务器，请确保 `PasswordAuthentication yes` 和 `PermitRootLogin yes`。
>
> 如果担心安全，可以 later 配置 SSH 密钥并禁用密码登录，但部署脚本需要同时改为密钥方式。

修改后保存（`Ctrl+O` 回车，`Ctrl+X` 退出），然后重启 SSH 服务：

```bash
systemctl restart sshd
```

### 2.7 创建 Swap 分区（内存不足时用）

如果你的 VPS 内存只有 4GB，建议创建 4GB 的 Swap：

```bash
fallocate -l 4G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile

# 永久生效
echo '/swapfile none swap sw 0 0' >> /etc/fstab

# 验证
free -h
```

输出应显示 Swap 总量为 4.0G。

---

## 第三章：安装 Docker 环境

### 3.1 一键安装 Docker

在 VPS 上执行：

```bash
curl -fsSL https://get.docker.com | sh
```

等待安装完成（约 1-3 分钟）。

### 3.2 配置 Docker 国内镜像加速（国内服务器推荐）

如果你的 VPS 在国内或访问 Docker Hub 较慢，配置镜像加速：

```bash
mkdir -p /etc/docker
cat > /etc/docker/daemon.json << 'EOF'
{
  "registry-mirrors": [
    "https://docker.1ms.run",
    "https://docker.xuanyuan.me"
  ],
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "5"
  }
}
EOF
```

重启 Docker 生效：

```bash
systemctl daemon-reload
systemctl restart docker
```

### 3.3 验证安装

```bash
docker --version
# 输出: Docker version 24.x.x

docker-compose --version
# 输出: Docker Compose version v2.x.x

# 运行测试容器
docker run --rm hello-world
```

如果看到 `Hello from Docker!` 的输出，说明 Docker 安装成功。

### 3.4 设置 Docker 开机自启

```bash
systemctl enable docker
```

### 3.5 安装辅助工具

```bash
# git 用于拉取代码
curl -fsSL https://get.docker.com | sh
apt install -y git

# curl 用于健康检查
apt install -y curl

# nano 文本编辑器（如果你习惯 vi 可跳过）
apt install -y nano
```

---

## 第四章：本地 SSH 密码配置

回到你的 Windows 电脑上操作。本项目使用密码方式连接服务器，无需配置 SSH 密钥。

### 4.1 验证 SSH 连接

打开命令提示符（CMD），测试能否通过密码连接到服务器：

```cmd
ssh root@123.45.67.89
```

输入服务器密码，如果成功登录说明 SSH 连接正常。输入 `exit` 退出。

> Windows 10 自带 OpenSSH 客户端，无需额外安装。

### 4.2 安装 PuTTY（可选 — 用于全自动部署）

如果希望部署脚本自动传入密码（无需手动输入），建议安装 PuTTY：

1. 下载 PuTTY：https://www.putty.org/
2. 安装时勾选 `pscp` 和 `plink` 组件（默认已包含）
3. 确认安装后在 CMD 中验证：
   ```cmd
   pscp -V
   plink -V
   ```

> 如果不安装 PuTTY，部署脚本仍可正常工作，只是每次需要手动输入密码（上传和远程执行各一次）。

---

## 第五章：初始化 Git 仓库并推送到 GitHub

在本地 Windows 电脑上操作。

### 5.1 在 GitHub 创建私有仓库

1. 打开 https://github.com/new
2. Repository name: `audiobook-manager`（名称自定）
3. 选择 **Private**（项目含业务代码，建议私有）
4. **不要**勾选任何初始化选项
5. 点击 "Create repository"

### 5.2 初始化本地 Git 仓库

打开命令提示符，切换到项目目录：

```cmd
cd /d h:\2026_main_project\yt_aduio_book_one_to_all

:: 初始化 git
git init
git add -A
git commit -m "initial commit"
git branch -M main
```

### 5.3 添加远程仓库并推送

```cmd
:: 添加远程仓库（替换成你的地址）
git remote add origin https://github.com/YOUR_USER/audiobook-manager.git

:: 推送到 GitHub
git push -u origin main
```

> **认证说明**：GitHub 不支持密码推送，需用 **Personal Access Token**：
> 1. 打开 https://github.com/settings/tokens → Generate new token (classic)
> 2. 勾选 `repo` 权限
> 3. 推送时用 token 代替密码
> 4. 或把 token 嵌入 URL：
>    `git remote set-url origin https://USER:TOKEN@github.com/USER/REPO.git`

> **一键脚本**：也可以用 `scripts\git-deploy.bat "initial commit"` 自动完成

---

## 第六章：服务器首次部署

### 6.1 SSH 登录服务器

```cmd
ssh root@123.45.67.89
```

### 6.2 安装 Git（如果没有）

```bash
apt install -y git
```

### 6.3 克隆仓库并配置环境

```bash
# 克隆仓库
git clone https://github.com/YOUR_USER/audiobook-manager.git /opt/audiobook
cd /opt/audiobook

# 创建 .env 配置
cp .env.example .env
nano .env
```

最简配置（稍后第七章再详细修改）：
```ini
DB_MODE=self
POSTGRES_PASSWORD=临时密码
SECRET_KEY=临时密钥
BASE_URL=http://123.45.67.89:8080
```

### 6.4 执行部署

```bash
bash scripts/git-server-deploy.sh
```

脚本会自动完成：
1. `git pull`（确认代码最新）
2. 检查 `.env` 是否存在
3. 读取 `DB_MODE` 选择正确的 docker-compose 配置
4. 构建镜像（首次约 5-10 分钟）
5. `docker-compose up -d` 启动服务
6. 健康检查（轮询 8080 端口）

输出示例：
```
═══════════════════════════════════════════════════════════
  部署开始 — 2026-07-14 15:30:00
  路径: /opt/audiobook
═══════════════════════════════════════════════════════════
[1/4] git pull...
  当前版本: a1b2c3d
[2/4] 检查 .env...
  .env OK
[3/4] Docker 构建...
  数据库模式: self
  首次部署，需要构建镜像
  正在构建镜像...
  重启服务...
[4/4] 等待服务就绪...
  ✓ 服务就绪

═══════════════════════════════════════════════════════════
  部署完成 — 15:40:00
  访问: http://123.45.67.89:8080
═══════════════════════════════════════════════════════════
```

### 6.5 验证部署

浏览器访问 `http://123.45.67.89:8080`，看到登录页面说明部署成功。

> **注意**：此时使用的是临时密码和密钥，必须继续下一章配置安全的环境变量。

---

## 第七章：配置服务器环境变量

### 7.1 SSH 登录服务器

```cmd
ssh root@123.45.67.89
```

### 7.2 编辑 .env 文件

```bash
cd /opt/audiobook
nano .env
```

修改为以下内容（将 `xxx` 替换为你的实际值）：

```ini
# ═══ 数据库模式 ═══
# self = 使用 Docker 内置 PostgreSQL（默认，零配置）
# external = 连接外部已有的 PostgreSQL
DB_MODE=self

# ═══ 自建数据库密码（DB_MODE=self 时使用）═══
# 设一个强密码（首次设定后不可更改，否则需重建数据库）
POSTGRES_PASSWORD=Xk9$mP2vLqR7nW3z

# ═══ 应用密钥（用于加密 OAuth Token 等敏感数据）═══
# 在服务器上执行 openssl rand -hex 32 生成
SECRET_KEY=（这里粘贴 openssl rand -hex 32 的输出）

# ═══ Web 访问地址 ═══
BASE_URL=http://123.45.67.89:8080

# ═══ Web 界面登录密码 ═══
# 默认 inriynisse，可自行修改
APP_PASSWORD=inriynisse
```

> **使用外部数据库？** 如果 DB_MODE=external，则不需要设置 POSTGRES_PASSWORD，
> 而是设置 `EXTERNAL_DATABASE_URL=postgresql://user:pass@host:5432/audiobook`。
> 详见下方「数据库模式切换」章节。

### 7.3 生成随机密钥

在 SSH 终端中执行：

```bash
openssl rand -hex 32
```

会输出类似 `a1b2c3d4e5f6...`（64 个字符），复制这个字符串，粘贴到 `.env` 文件的 `SECRET_KEY=` 后面。

### 7.4 重启服务使配置生效

```bash
cd /opt/audiobook
# 使用智能部署脚本（自动识别 DB_MODE）
bash scripts/deploy.sh

# 或手动指定 compose 文件：
# 自建数据库
docker-compose -f docker-compose.yml -f docker-compose.self-db.yml up -d
# 外部数据库
docker-compose -f docker-compose.yml -f docker-compose.external-db.yml up -d
```

> **重要**：如果你修改了 `POSTGRES_PASSWORD`，而数据库已经用旧密码初始化过了，密码不会自动更新。需要完全重建数据库：
> ```bash
> docker-compose -f docker-compose.yml -f docker-compose.self-db.yml down -v   # ⚠️ 这会删除所有数据！
> docker-compose -f docker-compose.yml -f docker-compose.self-db.yml up -d
> ```

### 7.5 数据库模式切换

系统支持两种数据库模式，通过 `.env` 中的 `DB_MODE` 控制：

#### 模式一：自建数据库（DB_MODE=self，默认）

使用 Docker 内置 PostgreSQL，零配置，适合首次部署或单机场景。

```ini
DB_MODE=self
POSTGRES_PASSWORD=your_strong_password
```

Docker 会自动启动 PostgreSQL 容器，数据存储在 `postgres_data` 卷中。

#### 模式二：外部数据库（DB_MODE=external）

连接已有的 PostgreSQL 实例（如云数据库 RDS、其他服务器上的 PG 等），
适合多实例共享数据库或已有数据库基础设施的场景。

```ini
DB_MODE=external
EXTERNAL_DATABASE_URL=postgresql://user:pass@your-db-host:5432/audiobook
```

> **注意**：外部数据库需要已执行过 `docker/init-db.sql` 初始化表结构。

#### 切换模式

修改 `.env` 中的 `DB_MODE` 后，重新运行部署脚本即可：

```bash
bash scripts/deploy.sh
# 或低配模式
bash scripts/lowmem-deploy.sh
```

部署脚本会自动读取 `DB_MODE` 并选择正确的 docker-compose 覆盖文件。

### 7.6 数据库端口说明

PostgreSQL 端口在 `docker-compose.self-db.yml` 中配置为 `5432:5432`，自建模式下可从外部访问。
外部数据库模式下不暴露任何 PostgreSQL 端口。

如需从其他机器连接数据库（例如高性能计算节点），使用以下连接信息：

```
主机: 123.45.67.89
端口: 5432
数据库名: audiobook
用户名: audiobook_app
密码: （你在 .env 中设置的 POSTGRES_PASSWORD）
```

---

## 第八章：Web 密码登录与直接访问

本系统为单人使用设计，不依赖 Nginx、域名或 SSL 证书。直接通过浏览器访问 `http://VPS_IP:8080` 即可，系统内置密码登录保护。

### 8.1 直接访问

打开浏览器，访问：

```
http://123.45.67.89:8080
```

会自动跳转到登录页。

### 8.2 输入密码登录

在登录页输入密码（默认 `inriynisse`，可在 `.env` 中通过 `APP_PASSWORD` 修改）：

- 输入正确密码后，点击「登录」
- 系统会设置一个有效期 365 天的 Cookie，下次访问自动跳过登录
- 如需登出，点击右上角「登出」按钮

### 8.3 修改登录密码（可选）

如需修改密码，编辑服务器上的 `.env` 文件：

```bash
cd /opt/audiobook
nano .env
```

修改或添加：

```ini
APP_PASSWORD=your_new_password
```

重启生效：

```bash
docker-compose up -d
```

### 8.4 关于 OAuth 授权

Google OAuth 要求回调地址为 HTTPS 或 `http://localhost`。直接用 IP 访问时，OAuth 自动回调可能不工作。解决方案：

1. **使用手动回调**：在频道授权页面，点击「手动粘贴回调 URL」按钮，将 Google 重定向后的完整 URL 粘贴回来即可完成授权
2. **临时本地转发**：在本地 CMD 执行 `ssh -L 8080:localhost:8080 root@VPS_IP`，然后浏览器访问 `http://localhost:8080` 进行 OAuth 授权（完成后即可关闭）

> OAuth 授权只需做一次，之后 Token 会自动刷新，无需反复操作。

---

## 第九章：配置 Google OAuth 回调地址

系统使用 YouTube OAuth 2.0 进行频道授权，需要正确配置回调地址。

### 9.1 确认回调地址

回调地址的格式为：

```
{BASE_URL}/api/oauth/callback
```

根据你第八章的配置，完整的回调地址为：

```
http://123.45.67.89:8080/api/oauth/callback
```

> **注意**：Google OAuth 对非 localhost 的地址要求 HTTPS。如果直接用 IP 访问，请在 OAuth 回调时使用「手动粘贴回调 URL」功能（见 8.4 节）。

### 9.2 在 Google Cloud Console 配置

1. 打开 https://console.cloud.google.com/
2. 创建项目（或选择已有项目）
3. 左侧菜单 → **API 和服务** → **凭据**
4. 创建 OAuth 2.0 客户端 ID（如果已有则点击编辑）
5. 在 **授权重定向 URI** 中添加：

```
http://123.45.67.89:8080/api/oauth/callback
```

6. 保存

### 9.3 下载 client_secret.json

在凭据页面，点击刚创建的 OAuth 客户端 ID → **下载 JSON**，这个文件稍后通过系统界面上传给每个频道。

### 9.4 测试 OAuth 流程

1. 浏览器访问 `http://123.45.67.89:8080`，输入密码登录
2. 进入频道管理页面
3. 添加频道 → 上传 `client_secret.json`
4. 点击「授权」→ 跳转到 Google 登录页面
5. 授权后自动回调到你的网站

如果回调成功并显示「授权成功」，说明 OAuth 配置正确。

---

## 第十章：GitHub 部署与日常更新

> 这是日常最常用的操作。开发机推送代码到 GitHub，服务器拉取并部署。

### 10.1 整体流程

```
开发机 (Windows)              GitHub                 服务器 (VPS)
┌──────────┐   git push  ┌──────────┐  git pull  ┌──────────────┐
│ 修改代码  │ ──────────→│  私有仓库  │ ──────────→│ git-server   │
│          │             │          │            │ -deploy.sh   │
└──────────┘             └──────────┘            │      ↓       │
                                                  │ docker build │
                                                  │      ↓       │
                                                  │ docker restart│
                                                  └──────────────┘
```

### 10.2 日常更新（最常用）

#### 第一步：开发机推送代码

```cmd
:: 方式一：一键推送（自动 git add + commit + push）
scripts\git-deploy.bat "修复了登录页面bug"

:: 方式二：手动 git 命令
git add -A
git commit -m "修复了登录页面bug"
git push
```

#### 第二步：服务器拉取并部署

```bash
ssh root@你的服务器IP
cd /opt/audiobook
bash scripts/git-server-deploy.sh
```

脚本输出示例（只改了 Python 代码，秒级更新）：
```
[1/4] git pull...
  当前版本: a1b2c3d
[2/4] 检查 .env...
  .env OK
[3/4] Docker 构建...
  数据库模式: self
  依赖未变更，跳过构建
  重启服务...
[4/4] 等待服务就绪...
  ✓ 服务就绪

═══════════════════════════════════════════════════════════
  部署完成 — 15:42:33
  访问: http://123.45.67.89:8080
═══════════════════════════════════════════════════════════
```

**全程约 10-15 秒**。

### 10.3 智能构建机制

`git-server-deploy.sh` 会对比 `requirements.txt` 和 `Dockerfile` 的哈希值，自动判断是否需要重建镜像：

| 你修改了什么 | 服务器行为 | 耗时 |
|------------|----------|------|
| `backend/*.py` | 跳过构建，直接重启 | ~10 秒 |
| `pipeline/*.py` | 跳过构建，直接重启 | ~10 秒 |
| `requirements.txt` | 自动重建镜像 | ~3-5 分钟 |
| `docker/Dockerfile.*` | 自动重建镜像 | ~3-5 分钟 |
| `docker-compose.yml` | 自动重启 | ~15 秒 |
| `.env`（服务器端） | `nano .env` → 再运行部署脚本 | ~15 秒 |

### 10.4 回滚到历史版本

```bash
cd /opt/audiobook

# 查看提交历史
git log --oneline -10

# 回滚到某个版本
git checkout e4f5g6h
bash scripts/git-server-deploy.sh

# 回到最新版本
git checkout main
bash scripts/git-server-deploy.sh
```

---

## 第十一章：手动从 GitHub 下载部署和更新

> 不使用任何脚本，纯手动用 git 和 docker 命令操作。适合理解原理或首次部署。

### 11.1 首次部署（纯手动）

#### 第一步：在服务器上安装 Git 和 Docker

```bash
# 安装 Git
apt update && apt install -y git

# 安装 Docker（如果还没装）
curl -fsSL https://get.docker.com | sh
systemctl enable docker
```

#### 第二步：克隆仓库

```bash
git clone https://github.com/YOUR_USER/audiobook-manager.git /opt/audiobook
cd /opt/audiobook
```

#### 第三步：配置环境变量

```bash
cp .env.example .env
nano .env
```

设置以下关键值：
```ini
DB_MODE=self
POSTGRES_PASSWORD=你的强密码
SECRET_KEY=（执行 openssl rand -hex 32 生成）
BASE_URL=http://你的服务器IP:8080
APP_PASSWORD=你的登录密码
```

#### 第四步：构建并启动

```bash
# 构建镜像（首次约 5-10 分钟）
docker-compose -f docker-compose.yml -f docker-compose.self-db.yml build

# 启动服务
docker-compose -f docker-compose.yml -f docker-compose.self-db.yml up -d
```

#### 第五步：验证

```bash# 查看容器状态
docker-compose -f docker-compose.yml -f docker-compose.self-db.yml ps

# 测试访问
curl -s http://localhost:8080/ | head -5
```

浏览器访问 `http://你的服务器IP:8080`，看到登录页面即成功。

### 11.2 日常更新（纯手动）

```bash
cd /opt/audiobook

# 1. 拉取最新代码
git pull

# 2. 判断是否需要重建镜像
#    改了 requirements.txt 或 Dockerfile？
if ! diff <(md5sum requirements.txt | awk '{print $1}') <(cat .cache_req_hash 2>/dev/null) > /dev/null 2>&1; then
    echo "依赖变了，重建镜像..."
    docker-compose -f docker-compose.yml -f docker-compose.self-db.yml build
    md5sum requirements.txt | awk '{print $1}' > .cache_req_hash
else
    echo "依赖没变，跳过构建"
fi

# 3. 重启服务
docker compose -f docker-compose.yml -f docker-compose.self-db.yml up -d

# 4. 等待就绪
sleep 3
curl -sf http://localhost:8080/ && echo "✓ 服务就绪"
```

> **提示**：日常更新用 `bash scripts/git-server-deploy.sh` 一键完成即可，
> 手动方式主要用于理解原理或排查问题。

### 11.3 回滚（纯手动）

```bash
cd /opt/audiobook

# 查看历史
git log --oneline -10

# 切换到指定版本
git checkout abc1234

# 重启服务
docker-compose -f docker-compose.yml -f docker-compose.self-db.yml up -d

# 回到最新
git checkout main
docker-compose -f docker-compose.yml -f docker-compose.self-db.yml up -d
```

### 11.4 外部数据库模式的手动操作

如果使用外部数据库（`DB_MODE=external`），将 compose 文件改为 `docker-compose.external-db.yml`：

```bash
# 首次部署
docker-compose -f docker-compose.yml -f docker-compose.external-db.yml build
docker-compose -f docker-compose.yml -f docker-compose.external-db.yml up -d

# 日常更新
git pull
docker-compose -f docker-compose.yml -f docker-compose.external-db.yml up -d
```

---

## 第十二章：服务器日常运维

### 12.1 查看服务状态

SSH 登录服务器后：

```bash
cd /opt/audiobook
docker-compose ps
```

正常输出：
```
Name                     Command              State        Ports
─────────────────────────────────────────────────────────────────
audiobook_postgres   docker-entrypoint.sh postgres    Up (healthy)
audiobook_web        uvicorn backend.main:app  ...    Up
```

### 12.2 查看实时日志

```bash
cd /opt/audiobook

# 查看所有服务日志
docker-compose logs -f

# 只看 Web 服务
docker-compose logs -f web

# 只看最近 100 行
docker-compose logs --tail 100 web

# 查看某个时间之后的日志
docker-compose logs --since 30m web
```

### 12.3 快速重启服务

```bash
cd /opt/audiobook

# 重启所有服务
bash scripts/quick-restart.sh

# 仅重启 Web
bash scripts/quick-restart.sh web
```

### 12.4 进入容器调试

```bash
# 进入 Web 容器
docker exec -it audiobook_web bash

# 进入 PostgreSQL 交互式终端
docker exec -it audiobook_postgres psql -U audiobook_app -d audiobook
```

### 12.5 查看磁盘空间

```bash
# 总体磁盘使用
df -h

# Docker 占用空间
docker system df

# 清理无用的镜像和容器（不影响运行中的服务）
docker system prune -f

# 清理无用的数据卷（谨慎！先确认没有重要数据）
# docker volume prune
```

### 12.6 查看内存和 CPU

```bash
# 实时资源使用
htop
# 如果没安装：apt install -y htop

# 快速查看
free -h          # 内存
nproc            # CPU 核数
uptime           # 负载
```

### 12.7 修改服务器配置

```bash
cd /opt/audiobook
nano .env
# 修改后重启
docker-compose up -d
```

### 12.8 完全停止和启动

```bash
cd /opt/audiobook

# 停止所有服务（数据保留）
docker-compose down

# 启动所有服务
docker-compose up -d

# 停止并删除数据（⚠️ 谨慎！会丢失所有数据）
# docker-compose down -v
```

### 12.9 更新代码后重启服务

```bash
cd /opt/audiobook
docker-compose restart web
```

---

## 第十三章：数据备份与恢复

### 13.1 手动备份数据库

```bash
cd /opt/audiobook

# 创建备份目录
mkdir -p /opt/backups

# 备份 PostgreSQL 数据库
docker exec audiobook_postgres pg_dump -U audiobook_app audiobook > /opt/backups/db_$(date +%Y%m%d_%H%M%S).sql

# 查看备份文件
ls -lh /opt/backups/
```

### 13.2 自动定时备份

创建备份脚本：

```bash
nano /opt/audiobook/scripts/backup.sh
```

写入：

```bash
#!/usr/bin/env bash
BACKUP_DIR="/opt/backups"
mkdir -p "${BACKUP_DIR}"

cd /opt/audiobook

# 备份数据库
docker exec audiobook_postgres pg_dump -U audiobook_app audiobook > "${BACKUP_DIR}/db_$(date +%Y%m%d_%H%M%S).sql"

# 删除 7 天前的备份
find "${BACKUP_DIR}" -name "db_*.sql" -mtime +7 -delete

echo "备份完成: $(ls -ht ${BACKUP_DIR}/db_*.sql | head -1)"
```

设置定时任务（每天凌晨 3 点自动备份）：

```bash
chmod +x /opt/audiobook/scripts/backup.sh

# 编辑定时任务
crontab -e
```

在文件末尾添加：

```
0 3 * * * /opt/audiobook/scripts/backup.sh >> /opt/backups/backup.log 2>&1
```

保存退出。验证：

```bash
# 手动执行一次测试
bash /opt/audiobook/scripts/backup.sh

# 查看备份
ls -lh /opt/backups/
```

### 13.3 恢复数据库

```bash
cd /opt/audiobook

# 停止 Web（避免写入冲突）
docker-compose stop web

# 恢复数据库
docker exec -i audiobook_postgres psql -U audiobook_app -d audiobook < /opt/backups/db_20260712_030000.sql

# 重启服务
docker-compose start web
```

### 13.4 备份输出文件

如果你需要备份 Web 容器生成的音频/视频文件：

```bash
# 备份输出目录
tar czf /opt/backups/output_$(date +%Y%m%d).tar.gz -C /var/lib/docker/volumes/audiobook_output_data/_data .

# 备份音乐库
tar czf /opt/backups/music_$(date +%Y%m%d).tar.gz -C /var/lib/docker/volumes/audiobook_music_data/_data .
```

### 13.5 下载备份到本地

在你的 Windows 电脑上：

```cmd
scp root@123.45.67.89:/opt/backups/db_20260712_030000.sql D:\backups\
```

---

## 第十四章：故障排查

### 14.1 服务无法启动

```bash
cd /opt/audiobook

# 查看服务日志
docker-compose logs web
docker-compose logs postgres

# 检查端口占用
ss -tlnp | grep -E '8080|5432'
```

### 14.2 Web 服务无法访问

浏览器访问 `http://123.45.67.89:8080` 无响应：

```bash
# 检查 Web 容器状态
docker-compose ps web

# 查看 Web 日志
docker-compose logs --tail 50 web

# 常见原因：
# 1. .env 中 POSTGRES_PASSWORD 与数据库实际密码不一致
# 2. 数据库未初始化完成
# 3. Python 代码有语法错误
# 4. 防火墙未放行 8080 端口（检查 ufw status）
```

### 14.3 数据库连接失败

```bash
# 检查 PostgreSQL 是否运行
docker-compose ps postgres
# 状态应为 Up (healthy)

# 手动测试连接
docker exec audiobook_postgres psql -U audiobook_app -d audiobook -c "SELECT 1;"
# 应输出 1

# 如果报密码错误，说明 .env 中的 POSTGRES_PASSWORD 与创建时不一致
# 解决方案：重置数据库密码
docker exec audiobook_postgres psql -U audiobook_app -d audiobook -c "ALTER USER audiobook_app PASSWORD '新密码';"
# 然后更新 .env 中的 POSTGRES_PASSWORD
```

### 14.4 任务不执行

```bash
# 查看 Web 日志（任务在 Web 容器的后台线程中执行）
docker-compose logs --tail 100 web

# 检查数据库中的任务状态
docker exec audiobook_postgres psql -U audiobook_app -d audiobook -c "SELECT task_id, channel_name, status, created_at FROM run_tasks ORDER BY created_at DESC LIMIT 10;"

# 如果任务状态为 failed，查看错误信息
docker exec audiobook_postgres psql -U audiobook_app -d audiobook -c "SELECT task_id, error_msg FROM run_tasks WHERE status = 'failed' ORDER BY created_at DESC LIMIT 5;"
```

### 14.5 OAuth 回调失败

```
错误信息: redirect_uri_mismatch
```

**原因**：Google Cloud Console 中配置的回调地址与 `BASE_URL` 不一致。

**检查步骤**：

1. 确认 `.env` 中的 `BASE_URL`：
   ```bash
   cd /opt/audiobook
   grep BASE_URL .env
   ```

2. 确认 Google Cloud Console 中的授权重定向 URI 为：
   ```
   {BASE_URL}/api/oauth/callback
   ```
   例如 `http://123.45.67.89:8080/api/oauth/callback`

3. 两者必须完全一致（包括 http/https、端口、末尾无斜杠）

### 14.6 磁盘空间不足

```bash
# 查看磁盘使用
df -h

# 查看 Docker 占用
docker system df

# 清理无用的镜像、容器、构建缓存
docker system prune -a -f

# 清理旧日志
docker-compose logs --tail 0 -f &  # 不实际操作，只是说明

# 清理旧的备份文件
find /opt/backups -name "*.sql" -mtime +7 -delete
find /opt/backups -name "*.tar.gz" -mtime +7 -delete

# 清理 Docker 日志文件
truncate -s 0 /var/lib/docker/containers/*/*-json.log
```

### 14.7 内存不足（OOM）

```bash
# 查看内存使用
free -h

# 查看各容器内存使用
docker stats --no-stream

# 如果内存不足，可以考虑增加 VPS 内存或减少同时运行的任务数
```

### 14.8 服务器 git pull 失败

SSH 登录服务器后排查：

```bash
cd /opt/audiobook

# 手动 git pull 查看错误
git pull

# 如果认证失败，配置 token 认证
git remote set-url origin https://USER:TOKEN@github.com/USER/REPO.git
git pull

# 检查网络
ping github.com
```

**常见原因**：
1. 仓库为 Private，服务器没有配置认证
2. Token 已过期
3. 网络问题

### 14.9 完全重装（最后的手段）

```bash
# SSH 登录服务器
cd /opt/audiobook

# 停止所有服务
docker-compose down

# 删除所有数据（⚠️ 确保已备份！）
docker-compose down -v

# 删除项目目录
cd /
rm -rf /opt/audiobook

# 从本地重新部署
```

然后在本地执行：
```cmd
scripts\git-deploy.bat "重新部署"
```
然后服务器执行：
```bash
cd /opt/audiobook && bash scripts/git-server-deploy.sh
```

---

## 附录 A：完整命令速查表

### 本地操作（Windows CMD）

```cmd
:: ─── 日常开发 ───
scripts\dev.bat                    :: 启动本地开发环境（热重载）

:: ─── 部署到 GitHub ───
scripts\git-deploy.bat "提交信息"  :: git add + commit + push to GitHub

:: ─── 依赖重建 ───
scripts\rebuild-deps.bat           :: 重建 Docker 镜像

:: ─── SSH 登录 ───
ssh root@123.45.67.89              :: 登录 VPS

:: ─── 下载备份 ───
scp root@123.45.67.89:/opt/backups/db_xxx.sql D:\backups\
```

### VPS 操作（SSH 终端）

> **注意**：项目现在使用分层 docker-compose 配置。
> 推荐使用 `bash scripts/deploy.sh` 自动识别数据库模式。
> 如需手动操作，请根据 DB_MODE 选择 compose 文件：
> - 自建数据库：`docker-compose -f docker-compose.yml -f docker-compose.self-db.yml <命令>`
> - 外部数据库：`docker-compose -f docker-compose.yml -f docker-compose.external-db.yml <命令>`

```bash
# ─── 服务管理 ───
cd /opt/audiobook
bash scripts/deploy.sh              # 智能部署（推荐）
docker-compose ps                   # 查看服务状态（需加 -f 参数）
docker-compose up -d                # 启动/重启服务（需加 -f 参数）
docker-compose down                 # 停止所有服务（需加 -f 参数）
bash scripts/quick-restart.sh       # 快速重启
bash scripts/quick-restart.sh web   # 仅重启 Web

# ─── 日志查看 ───
docker-compose logs -f web          # Web 实时日志（需加 -f 参数）
docker-compose logs --tail 100 web  # 最近 100 行（需加 -f 参数）

# ─── 容器调试 ───
docker exec -it audiobook_web bash          # 进入 Web 容器
docker exec -it audiobook_postgres psql -U audiobook_app -d audiobook  # 数据库（仅自建模式）

# ─── 配置修改 ───
nano .env                           # 编辑环境变量（含 DB_MODE）
nano docker-compose.self-db.yml     # 编辑自建数据库配置
nano docker-compose.external-db.yml # 编辑外部数据库配置
bash scripts/deploy.sh              # 使配置生效（推荐）

# ─── 数据备份 ───
bash scripts/backup.sh              # 手动备份
ls -lh /opt/backups/                # 查看备份列表

# ─── 系统监控 ───
df -h                               # 磁盘空间
free -h                             # 内存使用
docker stats                        # 容器资源使用
docker system df                    # Docker 空间

# ─── 清理 ───
docker system prune -f              # 清理无用镜像/容器
```

---

## 附录 B：架构图

### 部署架构总览

```
                          ┌─────────────────────┐
                          │   你的 Windows 电脑   │
                          │                     │
                          │  代码编辑器 (IDE)    │
                          │  scripts\dev.bat    │
                          │  scripts\git-       │
                          │    deploy.bat       │
                          │  浏览器 (8080)       │
                          └──────────┬──────────┘
                                     │ HTTP (8080)
                                     │ 密码登录保护
                                     ▼
    ┌──────────────────────────────────────────────────────┐
    │                   VPS (123.45.67.89)                  │
    │                                                      │
    │  ┌──────────────────────────────────────────────┐   │
    │  │           Docker Compose 网络                  │   │
    │  │                                               │   │
    │  │  ┌──────────────────┐  ┌─────────┐           │   │
    │  │  │   Web API + 后台   │  │PostgreSQL│          │   │
    │  │  │   线程任务执行       │  │  :5432  │          │   │
    │  │  │   FastAPI :8080  │  │         │           │   │
    │  │  └────────┬─────────┘  └─────────┘           │   │
    │  │           │                                   │   │
    │  │  ┌───────────────────────────────────────┐  │   │
    │  │  │ Docker Volumes                        │  │   │
    │  │  │  postgres_data  output_data           │  │   │
    │  │  │  music_data                            │  │   │
    │  │  └───────────────────────────────────────┘  │   │
    │  └───────────────────────────────────────────────┘   │
    │                                                      │
    │  ┌─────────────┐  ┌──────────────┐                  │
    │  │  Fail2Ban   │  │  UFW 防火墙   │                  │
    │  │  防暴力破解  │  │ 22/8080/5432 │                  │
    │  └─────────────┘  └──────────────┘                  │
    │                                                      │
    │  ┌─────────────────────────────────────────────┐    │
    │  │  /opt/audiobook/                            │    │
    │  │    .env  docker-compose.yml  backend/  ...  │    │
    │  │  /opt/backups/  (定时备份)                    │    │
    │  └─────────────────────────────────────────────┘    │
    └──────────────────────────────────────────────────────┘
                              │
                              │ HTTP (http://123.45.67.89:8080)
                              ▼
                    ┌─────────────────┐
                    │  你的浏览器       │
                    │  (密码登录)      │
                    └─────────────────┘
```

### 部署流程时序图

```
 本地 Windows                     GitHub                      VPS
 ────────────                    ────────                    ─────
      │                              │                           │
      │  scripts\git-deploy.bat     │                           │
      │  git add + commit + push    │                           │
      │  ──────────────────────────► │                           │
      │                              │                           │
      │                              │  git pull                 │
      │                              │  ──────────────────────► │
      │                              │                           │ bash git-server-deploy.sh
      │                              │                           │  a. git pull
      │                              │                           │  b. 检查 .env
      │                              │                           │  c. MD5 对比依赖
      │                              │                           │  d. 需要时重建镜像
      │                              │                           │  e. docker-compose up -d
      │                              │                           │  f. 健康检查
      │                              │                           │
```

---

## 首次部署 Checklist

按顺序逐项确认：

- [ ] **第一章**：已购买 VPS，获得 IP、用户名、密码
- [ ] **第二章**：SSH 登录、更新系统、配置防火墙、安装 Fail2Ban、创建 Swap
- [ ] **第三章**：安装 Docker、配置镜像加速、验证 `docker run hello-world`
- [ ] **第四章**：验证 SSH 密码连接、（可选）安装 PuTTY
- [ ] **第五章**：GitHub 创建仓库、`git init` + `git push` 推送代码
- [ ] **第六章**：服务器 `git clone` + 配置 `.env` + `bash scripts/git-server-deploy.sh`、浏览器访问验证
- [ ] **第七章**：配置 `.env`（密码、密钥、BASE_URL）、限制端口、重启服务
- [ ] **第八章**：浏览器访问 IP:8080、输入密码登录、测试 OAuth 手动回调
- [ ] **第九章**：Google Cloud Console 配置 OAuth 回调地址、测试授权流程
- [ ] **第十三章**：创建定时备份任务、手动备份测试

全部打勾后，你的 VPS 部署就完整了！🎉
