"""通用网盘 - UC 网盘提供方（游客模式，无需登录）。

流程（端口自 YunX-Desktop UCApi.kt 的游客链路）：
    POST /1/clouddrive/share/sharepage/token（pwd_id + passcode → stoken）
    → POST /1/clouddrive/share/sharepage/v2/detail（文件列表，data.detail_info.list）
    → POST /1/clouddrive/file/download（fids + fids_token + pwd_id + stoken → download_url，
       响应 Set-Cookie 下发游客令牌 __pugs，直链下载时回带，缺它 OSS 直接 403）

限制：超大文件报 23018、部分分享报 31001（需登录），如实报错。
"""
from __future__ import annotations

import urllib.parse
from typing import Optional

import httpx

from ..models import ProviderError, ShareFile, ShareInfo
from .base import DownloadLink, Provider

API_BASE = "https://pc-api.uc.cn"
WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
GUEST_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) uc-cloud-drive/2.5.20 Chrome/100.0.4896.160 "
    "Electron/18.3.5.4-b478491100 Safari/537.36 Channel/pckk_other_ch"
)
DOWNLOAD_REFERER = "https://drive.uc.cn/"


class UCProvider(Provider):
    platform = "uc"
    display_name = "UC网盘"

    # ---------- 解析 ----------

    async def resolve(self, share_id: str, pwd: Optional[str]) -> tuple[ShareInfo, dict]:
        client = self.new_client(headers={"User-Agent": WEB_UA})
        session: dict = {"pwd_id": share_id, "pwd": pwd or "", "client": client}
        try:
            token = await self._get_token(client, share_id, pwd or "")
            session["stoken"] = token["stoken"]
            files = await self._list_detail(client, session, None)
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
            "pwd_id": session["pwd_id"],
            "stoken": session["stoken"],
            "fids_token": [file.fid_token],
        }
        headers = {
            "User-Agent": GUEST_UA,
            "Sec-Ch-Ua": '"Not=A?Brand";v="99", "Chromium";v="100", "Google Chrome";v="100"',
            "Content-Type": "application/json",
        }
        pugs = session.get("pugs")
        if pugs:
            headers["Cookie"] = f"__pugs={pugs}"
        r = await client.post(
            f"{API_BASE}/1/clouddrive/file/download?entry=ft&fr=pc&pr=UCBrowser",
            json=body,
            headers=headers,
        )
        data = self._as_json(r)
        code = data.get("code", 0)
        if data.get("status", 200) != 200 and code != 0:
            raise ProviderError(self._guest_err(code, data.get("message", "")), str(code))
        arr = data.get("data") or []
        if not arr:
            raise ProviderError("UC 未返回下载链接")
        item = arr[0]
        url = item.get("download_url", "")
        if not url:
            raise ProviderError("UC 未返回下载链接")
        # 捕获游客令牌 __pugs（直链下载必须带，缺它 403）
        pugs = ""
        for ck in client.cookies.jar:
            if ck.name == "__pugs":
                pugs = ck.value
        if pugs:
            session["pugs"] = pugs
        cookies = {"__pugs": pugs} if pugs else None
        return DownloadLink(
            url=url,
            headers={"User-Agent": GUEST_UA, "Referer": DOWNLOAD_REFERER},
            cookies=cookies,
        )

    # ---------- 内部 ----------

    async def _get_token(
        self, client: httpx.AsyncClient, pwd_id: str, pwd: str
    ) -> dict:
        r = await client.post(
            f"{API_BASE}/1/clouddrive/share/sharepage/token?pr=UCBrowser&fr=pc",
            json={"pwd_id": pwd_id, "passcode": pwd, "share_for_transfer": True},
            headers={"User-Agent": WEB_UA, "Content-Type": "application/json"},
        )
        data = self._as_json(r)
        code = data.get("code", 0)
        if data.get("status", 200) != 200 and code != 0:
            raise ProviderError(self._guest_err(code, data.get("message", "")), str(code))
        d = data.get("data") or {}
        if not d.get("stoken"):
            raise ProviderError("UC 未返回分享凭证（stoken），分享可能已失效")
        return d

    async def _list_detail(
        self, client: httpx.AsyncClient, session: dict, pdir_fid: Optional[str], page: int = 1
    ) -> list[ShareFile]:
        body: dict = {
            "pwd_id": session["pwd_id"],
            "passcode": session.get("pwd", ""),
            "force": 0,
            "page": page,
            "size": 50,
            "fetch_banner": 1,
            "fetch_share": 1,
            "fetch_total": 1,
            "sort": "file_type:asc,file_name:asc",
            "banner_platform": "other",
            "web_platform": "windows",
            "fetch_error_background": 1,
        }
        if pdir_fid:
            body["pdir_fid"] = pdir_fid
        r = await client.post(
            f"{API_BASE}/1/clouddrive/share/sharepage/v2/detail?pr=UCBrowser&fr=pc&ve=2.5.20",
            json=body,
            headers={
                "User-Agent": WEB_UA,
                "Origin": "https://drive.uc.cn",
                "Referer": "https://drive.uc.cn/",
                "Content-Type": "application/json;charset=UTF-8",
            },
        )
        data = self._as_json(r)
        code = data.get("code", 0)
        if data.get("status", 200) != 200 and code != 0:
            raise ProviderError(self._guest_err(code, data.get("message", "")), str(code))
        d = data.get("data") or {}
        detail = d.get("detail_info") or {}
        files: list[ShareFile] = []
        for item in detail.get("list") or []:
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
        if code == 31001:
            return "该分享需要登录 UC 网盘后才能下载"
        if code == 23018:
            return "该文件超出游客可获取的大小上限，请先登录 UC 网盘再下载"
        if code == 14001:
            return "分享已失效或提取码有误，请重新解析"
        if code == 41020:
            return "文件令牌已过期，请重新解析分享"
        return message or f"UC 接口返回错误（code={code}）"

    @staticmethod
    def _as_json(r: httpx.Response) -> dict:
        try:
            data = r.json()
        except Exception:
            raise ProviderError(f"UC 返回了非 JSON 内容（HTTP {r.status_code}），可能是风控页")
        if not isinstance(data, dict):
            raise ProviderError("UC 返回格式异常")
        return data
