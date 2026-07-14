"""FastAPI 应用入口 — 使用 Jinja2 服务端渲染（替代 Vue）。"""

from __future__ import annotations

import os
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .settings import settings as app_settings, get_db_mode
from .auth import AuthMiddleware, COOKIE_NAME, COOKIE_MAX_AGE, create_auth_cookie_value
from .api import channels, oauth, tasks, books, config, settings as system_api

# ─── 日志配置 ───
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─── 创建 FastAPI 应用 ───

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理 — 替代已废弃的 @app.on_event 装饰器。"""
    # ── 启动 ──
    db_mode = get_db_mode()
    logger.info("应用启动中...")
    logger.info(f"数据库模式: {db_mode}")
    os.makedirs(app_settings.output_root, exist_ok=True)
    os.makedirs(app_settings.music_dir, exist_ok=True)
    logger.info(f"输出目录: {app_settings.output_root}")
    logger.info(f"音乐目录: {app_settings.music_dir}")
    logger.info(f"基础 URL: {app_settings.base_url}")

    # 启动时执行一次清理
    try:
        from .services.task_service import cleanup_old_tasks
        cleanup_old_tasks()
        logger.info("启动清理完成")
    except Exception as e:
        logger.warning(f"启动清理失败（非致命）: {e}")

    yield

    # ── 关闭 ──
    # 关闭 backend 数据库连接池
    try:
        from .database import close_pool
        close_pool()
    except Exception:
        pass
    # 关闭 pipeline 数据库连接池
    try:
        from pipeline.db import close_pool as close_pipeline_pool
        close_pipeline_pool()
    except Exception:
        pass
    logger.info("应用关闭")


app = FastAPI(
    title="有声书 YouTube 频道管理系统",
    description="多频道管理、视频上传、配置管理",
    version="2.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    lifespan=lifespan,
)

# ─── 认证中间件 ───
app.add_middleware(AuthMiddleware)

# ─── 注册 API 路由 ───
app.include_router(channels.router)
app.include_router(oauth.router)
app.include_router(tasks.router)
app.include_router(books.router)
app.include_router(config.router)
app.include_router(system_api.router)

# ─── Jinja2 模板 ───
templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))


# ═══════════════════════════════════════════════════
# 登录 / 登出
# ═══════════════════════════════════════════════════

@app.get("/login", response_class=HTMLResponse)
async def page_login(request: Request):
    """登录页。"""
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
async def do_login(request: Request, password: str = Form(...)):
    """处理登录表单提交。"""
    if password == app_settings.app_password:
        resp = RedirectResponse(url="/", status_code=302)
        resp.set_cookie(
            key=COOKIE_NAME,
            value=create_auth_cookie_value(),
            max_age=COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
        )
        logger.info("用户登录成功")
        return resp

    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": "密码错误，请重试",
    })


@app.get("/logout")
async def do_logout():
    """登出 — 清除 Cookie。"""
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ═══════════════════════════════════════════════════
# 页面路由（服务端渲染）
# ═══════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def page_dashboard(request: Request):
    """仪表盘。"""
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/channels", response_class=HTMLResponse)
async def page_channels(request: Request):
    """频道列表。"""
    return templates.TemplateResponse("channels.html", {"request": request})


@app.get("/channels/{channel_name}", response_class=HTMLResponse)
async def page_channel_detail(request: Request, channel_name: str):
    """频道详情。"""
    return templates.TemplateResponse("channel_detail.html", {
        "request": request, "channel_name": channel_name,
    })


@app.get("/tasks", response_class=HTMLResponse)
async def page_tasks(request: Request):
    """任务列表。"""
    return templates.TemplateResponse("tasks.html", {"request": request})


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
async def page_task_detail(request: Request, task_id: str):
    """任务详情（含实时日志轮询）。"""
    return templates.TemplateResponse("task_detail.html", {
        "request": request, "task_id": task_id,
    })


@app.get("/books", response_class=HTMLResponse)
async def page_books(request: Request):
    """书籍列表。"""
    return templates.TemplateResponse("books.html", {"request": request})


@app.get("/settings", response_class=HTMLResponse)
async def page_settings(request: Request):
    """全局设置。"""
    return templates.TemplateResponse("settings.html", {"request": request})


@app.get("/oauth/success", response_class=HTMLResponse)
async def page_oauth_success(request: Request, channel: str = ""):
    """OAuth 授权成功页。"""
    return templates.TemplateResponse("oauth_result.html", {
        "request": request, "success": True, "channel": channel,
    })


@app.get("/oauth/error", response_class=HTMLResponse)
async def page_oauth_error(request: Request, message: str = ""):
    """OAuth 授权失败页。"""
    return templates.TemplateResponse("oauth_result.html", {
        "request": request, "success": False, "message": message,
    })


