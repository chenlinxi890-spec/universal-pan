"""通用网盘 - 提供方基类。

每个提供方一个模块，统一接口：
    resolve(share_id, pwd) -> ShareInfo   解析分享，返回标题 + 根目录文件列表
    list_dir(session, dir_ref) -> list[ShareFile]   列子目录
    get_download_url(session, file) -> DownloadLink  取文件下载直链

session 是 resolve 返回后保存在 Task 里的不透明 dict（sekey/stoken/cookie 等），
只在服务端使用，绝不返回给前端。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import httpx

from ..models import ProviderError, ShareFile, ShareInfo

# 桌面 Chrome UA（各家通用）
UA_WEB = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class DownloadLink:
    url: str
    headers: dict | None = None
    cookies: dict | None = None
    # 直链是否支持断点续传（Range）；未知时为 None
    supports_range: Optional[bool] = None


class Provider:
    platform: str = ""
    display_name: str = ""
    # 该提供方游客模式是否可用（False = 必须登录，本工具会明确报错）
    guest_ok: bool = True
    # 游客模式能否取下载直链（False = 只能列文件，下载需登录）
    guest_download: bool = True

    def new_client(self, headers: dict | None = None) -> httpx.AsyncClient:
        h = {"User-Agent": UA_WEB}
        if headers:
            h.update(headers)
        proxy = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("http_proxy")
        )
        return httpx.AsyncClient(
            headers=h,
            follow_redirects=True,
            timeout=httpx.Timeout(120.0, connect=30.0),
            proxy=proxy or None,
            # 必须关掉 trust_env：本机 no_proxy 里有裸 IPv6（::1）写法，
            # httpx 解析环境代理时会把它当 URL 拼而报错 "Invalid port"
            trust_env=False,
            # 本机出口代理做 TLS 拦截，CA 在 SSL_CERT_FILE 里；
            # 笔记本上无此变量时走系统默认 CA
            verify=os.environ.get("SSL_CERT_FILE") or True,
        )

    async def resolve(self, share_id: str, pwd: Optional[str]) -> tuple[ShareInfo, dict]:
        """解析分享链接。返回 (ShareInfo, session)。"""
        raise NotImplementedError

    async def list_dir(self, session: dict, dir_ref: str) -> list[ShareFile]:
        """列出分享内子目录。dir_ref 含义由各家定义（百度：path；夸克：pdir_fid）。"""
        raise NotImplementedError

    async def get_download_url(self, session: dict, file: ShareFile) -> DownloadLink:
        """取单个文件的下载直链。"""
        raise NotImplementedError

    def _check_json_errno(self, data: dict, ok_field: str = "errno") -> None:
        errno = data.get(ok_field)
        if errno not in (0, None):
            msg = data.get("err_msg") or data.get("show_msg") or data.get("message") or ""
            raise ProviderError(f"{msg or '接口返回错误'}（errno={errno}）", str(errno))
