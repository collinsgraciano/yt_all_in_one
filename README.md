# 有声书 YouTube 频道统一管理系统

多频道管理、视频自动上传、配置可视化编辑、一键 Docker 部署。

## 技术栈

| 层级 | 技术 |
|------|------|
| Web 框架 | FastAPI + Uvicorn |
| 数据库 | PostgreSQL 15 |
| 前端 | Jinja2 服务端渲染（无前端构建步骤） |
| 任务执行 | Python `threading.Thread`（后台线程） |
| 容器 | Docker Compose |

> **架构说明**：项目已精简为 **Python + PostgreSQL** 两个核心组件。不使用 Redis、Celery 或 Vue.js。任务通过 Python 后台线程执行，前端通过 Jinja2 模板服务端渲染，日志通过 API 轮询获取。

## 项目结构

```
有声书yt频道统一管理网页程序/
├── docker-compose.yml              # 生产环境编排配置
├── docker-compose.dev.yml          # 开发环境覆盖配置（热重载）
├── requirements.txt                # Python 依赖
├── .env.example                    # 环境变量模板
│
├── docker/
│   ├── Dockerfile.web              # Web 服务镜像（Python 环境）
│   └── init-db.sql                 # 数据库初始化脚本
│
├── backend/
│   ├── main.py                     # FastAPI 入口（含页面路由、登录路由）
│   ├── auth.py                     # 密码认证中间件（Cookie 登录保护）
│   ├── settings.py                 # 应用配置（pydantic-settings）
│   ├── database.py                 # 数据库连接工具
│   ├── log_interceptor.py          # 日志拦截器（写入 PostgreSQL）
│   ├── config_schema.py            # 95+ 参数 Schema
│   ├── api/                        # API 路由
│   │   ├── channels.py             #   频道管理 API
│   │   ├── oauth.py                #   OAuth 认证 API
│   │   ├── tasks.py                #   任务管理 API
│   │   ├── books.py                #   书籍管理 API
│   │   ├── config.py               #   配置管理 API
│   │   └── settings.py             #   系统信息 API
│   ├── models/                     # 数据模型
│   ├── services/                   # 业务逻辑层
│   │   ├── channel_service.py
│   │   ├── oauth_service.py        #   OAuth State 存于 PostgreSQL
│   │   ├── task_service.py         #   任务线程管理 + 停止标志
│   │   ├── config_service.py
│   │   └── log_service.py
│   └── templates/                  # Jinja2 HTML 模板
│       ├── base.html
│       ├── login.html
│       ├── dashboard.html
│       ├── channels.html
│       ├── channel_detail.html
│       ├── tasks.html
│       ├── task_detail.html
│       ├── books.html
│       ├── settings.html
│       └── oauth_result.html
│
├── pipeline/                        # Pipeline 核心包（Docker 挂载到 /app/pipeline）
│   ├── pipeline.py
│   ├── config.py
│   ├── db.py
│   ├── youtube.py
│   ├── audio.py
│   └── ...
│
└── scripts/
    ├── dev.bat                     # 本地开发一键启动
    ├── git-deploy.bat               # 一键推送到 GitHub（推荐）
    ├── git-server-deploy.sh         # 服务器端 git pull + 部署脚本
    ├── rebuild-deps.bat            # 依赖变更时重建镜像
    └── quick-restart.sh            # 服务器端快速重启
```

## 快速部署

### 方式一：GitHub 部署（推荐）

```bash
# 1. 服务器克隆仓库
git clone https://github.com/YOUR_USER/audiobook-manager.git /opt/audiobook
cd /opt/audiobook

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，设置 POSTGRES_PASSWORD, SECRET_KEY, BASE_URL

# 3. 首次部署
bash scripts/git-server-deploy.sh
```

日常更新只需两步：
```cmd
:: 开发机推送
scripts\git-deploy.bat "提交信息"
```
```bash
# 服务器拉取并部署
cd /opt/audiobook && bash scripts/git-server-deploy.sh
```

### 方式二：本地 Docker 直接启动

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑 .env，设置密码和密钥
# POSTGRES_PASSWORD=your_strong_password
# SECRET_KEY=your_64_char_secret_key
# BASE_URL=http://your-domain:8080
```

### 2. 一键启动

```bash
docker-compose up -d --build
```

启动后会创建以下服务：

| 服务 | 端口 | 说明 |
|------|------|------|
| `web` | 8080 | FastAPI Web 服务（含后台线程任务执行） |
| `postgres` | 5432 | PostgreSQL 数据库 |

### 3. 访问系统

浏览器打开 `http://localhost:8080` 即可使用（首次访问需输入密码，默认 `inriynisse`）。

API 文档：`http://localhost:8080/api/docs`

## 使用流程

### 第一步：添加频道

1. 进入「频道管理」页面
2. 点击「添加频道」
3. 填写频道名、显示名
4. 上传 Google Cloud Console 下载的 `client_secret.json`
5. 点击「创建」

### 第二步：OAuth 授权

1. 在频道列表点击「授权」按钮
2. 点击「打开 Google 授权页面」，在新窗口完成 Google 登录授权
3. 授权后自动回调到系统

### 第三步：配置参数

1. 进入频道详情 → 「运行配置」标签页
2. 修改 95+ 参数（下载、降噪、BGM、封面、上传、Podcast 等）
3. 点击「保存配置」

### 第四步：运行 Pipeline

1. 仪表盘或任务管理页点击「运行 Pipeline」
2. 选择一个或多个频道
3. 选择任务类型（完整流程/仅处理/仅上传）
4. 点击「开始运行」

### 第五步：查看日志

1. 任务列表点击「日志」按钮
2. 页面通过 API 轮询实时获取日志
3. 历史日志持久化在 PostgreSQL 中

## 数据库表

| 表名 | 说明 |
|------|------|
| `channels` | 频道注册表（channel_name, display_name, oauth_status...） |
| `channel_configs` | 频道完整运行配置（config_json, config_version） |
| `channel_runtime_settings` | 频道级运行时设置键值对 |
| `youtube_credentials` | YouTube OAuth Token |
| `modelscope_tokens` | AI 生图 Token |
| `books` | 书籍库 |
| `book_processing_states` | 断点续跑状态 |
| `task_queue` | 原始任务队列 |
| `run_tasks` | Web 管理层任务记录（含 stop_requested 停止标志） |
| `run_task_logs` | 任务日志（持久化） |
| `oauth_states` | OAuth State 临时存储（替代 Redis） |
| `global_settings` | 全局共享设置 |

## 开发模式

### 一键启动开发环境

```cmd
scripts\dev.bat
```

开发环境使用 Docker Compose 热重载，修改 `backend/` 下的 Python 代码后自动重载。

### 修改代码后部署到服务器

```cmd
:: GitHub 方式（推荐）
scripts\git-deploy.bat "提交信息"
:: 然后在服务器执行：bash scripts/git-server-deploy.sh
```

## 常见问题

### Q: OAuth 回调失败？

检查 `.env` 中的 `BASE_URL` 是否与 Google Cloud Console 中配置的授权重定向 URI 一致。回调地址格式为 `{BASE_URL}/api/oauth/callback`。

### Q: 如何刷新过期的 YouTube Token？

在频道列表点击「刷新Token」按钮，系统会使用 refresh_token 自动获取新 Token。

### Q: 日志存储在哪里？

- **实时**：前端通过 API 轮询 `/api/tasks/{task_id}/logs/recent` 获取最新日志
- **持久化**：PostgreSQL `run_task_logs` 表

### Q: 如何停止运行中的任务？

在任务管理或日志页面点击「停止」按钮，系统会：
1. 在 PostgreSQL `run_tasks` 表中设置 `stop_requested = true`
2. Pipeline 在下一个检查点检测到标志后优雅退出

### Q: 为什么不使用 Redis 和 Celery？

项目已精简架构，任务量不大时 Python 后台线程完全够用，减少部署复杂度和维护成本。

## License

Private
