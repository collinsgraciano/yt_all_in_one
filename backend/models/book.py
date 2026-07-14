"""书籍相关 Pydantic 模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Any
from pydantic import BaseModel, Field


class BookResponse(BaseModel):
    book_id: str
    book_name: Optional[str] = None
    author: Optional[str] = None
    category: Optional[str] = None
    total_chapters: Optional[int] = None
    tags: list[str] = []
    status: str = ""
    note: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class BookCreate(BaseModel):
    book_id: str
    book_name: str
    author: Optional[str] = None
    category: Optional[str] = None
    total_chapters: Optional[int] = None
    book_data: Optional[dict] = None
    tags: list[str] = []
    note: Optional[str] = None


class BookUpdate(BaseModel):
    book_name: Optional[str] = None
    author: Optional[str] = None
    category: Optional[str] = None
    total_chapters: Optional[int] = None
    book_data: Optional[dict] = None
    tags: Optional[list[str]] = None
    note: Optional[str] = None


class BookTagsUpdate(BaseModel):
    tags: list[str]
