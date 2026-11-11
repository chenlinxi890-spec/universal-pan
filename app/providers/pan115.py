"""通用网盘 - 115网盘提供方（游客模式：列目录 + 小文件取直链）。

流程（端口自 YunX-Desktop Pan115Api.kt 的游客链路）：
    GET webapi.115.com/share/snap（列目录；无签名；Cookie 为空即不带）
    → GET appversion.115.com/1/web/1.0/api/chrome（取 115Browser 版本号）
    → GET webapi.115.com/share/downurl（取直链；UA 必须为 115Browser/<版本>；
       响应 Set-Cookie 下发的 CDN Cookie 必须带进下载请求，缺它 403）

限制：大文件取链报 50029，游客无兜底（兜底需登录后转存），如实报错。
"""
from __future__ import annotations

import urllib.parse
from typing import Optional

import httpx

from ..models import ProviderError, ShareFile, ShareInfo
from .base import DownloadLink, Provider

WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)
VERSION_URL = "https://appversion.115.com/1/web/1.0/api/chrome"


def share_referer(share_code: str, receive_code: str) -> str:
    if receive_code:
        return f"https://115cdn.com/s/{share_code}?password={receive_code}&"
    return f"https://115cdn.com/s/{share_code}"


class Pan115Provider(Provider):
    platform = "pan115"
    display_name = "115网盘"

    # ---------- 解析 ----------

    async def resolve(self, share_id: str, pwd: Optional[str]) -> tuple[ShareInfo, dict]:
        client = self.new_client(headers={"User-Agent": WEB_UA})
        session: dict = {
            "share_code": share_id,
            "pwd": pwd or "",
            "client": client,
            "user_id": "",
            "client_ua": "",
        }
        try:
            data = await self._snap(client, session, "0", 0)
            session["user_id"] = str((data.get("userinfo") or {}).get("user_id", ""))
            session["title"] = str((data.get("shareinfo") or {}).get("share_title", ""))
            files = self._parse_list(data.get("list") or [], "0")
            return (
                ShareInfo(
                    platform=self.platform,
                    platform_name=self.display_name,
                    share_id=share_id,
                    title=session["title"],
                    files=files,
                ),
                session,
            )
        except Exception:
            await client.aclose()
            raise

    async def list_dir(self, session: dict, dir_ref: str) -> list[ShareFile]:
        client: httpx.AsyncClient = session["client"]
        files: list[ShareFile] = []
        offset = 0
        while True:
            data = await self._snap(client, session, dir_ref, offset)
            items = data.get("list") or []
            files.extend(self._parse_list(items, dir_ref))
            total = int(data.get("count") or 0)
            offset += len(items)
            if not items or len(items) < 100 or offset >= total:
                break
        return files

    # ---------- 取链 ----------

    async def get_download_url(self, session: dict, file: ShareFile) -> DownloadLink:
        client: httpx.AsyncClient = session["client"]
        if not session.get("user_id"):
            raise ProviderError("缺少分享者标识，请重新解析分享链接")
        client_ua = session.get("client_ua") or await self._refresh_client_ua(client)
        session["client_ua"] = client_ua
        params: dict[str, str] = {
            "dl": "1",
            "user_id": session["user_id"],
            "share_code": session["share_code"],
            "file_id": file.fid,
        }
        if session.get("pwd"):
            params["receive_code"] = session["pwd"]
        url = "https://webapi.115.com/share/downurl?" + urllib.parse.urlencode(params)
        r = await client.get(
            url,
            headers={
                "User-Agent": client_ua,
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": share_referer(session["share_code"], session.get("pwd", "")),
            },
        )
        data = self._as_json(r)
        if not data.get("state"):
            errno = data.get("errno")
            raise ProviderError(self._err_text(errno, data.get("error", "")), str(errno or ""))
        d = data.get("data") or {}
        url_field = d.get("file_url_302") or d.get("file_url") or (d.get("url") or {}).get("url")
        if not url_field:
            raise ProviderError("115 未返回下载地址")
        # CDN Cookie：从 Set-Cookie 取第一条（优先 path 绑定对象路径的）
        cdn_cookie = self._pick_cdn_cookie(r)
        cookies = {cdn_cookie[0]: cdn_cookie[1]} if cdn_cookie else None
        return DownloadLink(
            url=url_field,
            headers={"User-Agent": client_ua, "Referer": "https://115.com/"},
            cookies=cookies,
        )

    # ---------- 内部 ----------

    async def _snap(
        self, client: httpx.AsyncClient, session: dict, cid: str, offset: int
    ) -> dict:
        params = {
            "share_code": session["share_code"],
            "receive_code": session.get("pwd", ""),
            "offset": str(offset),
            "limit": "100",
            "asc": "0",
            "cid": cid,
            "format": "json",
        }
        url = "https://webapi.115.com/share/snap?" + urllib.parse.urlencode(params)
        r = await client.get(
            url,
            headers={
                "User-Agent": WEB_UA,
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": share_referer(session["share_code"], session.get("pwd", "")),
            },
        )
        data = self._as_json(r)
        if not data.get("state"):
            errno = data.get("errno")
            raise ProviderError(self._err_text(errno, data.get("error", "")), str(errno or ""))
        return data.get("data") or {}

    @staticmethod
    def _parse_list(items: list, parent_cid: str) -> list[ShareFile]:
        files: list[ShareFile] = []
        for item in items:
            is_dir = item.get("fc") == 0
            fid = str(item.get("cid", "")) if is_dir else str(item.get("fid", ""))
            files.append(
                ShareFile(
                    fid=fid,
                    name=item.get("n", ""),
                    size=int(item.get("s") or 0),
                    is_dir=is_dir,
                    path=str(item.get("pid") if is_dir else item.get("cid", "")),
                    modify_time=str(item.get("t", "")),
                )
            )
        return files

    async def _refresh_client_ua(self, client: httpx.AsyncClient) -> str:
        version = "36.0.1"
        try:
            r = await client.get(
                VERSION_URL,
                headers={"User-Agent": WEB_UA, "Referer": "https://115.com/"},
            )
            data = r.json()
            v = ((data.get("data") or {}).get("win") or {}).get("version_code")
            if v:
                version = str(v)
        except Exception:
            pass
        return f"Mozilla/5.0 115Browser/{version}"

    @staticmethod
    def _pick_cdn_cookie(r: httpx.Response):
        """从 Set-Cookie 里取 CDN Cookie（优先 path 绑定对象路径的那条）。"""
        raw = r.headers.get_list("set-cookie")
        best = None
        for entry in raw:
            part = entry.split(";", 1)[0].strip()
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            if "/<" in entry or "path=/" in entry.lower():
                return (name.strip(), value.strip())
            if best is None:
                best = (name.strip(), value.strip())
        return best

    @staticmethod
    def _err_text(errno, msg: str) -> str:
        table = {
            50029: "该文件较大，115 不向游客下发直链，需登录后转存才能下载",
            4100012: "该分享需要访问码",
            4100008: "访问码错误",
            4100026: "分享不存在或已删除",
            4100016: "不允许分享空文件夹",
            990001: "115 登录超时，请重新解析",
        }
        if errno in table:
            return table[errno]
        return msg or f"115 接口返回错误（errno={errno}）"

    @staticmethod
    def _as_json(r: httpx.Response) -> dict:
        try:
            data = r.json()
        except Exception:
            raise ProviderError(f"115 返回了非 JSON 内容（HTTP {r.status_code}），可能是风控页")
        if not isinstance(data, dict):
            raise ProviderError("115 返回格式异常")
        return data
