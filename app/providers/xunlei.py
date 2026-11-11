"""通用网盘 - 迅雷网盘提供方（游客模式：只列文件，下载需登录）。

流程（端口自 YunX-Desktop XunleiApi.kt 的游客链路）：
    GET /drive/v1/share（根目录；匿名，不带 Authorization）
    → GET /drive/v1/share/detail（子目录；用 pass_code_token）

游客取下载地址在代码里不存在：下载必须登录后转存、再调个人文件详情。
因此 get_download_url 明确报错，不编造直链。
"""
from __future__ import annotations

import secrets
import urllib.parse
from typing import Optional

import httpx

from ..models import ProviderError, ShareFile, ShareInfo
from .base import DownloadLink, Provider

API_BASE = "https://api-pan.xunlei.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
CLIENT_VERSION = "8.31.0.9726"


class XunleiProvider(Provider):
    platform = "xunlei"
    display_name = "迅雷网盘"
    guest_download = False  # 游客只能列文件，下载需登录后转存

    # ---------- 解析 ----------

    async def resolve(self, share_id: str, pwd: Optional[str]) -> tuple[ShareInfo, dict]:
        client = self.new_client(headers={"User-Agent": UA})
        session: dict = {
            "share_id": share_id,
            "pwd": pwd or "",
            "client": client,
            "device_id": secrets.token_hex(16),
        }
        try:
            data = await self._get_share(client, session, share_id, pwd or "")
            session["pass_code_token"] = data.get("pass_code_token", "")
            files = self._parse_files(data.get("files") or [])
            return (
                ShareInfo(
                    platform=self.platform,
                    platform_name=self.display_name,
                    share_id=share_id,
                    title=data.get("title", ""),
                    files=files,
                ),
                session,
            )
        except Exception:
            await client.aclose()
            raise

    async def list_dir(self, session: dict, dir_ref: str) -> list[ShareFile]:
        client: httpx.AsyncClient = session["client"]
        params = {
            "share_id": session["share_id"],
            "parent_id": dir_ref,
            "pass_code_token": session.get("pass_code_token", ""),
            "limit": "100",
            "page_token": "",
            "thumbnail_size": "SIZE_SMALL",
        }
        url = f"{API_BASE}/drive/v1/share/detail?" + urllib.parse.urlencode(params)
        r = await client.get(url, headers=self._headers(session))
        data = self._as_json(r)
        d = data.get("data") or data
        return self._parse_files(d.get("files") or [])

    # ---------- 取链（游客不可用） ----------

    async def get_download_url(self, session: dict, file: ShareFile) -> DownloadLink:
        raise ProviderError(
            "迅雷网盘的分享下载需要先登录账号、把文件转存到个人盘后才能取直链，"
            "本工具暂不支持登录，请用迅雷客户端下载该分享"
        )

    # ---------- 内部 ----------

    def _headers(self, session: dict) -> dict:
        return {
            "User-Agent": UA,
            "X-Device-Id": session["device_id"],
            "X-Client-Version": CLIENT_VERSION,
            "Content-Type": "application/json",
            "Origin": "https://pan.xunlei.com",
            "Referer": "https://pan.xunlei.com/",
        }

    async def _get_share(
        self, client: httpx.AsyncClient, session: dict, share_id: str, pwd: str
    ) -> dict:
        params = {
            "share_id": share_id,
            "pass_code": pwd,
            "limit": "100",
            "page_token": "",
            "thumbnail_size": "SIZE_SMALL",
        }
        url = f"{API_BASE}/drive/v1/share?" + urllib.parse.urlencode(params)
        r = await client.get(url, headers=self._headers(session))
        data = self._as_json(r)
        # 错误形（非 200 data 包）直接报出来，别当成空列表静默成功
        if data.get("error") or data.get("error_code") is not None:
            err = str(data.get("error") or "")
            if "captcha" in err:
                raise ProviderError(
                    "迅雷要求人机验证（captcha_token），游客解析暂不可用，"
                    "请用迅雷客户端打开该分享"
                )
            raise ProviderError(
                f"迅雷接口返回错误：{data.get('error_description') or err or data.get('error_code')}"
            )
        d = data.get("data") or data
        status = d.get("share_status", "")
        if status == "PASS_CODE_NEED":
            raise ProviderError("该分享需要提取码")
        if status == "PASS_CODE_ERROR":
            raise ProviderError("提取码错误")
        if status == "PASS_CODE_EMPTY":
            raise ProviderError("该分享需要提取码")
        if not (d.get("files") or []) and not d.get("title"):
            raise ProviderError("该迅雷分享不存在或已失效")
        return d

    @staticmethod
    def _parse_files(items: list) -> list[ShareFile]:
        files: list[ShareFile] = []
        for item in items:
            files.append(
                ShareFile(
                    fid=str(item.get("id", "")),
                    name=item.get("name", ""),
                    size=int(item.get("size") or 0),
                    is_dir=item.get("kind") == "drive#folder",
                    path=str(item.get("parent_id", "")),
                    modify_time=str(item.get("modified_time", "")),
                )
            )
        return files

    @staticmethod
    def _as_json(r: httpx.Response) -> dict:
        try:
            data = r.json()
        except Exception:
            raise ProviderError(f"迅雷返回了非 JSON 内容（HTTP {r.status_code}），可能是风控页")
        if not isinstance(data, dict):
            raise ProviderError("迅雷返回格式异常")
        return data
