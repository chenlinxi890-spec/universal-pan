"""通用网盘 - 数据模型与任务存储。"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ShareFile:
    fid: str
    name: str
    size: int = 0
    is_dir: bool = False
    # 目录导航用：百度为 path，夸克为 pdir_fid
    path: str = ""
    # 下载取链用的令牌（如夸克 share_fid_token）
    fid_token: str = ""
    modify_time: str = ""

    def to_dict(self) -> dict:
        return {
            "fid": self.fid,
            "name": self.name,
            "size": self.size,
            "is_dir": self.is_dir,
            "path": self.path,
            "fid_token": self.fid_token,
            "modify_time": self.modify_time,
        }


@dataclass
class ShareInfo:
    platform: str
    platform_name: str
    share_id: str
    title: str = ""
    files: list[ShareFile] = field(default_factory=list)


class ProviderError(Exception):
    """提供方返回的明确错误（链接失效、提取码错误、需要登录等）。"""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class Job:
    id: str
    task_id: str
    kind: str  # download / extract
    status: str = "pending"  # pending / running / done / failed
    message: str = ""
    progress: float = 0.0  # 0..1
    total_bytes: int = 0
    done_bytes: int = 0
    current_file: str = ""
    result: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "kind": self.kind,
            "status": self.status,
            "message": self.message,
            "progress": round(self.progress, 4),
            "total_bytes": self.total_bytes,
            "done_bytes": self.done_bytes,
            "current_file": self.current_file,
            "result": self.result,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class Task:
    id: str
    platform: str
    platform_name: str
    share_id: str
    pwd: Optional[str]
    url: str
    title: str = ""
    status: str = "ready"  # ready / failed
    error: str = ""
    files: list[ShareFile] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    # 提供方会话数据（sekey/stoken/cookie 等，不对外暴露）
    session: dict = field(default_factory=dict)

    def to_dict(self, include_files: bool = True) -> dict:
        d: dict[str, Any] = {
            "id": self.id,
            "platform": self.platform,
            "platform_name": self.platform_name,
            "share_id": self.share_id,
            "title": self.title,
            "status": self.status,
            "error": self.error,
            "created_at": self.created_at,
        }
        if include_files:
            d["files"] = [f.to_dict() for f in self.files]
        return d


def new_id() -> str:
    return uuid.uuid4().hex[:12]
