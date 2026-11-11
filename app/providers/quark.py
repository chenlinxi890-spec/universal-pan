"""通用网盘 - 夸克网盘提供方（游客模式，无需登录）。

流程（端口自 YunX-Desktop QuarkApi.kt 的游客链路）：
    分享页 GET（拿会话 Cookie）
    → POST /1/clouddrive/share/sharepage/token（pwd_id + passcode → stoken）
    → GET /1/clouddrive/share/sharepage/detail（文件列表）
    → POST /1/clouddrive/file/download（fids + fids_token + pwd_id + stoken → download_url，
       响应 Set-Cookie 下发游客态 __pugs，直链下载时回带）

限制（YunX 实测）：游客取链约 50MB 以内的小文件可用，超出报 23018；
部分分享需要登录（31001）。遇到时如实报错，不绕过。
关键细节（2026-11-11 实测）：Origin 头必须是不带尾斜杠的
https://pan.quark.cn，带 "/" 会被夸克网关判 Invalid CORS request（403）。
"""
from __future__ import annotations

import urllib.parse
from typing import Optional

import httpx

from ..models import ProviderError, ShareFile, ShareInfo
from .base import DownloadLink, Provider

API_BASE = "https://drive-pc.quark.cn"
API_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "quark-cloud-drive/2.5.20 Chrome/100.0.4896.160 Electron/18.3.5.12-a038f7b798 "
    "Safari/537.36 Channel/pckk_other_ch"
)
REFERER = "https://pan.quark.cn/"
# Origin 头必须是不带斜杠的源（带 "/" 会被夸克网关判 Invalid CORS request，403）
ORIGIN = "https://pan.quark.cn"


class QuarkProvider(Provider):
    platform = "quark"
    display_name = "夸克网盘"

    # ---------- 解析 ----------

    async def resolve(self, share_id: str, pwd: Optional[str]) -> tuple[ShareInfo, dict]:
        client = self.new_client(headers={"User-Agent": API_UA})
        session: dict = {"pwd_id": share_id, "pwd": pwd or "", "client": client}
        try:
            # 1. 先 GET 分享页，拿会话 Cookie
            try:
                await client.get(
                    f"https://pan.quark.cn/s/{share_id}",
                    headers={"Referer": REFERER},
                )
            except Exception:
                pass
            # 2. 拿 stoken
            token = await self._get_token(client, share_id, pwd or "")
            session["stoken"] = token["stoken"]
            session["title"] = token.get("title", "")
            # 3. 列根目录
            files = await self._list_detail(client, session, "0")
            return (
                ShareInfo(
                    platform=self.platform,
                    platform_name=self.display_name,
                    share_id=share_id,
                    title=token.get("title", ""),
                    files=files,
                ),
                session,
            )
        except Exception:
            await client.aclose()
            raise

    async def list_dir(self, session: dict, dir_ref: str) -> list[ShareFile]:
        client: httpx.AsyncClient = session["client"]
        return await self._list_detail(client, session, dir_ref)

    # ---------- 取链 ----------

    async def get_download_url(self, session: dict, file: ShareFile) -> DownloadLink:
        client: httpx.AsyncClient = session["client"]
        body = {
            "fids": [file.fid],
            "fids_token": [file.fid_token],
            "pwd_id": session["pwd_id"],
            "stoken": session["stoken"],
            "speedup_session": "",
            "token": "",
        }
        r = await client.post(
            f"{API_BASE}/1/clouddrive/file/download?pr=ucpro&fr=pc&sys=win32&ve=3.23.2",
            json=body,
            headers={
                "User-Agent": API_UA,
                "Origin": ORIGIN,
                "Referer": REFERER,
                "Content-Type": "application/json",
            },
        )
        data = self._as_json(r)
        code = data.get("code", 0)
        status = data.get("status", 200)
        if status != 200 and code != 0:
            raise ProviderError(self._guest_err(code, data.get("message", "")), str(code))
        arr = data.get("data") or []
        if not arr:
            raise ProviderError("夸克未返回下载链接")
        item = arr[0]
        url = item.get("download_url", "")
        if not url:
            raise ProviderError("夸克未返回下载链接")
        # 直接扫 jar（同名取最后一个），不用 .items()（同名会抛 CookieConflict）
        cookies: dict[str, str] = {}
        for ck in client.cookies.jar:
            cookies[ck.name] = ck.value
        return DownloadLink(
            url=url,
            headers={"User-Agent": API_UA, "Referer": REFERER},
            cookies=cookies or None,
        )

    # ---------- 内部 ----------

    async def _get_token(
        self, client: httpx.AsyncClient, pwd_id: str, pwd: str
    ) -> dict:
        r = await client.post(
            f"{API_BASE}/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc",
            json={
                "pwd_id": pwd_id,
                "passcode": pwd,
                "support_visit_limit_private_share": True,
            },
            headers={
                "User-Agent": API_UA,
                "Origin": ORIGIN,
                "Referer": REFERER,
                "Content-Type": "application/json",
            },
        )
        data = self._as_json(r)
        code = data.get("code", 0)
        if data.get("status", 200) != 200 and code != 0:
            raise ProviderError(self._guest_err(code, data.get("message", "")), str(code))
        d = data.get("data") or {}
        stoken = d.get("stoken", "")
        if not stoken:
            raise ProviderError("夸克未返回分享凭证（stoken），分享可能已失效")
        return d

    async def _list_detail(
        self, client: httpx.AsyncClient, session: dict, pdir_fid: str, page: int = 1
    ) -> list[ShareFile]:
        params = {
            "pr": "ucpro",
            "fr": "pc",
            "pwd_id": session["pwd_id"],
            "stoken": session["stoken"],
            "pdir_fid": pdir_fid,
            "ver": "2",
            "force": "0",
            "_page": str(page),
            "_size": "100",
            "_fetch_banner": "0",
            "_fetch_share": "0",
            "fetch_relate_conversation": "0",
            "_fetch_total": "1",
            "_sort": "file_type:asc,file_name:asc",
        }
        url = f"{API_BASE}/1/clouddrive/share/sharepage/detail?" + urllib.parse.urlencode(params)
        r = await client.get(
            url,
            headers={
                "User-Agent": API_UA,
                "Origin": ORIGIN,
                "Referer": REFERER,
            },
        )
        data = self._as_json(r)
        code = data.get("code", 0)
        if data.get("status", 200) != 200 and code != 0:
            raise ProviderError(self._guest_err(code, data.get("message", "")), str(code))
        d = data.get("data") or {}
        files: list[ShareFile] = []
        for item in d.get("list") or []:
            files.append(
                ShareFile(
                    fid=str(item.get("fid", "")),
                    name=item.get("file_name", ""),
                    size=int(item.get("size") or 0),
                    is_dir=bool(item.get("dir", False)),
                    path=str(item.get("pdir_fid", "")),
                    fid_token=item.get("share_fid_token", ""),
                    modify_time=str(item.get("updated_at", "")),
                )
            )
        return files

    @staticmethod
    def _guest_err(code: int, message: str) -> str:
        if code == 23018:
            return "该文件超出游客可获取的大小上限（夸克约 50MB），请先登录夸克网盘再下载"
        if code == 31001:
            return "该分享需要登录夸克网盘后才能下载"
        if code in (41005, 41006, 41007):
            return "提取码错误或分享已失效"
        return message or f"夸克接口返回错误（code={code}）"

    @staticmethod
    def _as_json(r: httpx.Response) -> dict:
        try:
            data = r.json()
        except Exception:
            raise ProviderError(f"夸克返回了非 JSON 内容（HTTP {r.status_code}），可能是风控页")
        if not isinstance(data, dict):
            raise ProviderError("夸克返回格式异常")
        return data
