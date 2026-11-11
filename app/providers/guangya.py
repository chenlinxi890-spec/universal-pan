"""通用网盘 - 光鸭云盘提供方（游客模式）。

流程（端口自 YunX-Desktop GuangYaApi.kt 的游客链路）：
    POST /userres/v1/get_share_access_token（shareId + code → 分享访问令牌）
    → POST /userres/v1/get_share_page_files_list（parentId + accessToken → 文件列表）
    → POST /userres/v1/get_share_download_url（fileId + accessToken → signedURL）

游客流程无签名、无加密；每请求带随机 did（32 位 hex）与 traceparent。
限制：分享者未开启免登录下载时报业务码 207，需登录后转存，如实报错。
"""
from __future__ import annotations

import secrets
from typing import Optional

import httpx

from ..models import ProviderError, ShareFile, ShareInfo
from .base import DownloadLink, Provider

API_BASE = "https://api.guangyapan.com/userres/v1"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DOWNLOAD_REFERER = "https://www.guangyapan.com/"


class GuangYaProvider(Provider):
    platform = "guangya"
    display_name = "光鸭云盘"

    # ---------- 解析 ----------

    async def resolve(self, share_id: str, pwd: Optional[str]) -> tuple[ShareInfo, dict]:
        client = self.new_client(headers={"User-Agent": UA})
        session: dict = {
            "shareId": share_id,
            "pwd": pwd or "",
            "client": client,
            "did": secrets.token_hex(16),
        }
        try:
            token = await self._post(
                client, session, "/get_share_access_token",
                {"shareId": share_id, "code": pwd or ""},
            )
            access = token.get("accessToken") or token.get("access_token")
            if not access:
                raise ProviderError("光鸭未返回分享访问令牌")
            session["accessToken"] = access
            title = ""
            try:
                summary = await self._post(
                    client, session, "/get_share_summary", {"shareId": share_id}
                )
                title = str(summary.get("title") or summary.get("shareName") or "")
            except ProviderError:
                pass
            files = await self._list(client, session, "")
            return (
                ShareInfo(
                    platform=self.platform,
                    platform_name=self.display_name,
                    share_id=share_id,
                    title=title,
                    files=files,
                ),
                session,
            )
        except Exception:
            await client.aclose()
            raise

    async def list_dir(self, session: dict, dir_ref: str) -> list[ShareFile]:
        client: httpx.AsyncClient = session["client"]
        return await self._list(client, session, dir_ref)

    # ---------- 取链 ----------

    async def get_download_url(self, session: dict, file: ShareFile) -> DownloadLink:
        client: httpx.AsyncClient = session["client"]
        data = await self._post(
            client, session, "/get_share_download_url",
            {"fileId": file.fid, "accessToken": session["accessToken"]},
        )
        url = (
            data.get("signedURL")
            or data.get("signedUrl")
            or data.get("downloadUrl")
            or data.get("download_url")
            or data.get("url")
            or data.get("cdnUrl")
        )
        if not url:
            raise ProviderError("光鸭未返回下载地址")
        return DownloadLink(
            url=url,
            headers={"User-Agent": UA, "Referer": DOWNLOAD_REFERER},
        )

    # ---------- 内部 ----------

    def _headers(self, session: dict) -> dict:
        tp = f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"
        return {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://www.guangyapan.com",
            "Referer": "https://www.guangyapan.com/",
            "dt": "4",
            "traceparent": tp,
            "did": session["did"],
        }

    async def _post(
        self, client: httpx.AsyncClient, session: dict, path: str, body: dict
    ) -> dict:
        r = await client.post(
            f"{API_BASE}{path}", json=body, headers=self._headers(session)
        )
        try:
            data = r.json()
        except Exception:
            raise ProviderError(f"光鸭返回了非 JSON 内容（HTTP {r.status_code}），可能是风控页")
        if not isinstance(data, dict):
            raise ProviderError("光鸭返回格式异常")
        code = data.get("code")
        if data.get("success") is False or data.get("error"):
            raise ProviderError(self._err_text(code, str(data.get("error") or "")), str(code or ""))
        if code not in (None, "", "0", 0):
            raise ProviderError(self._err_text(code, data.get("message", "") or ""), str(code))
        d = data.get("data")
        if isinstance(d, dict):
            return d
        return data

    async def _list(
        self, client: httpx.AsyncClient, session: dict, parent_id: str
    ) -> list[ShareFile]:
        files: list[ShareFile] = []
        cursor: Optional[str] = None
        while True:
            body: dict = {
                "parentId": parent_id,
                "pageSize": 100,
                "orderBy": 0,
                "sortType": 0,
                "accessToken": session["accessToken"],
            }
            if cursor:
                body["cursor"] = cursor
            data = await self._post(client, session, "/get_share_page_files_list", body)
            items = None
            for key in ("list", "items", "files", "records", "content"):
                if isinstance(data.get(key), list):
                    items = data[key]
                    break
            if items is None:
                break
            for item in items:
                files.append(self._parse_item(item))
            if not data.get("hasMore"):
                break
            cursor = data.get("cursor")
            if not cursor:
                break
        return files

    @staticmethod
    def _parse_item(item: dict) -> ShareFile:
        fid = str(
            item.get("fileId")
            or item.get("file_id")
            or item.get("id")
            or item.get("resId")
            or item.get("resourceId")
            or ""
        )
        name = str(
            item.get("fileName")
            or item.get("name")
            or item.get("filename")
            or item.get("dirName")
            or item.get("title")
            or ""
        )
        size = item.get("fileSize", item.get("size", item.get("file_size", 0))) or 0
        res_type = str(item.get("resType", ""))
        is_dir = (
            res_type == "2"
            or bool(item.get("isDir", item.get("isdir", item.get("dir", item.get("isFolder", item.get("folder", False))))))
            or bool(item.get("dirName"))
            or str(item.get("type", item.get("fileType", ""))).lower() in ("folder", "dir", "directory", "0")
        )
        mtime = str(
            item.get("updatedAt")
            or item.get("updateAt")
            or item.get("updated_at")
            or item.get("updateTime")
            or item.get("mtime")
            or item.get("utime")
            or item.get("createdAt")
            or item.get("createTime")
            or item.get("ctime")
            or ""
        )
        return ShareFile(
            fid=fid,
            name=name,
            size=int(size),
            is_dir=is_dir,
            path=str(item.get("parentId", item.get("parentFileId", item.get("parent_id", "")))),
            modify_time=mtime,
        )

    @staticmethod
    def _err_text(code, msg: str) -> str:
        code = str(code or "")
        if code == "209":
            return "光鸭分享提取码错误或缺失"
        if code in ("205", "206", "207", "504"):
            return "该光鸭分享下载受限（分享者未开启免登录下载），需登录并转存后下载"
        if code in ("200", "201", "202"):
            return "该光鸭分享已取消、过期或不可访问"
        return msg or f"光鸭接口返回错误（code={code}）"
