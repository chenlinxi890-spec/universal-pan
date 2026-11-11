"""通用网盘 - 阿里云盘提供方（游客模式，无需登录）。

流程（公开接口知识，见 tickstep/aliyunpan 与阿里云 PDS 文档
“分享相关操作”；LinkSwift 油猴脚本只给出登录态直链，未用其代码）：
    POST /v2/share_link/get_share_token（share_id + share_pwd → share_token，
    无需登录；分享不存在返回 NotFound.ShareLink）
    → POST /v2/file/list_by_anonymous（share_id + parent_file_id + x-share-token，
    根目录 parent_file_id 为 "root"，next_marker 翻页）
    → POST /v2/file/get_share_link_download_url（drive_id + file_id + share_id
    + x-share-token → download_url / url）

标题取自 POST /v2/share_link/get_share_by_anonymous（share_name），
该接口失败时不阻断解析（以文件列表为准）。

注意：游客接口无有效 share_token 时返回 HTTP 404 空 body，
本模块把空 body / 非 JSON 一律按“分享不存在或已失效”处理。
"""
from __future__ import annotations

from typing import Optional

import httpx

from ..models import ProviderError, ShareFile, ShareInfo
from .base import DownloadLink, Provider

API_BASE = "https://api.aliyundrive.com"
REFERER = "https://www.alipan.com/"
ORIGIN = "https://www.alipan.com"
# alist aliyundrive_share 驱动同款 Canary 头（用于解除分享接口限速）
CANARY = "client=web,app=share,version=v2.3.1"
ROOT_PARENT = "root"


class AliyunProvider(Provider):
    platform = "aliyun"
    display_name = "阿里云盘"

    # ---------- 解析 ----------

    async def resolve(self, share_id: str, pwd: Optional[str]) -> tuple[ShareInfo, dict]:
        client = self.new_client()
        session: dict = {"share_id": share_id, "pwd": pwd or "", "client": client}
        try:
            token = await self._get_share_token(client, share_id, pwd or "")
            session["share_token"] = token
            title = ""
            try:
                info = await self._get_share_by_anonymous(client, share_id, token)
                title = str(info.get("share_name") or "")
            except ProviderError:
                # 标题接口失败不阻断解析，以文件列表为准
                pass
            files = await self._list(client, session, ROOT_PARENT)
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
        parent = dir_ref if dir_ref not in ("", "/", ROOT_PARENT) else ROOT_PARENT
        return await self._list(client, session, parent)

    # ---------- 取链 ----------

    async def get_download_url(self, session: dict, file: ShareFile) -> DownloadLink:
        client: httpx.AsyncClient = session["client"]
        drive_id = file.fid_token or session.get("drive_id", "")
        r = await client.post(
            f"{API_BASE}/v2/file/get_share_link_download_url",
            json={
                "drive_id": drive_id,
                "file_id": file.fid,
                "share_id": session["share_id"],
                "expire_sec": 14400,
            },
            headers=self._headers(session),
        )
        data = self._as_json(r)
        url = str(data.get("download_url") or data.get("url") or "")
        if not url:
            raise ProviderError("阿里云盘未返回下载链接")
        return DownloadLink(
            url=url,
            headers={"Referer": REFERER},
        )

    # ---------- 内部 ----------

    def _headers(self, session: dict) -> dict:
        h = {
            "Content-Type": "application/json",
            "Referer": REFERER,
            "Origin": ORIGIN,
            "X-Canary": CANARY,
        }
        token = session.get("share_token")
        if token:
            h["x-share-token"] = token
        return h

    async def _get_share_token(
        self, client: httpx.AsyncClient, share_id: str, pwd: str
    ) -> str:
        r = await client.post(
            f"{API_BASE}/v2/share_link/get_share_token",
            json={"share_id": share_id, "share_pwd": pwd},
            headers={
                "Content-Type": "application/json",
                "Referer": REFERER,
                "Origin": ORIGIN,
                "X-Canary": CANARY,
            },
        )
        data = self._as_json(r)
        token = str(data.get("share_token") or "")
        if not token:
            raise ProviderError("阿里云盘未返回分享凭证（share_token），分享可能已失效")
        return token

    async def _get_share_by_anonymous(
        self, client: httpx.AsyncClient, share_id: str, token: str
    ) -> dict:
        r = await client.post(
            f"{API_BASE}/v2/share_link/get_share_by_anonymous",
            json={"share_id": share_id},
            headers={
                "Content-Type": "application/json",
                "Referer": REFERER,
                "Origin": ORIGIN,
                "X-Canary": CANARY,
                "x-share-token": token,
            },
        )
        return self._as_json(r)

    async def _list(
        self, client: httpx.AsyncClient, session: dict, parent_file_id: str
    ) -> list[ShareFile]:
        files: list[ShareFile] = []
        marker = ""
        while True:
            r = await client.post(
                f"{API_BASE}/v2/file/list_by_anonymous",
                json={
                    "share_id": session["share_id"],
                    "parent_file_id": parent_file_id,
                    "limit": 100,
                    "marker": marker,
                    "order_by": "name",
                    "order_direction": "ASC",
                },
                headers=self._headers(session),
            )
            data = self._as_json(r)
            for item in data.get("items") or []:
                files.append(
                    ShareFile(
                        fid=str(item.get("file_id", "")),
                        name=item.get("name", ""),
                        size=int(item.get("size") or 0),
                        is_dir=item.get("type") == "folder",
                        path=str(item.get("parent_file_id", "")),
                        # 下载接口要 drive_id，借用 fid_token 字段随文件带走
                        fid_token=str(item.get("drive_id", "")),
                        modify_time=str(item.get("updated_at", "")),
                    )
                )
                if item.get("drive_id"):
                    session.setdefault("drive_id", str(item["drive_id"]))
            marker = str(data.get("next_marker") or "")
            if not marker:
                break
        return files

    @staticmethod
    def _guest_err(code: str, message: str) -> str:
        if "Pwd" in code:
            return "提取码错误"
        if "NotFound" in code:
            return "分享不存在或已失效"
        if "Expired" in code:
            return "分享已过期"
        if "Cancelled" in code:
            return "分享已被取消"
        if "Forbidden" in code:
            return "该分享需要登录阿里云盘后才能访问"
        if "TooManyRequests" in code or "RateLimit" in code:
            return "阿里云盘请求过于频繁，请稍后重试"
        return message or f"阿里云盘接口返回错误（code={code}）"

    @staticmethod
    def _as_json(r: httpx.Response) -> dict:
        try:
            data = r.json()
        except Exception:
            # 游客接口无有效凭证时返回 HTTP 404 空 body
            raise ProviderError("分享不存在或已失效", str(r.status_code))
        if not isinstance(data, dict):
            raise ProviderError("阿里云盘返回格式异常")
        code = str(data.get("code") or "")
        if code:
            raise ProviderError(
                AliyunProvider._guest_err(code, str(data.get("message") or "")),
                code,
            )
        return data
