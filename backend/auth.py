"""Web 密码认证 — 基于 Cookie 的简单登录保护。

登录密码通过环境变量 APP_PASSWORD 配置（默认 inriynisse）。
登录成功后设置签名 Cookie（有效期 365 天），下次自动跳过登录。
"""

from __future__ import annotations

import hmac
import hashlib
import base64
import time
import json
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, JSONResponse

from .settings import settings

logger = logging.getLogger(__name__)

# Cookie 名称
COOKIE_NAME = "audiobook_auth"
# Cookie 有效期（秒）— 365 天
COOKIE_MAX_AGE = 365 * 24 * 3600

# 不需要认证的路径前缀
PUBLIC_PATHS = (
    "/login",
    "/api/oauth/callback",
    "/oauth/success",
    "/oauth/error",
    "/api/docs",
    "/api/redoc",
    "/openapi.json",
    "/api/system/health",
)


def _sign(payload: str) -> str:
    """用 SECRET_KEY 对 payload 做 HMAC-SHA256 签名。"""
    key = settings.secret_key.encode("utf-8")
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def create_auth_cookie_value() -> str:
    """生成签名 Cookie 值：base64(payload).signature"""
    payload = json.dumps({"t": int(time.time())}, separators=(",", ":"))
    payload_b64 = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("utf-8")
    sig = _sign(payload_b64)
    return f"{payload_b64}.{sig}"


def verify_auth_cookie(cookie_value: str) -> bool:
    """验证签名 Cookie 是否有效。"""
    if not cookie_value or "." not in cookie_value:
        return False
    parts = cookie_value.split(".", 1)
    if len(parts) != 2:
        return False
    payload_b64, sig = parts
    expected_sig = _sign(payload_b64)
    if not hmac.compare_digest(sig, expected_sig):
        return False
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("utf-8")))
        issued_at = payload.get("t", 0)
        # 检查是否过期
        if time.time() - issued_at > COOKIE_MAX_AGE:
            return False
        return True
    except Exception:
        return False


def is_authenticated(request: Request) -> bool:
    """检查请求是否已认证。"""
    cookie = request.cookies.get(COOKIE_NAME)
    return verify_auth_cookie(cookie) if cookie else False


def _is_public(path: str) -> bool:
    """判断路径是否不需要认证。"""
    for prefix in PUBLIC_PATHS:
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?"):
            return True
    return False


class AuthMiddleware(BaseHTTPMiddleware):
    """认证中间件 — 未登录的请求重定向到 /login。"""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # 公开路径直接放行
        if _is_public(path):
            return await call_next(request)

        # 已认证放行
        if is_authenticated(request):
            return await call_next(request)

        # API 请求返回 401 JSON
        if path.startswith("/api/"):
            return JSONResponse(
                status_code=401,
                content={"detail": "未登录或登录已过期"},
            )

        # 页面请求重定向到登录页
        return RedirectResponse(url="/login", status_code=302)
