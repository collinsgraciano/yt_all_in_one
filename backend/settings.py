"""应用配置 — 通过环境变量注入。

支持两种数据库模式：
  - DB_MODE=self      → 使用 Docker 内置 PostgreSQL
  - DB_MODE=external  → 连接外部已有的 PostgreSQL 实例

模式由 .env 文件中的 DB_MODE 控制，
docker-compose 会根据模式选择不同的覆盖文件，
最终都通过 DATABASE_URL 环境变量注入到应用中。
"""

from __future__ import annotations

import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """全局应用配置。"""

    # ─── 数据库模式 ───
    # self = Docker 内置 PostgreSQL，external = 外部 PostgreSQL
    db_mode: str = "self"

    # ─── 数据库连接 ───
    # 自建模式下由 docker-compose.self-db.yml 自动设置
    # 外部模式下由 docker-compose.external-db.yml 从 EXTERNAL_DATABASE_URL 注入
    database_url: str = "postgresql://audiobook_app:changeme@localhost:5432/audiobook"
    external_database_url: str = ""

    # ─── 自建数据库密码 ───
    postgres_password: str = "changeme_strong_password"

    # ─── Web 服务 ───
    secret_key: str = "dev_secret_key_change_in_production"
    base_url: str = "http://localhost:8080"
    app_password: str = "inriynisse"

    # ─── 文件路径 ───
    output_root: str = "/data/output"
    music_dir: str = "/data/music"

    # ─── YouTube OAuth ───
    youtube_scopes: str = "https://www.googleapis.com/auth/youtube"
    oauth_state_ttl_seconds: int = 600

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()


# ─── 辅助：获取当前数据库模式 ───
def get_db_mode() -> str:
    """返回当前数据库模式（self 或 external）。"""
    mode = os.environ.get("DB_MODE", settings.db_mode).strip().lower()
    if mode not in ("self", "external"):
        mode = "self"
    return mode


# ─── 辅助：将 DATABASE_URL 转换为 psycopg 原生 DSN ───
def get_dsn() -> str:
    """返回 PostgreSQL DSN 连接串。

    优先级：
    1. 环境变量 DATABASE_URL（由 docker-compose 覆盖文件注入）
    2. 外部数据库模式：settings.external_database_url
    3. 默认本地连接串
    """
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        # 环境变量未设置时，根据模式回退
        if get_db_mode() == "external" and settings.external_database_url:
            dsn = settings.external_database_url
        else:
            dsn = settings.database_url

    # 兼容 postgresql+psycopg:// 前缀
    if dsn.startswith("postgresql+psycopg://"):
        dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
    return dsn


# ─── 辅助：是否使用自建数据库 ───
def is_self_db() -> bool:
    """当前是否使用 Docker 内置 PostgreSQL。"""
    return get_db_mode() == "self"


# ─── 辅助：是否使用外部数据库 ───
def is_external_db() -> bool:
    """当前是否使用外部 PostgreSQL。"""
    return get_db_mode() == "external"
