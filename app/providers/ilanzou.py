"""通用网盘 - 蓝奏云优享版提供方（游客模式）。

流程（端口自 YunX-Desktop ILanzouApi.kt 的游客链路）：
    GET apis.ilanzou.com/unproved/getUuid（取设备标识 uuid）
    → GET apix.ilanzou.com/unproved/share/list（文件列表）
    → GET apix.ilanzou.com/unproved/file/redirect（取下载地址，302 Location 或 JSON url）

签名：AES/ECB/PKCS5Padding，密钥 lanZouY-disk-app，密文小写十六进制；
用于 timestamp（毫秒时间戳本身）、downloadId（fileId|userId）、auth（fileId|ts）。

限制：匿名分享接口没有传提取码的位置，带提取码的优享版分享按这份代码走不通，
如实报错。
"""
from __future__ import annotations

import urllib.parse
from typing import Optional

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from ..models import ProviderError, ShareFile, ShareInfo
from .base import DownloadLink, Provider

APIS_BASE = "https://apis.ilanzou.com"
APIX_BASE = "https://apix.ilanzou.com"
AES_KEY = b"lanZouY-disk-app"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _aes_hex(plaintext: str) -> str:
    cipher = AES.new(AES_KEY, AES.MODE_ECB)
    ct = cipher.encrypt(pad(plaintext.encode("utf-8"), AES.block_size))
    return ct.hex()


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


def _common_headers() -> dict:
    return {
        "User-Agent": UA,
        "Origin": "https://www.ilanzou.com",
        "Referer": "https://www.ilanzou.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    }


class ILanzouProvider(Provider):
    platform = "ilanzou"
    display_name = "蓝奏云优享版"

    # ---------- 解析 ----------

    async def resolve(self, share_id: str, pwd: Optional[str]) -> tuple[ShareInfo, dict]:
        if pwd:
            raise ProviderError(
                "蓝奏云优享版带提取码的分享暂不支持解析（匿名接口没有传提取码的位置）"
            )
        client = self.new_client(headers={"User-Agent": UA})
        session: dict = {"shareId": share_id, "client": client}
        try:
            uuid = await self._get_uuid(client)
            session["uuid"] = uuid
            files = await self._list(client, session, "", 1)
            if not files:
                raise ProviderError("该优享版分享不存在或为空（未列出任何文件）")
            return (
                ShareInfo(
                    platform=self.platform,
                    platform_name=self.display_name,
                    share_id=share_id,
                    title="",
                    files=files,
                ),
                session,
            )
        except Exception:
            await client.aclose()
            raise

    async def list_dir(self, session: dict, dir_ref: str) -> list[ShareFile]:
        client: httpx.AsyncClient = session["client"]
        folder_id = dir_ref[2:] if dir_ref.startswith("d:") else dir_ref
        return await self._list(client, session, folder_id, 1)

    # ---------- 取链 ----------

    async def get_download_url(self, session: dict, file: ShareFile) -> DownloadLink:
        client: httpx.AsyncClient = session["client"]
        file_id = file.fid[2:] if file.fid.startswith("f:") else file.fid
        ts = str(_now_ms())
        params = {
            "downloadId": _aes_hex(f"{file_id}|"),
            "enable": "1",
            "devType": "6",
            "uuid": session["uuid"],
            "timestamp": _aes_hex(ts),
            "auth": _aes_hex(f"{file_id}|{ts}"),
            "shareId": session["shareId"],
        }
        url = f"{APIX_BASE}/unproved/file/redirect?" + urllib.parse.urlencode(params)
        r = await client.get(url, headers=_common_headers(), follow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("Location", "")
            if loc:
                return DownloadLink(url=urllib.parse.urljoin(APIX_BASE, loc))
        try:
            data = r.json()
        except Exception:
            raise ProviderError(f"优享版返回内容无法解析（HTTP {r.status_code}）")
        d = data.get("data") if isinstance(data.get("data"), dict) else data
        dl = (d or {}).get("url") or data.get("url")
        if not dl:
            raise ProviderError("优享版未返回下载地址")
        return DownloadLink(url=dl)

    # ---------- 内部 ----------

    async def _get_uuid(self, client: httpx.AsyncClient) -> str:
        ts = str(_now_ms())
        params = {
            "uuid": "",
            "devType": "6",
            "devCode": "",
            "devModel": "chrome",
            "devVersion": "125",
            "appVersion": "",
            "timestamp": _aes_hex(ts),
            "extra": "2",
        }
        url = f"{APIS_BASE}/unproved/getUuid?" + urllib.parse.urlencode(params)
        r = await client.get(url, headers=_common_headers())
        data = self._as_json(r)
        uuid = data.get("uuid", "")
        if not uuid:
            raise ProviderError("优享版未返回设备标识")
        return uuid

    async def _list(
        self, client: httpx.AsyncClient, session: dict, folder_id: str, page: int
    ) -> list[ShareFile]:
        ts = str(_now_ms())
        params = {
            "uuid": session["uuid"],
            "devType": "6",
            "devCode": session["uuid"],
            "devModel": "Chrome",
            "devVersion": "125",
            "appVersion": "",
            "timestamp": _aes_hex(ts),
            "extra": "2",
            "shareId": session["shareId"],
            "folderId": folder_id,
            "offset": str(page),
            "limit": "60",
        }
        url = f"{APIX_BASE}/unproved/share/list?" + urllib.parse.urlencode(params)
        r = await client.get(url, headers=_common_headers())
        data = self._as_json(r)
        if data.get("code") != 200:
            raise ProviderError(data.get("msg") or f"优享版接口返回错误（code={data.get('code')}）")
        files: list[ShareFile] = []
        for item in data.get("list") or []:
            if item.get("fileType") == 2:
                files.append(
                    ShareFile(
                        fid="d:" + str(item.get("folderId", "")),
                        name=item.get("folderName", ""),
                        size=0,
                        is_dir=True,
                        modify_time=str(item.get("updTime") or item.get("addTime") or ""),
                    )
                )
            else:
                files.append(
                    ShareFile(
                        fid="f:" + str(item.get("fileId", "")),
                        name=item.get("fileName", ""),
                        size=int(item.get("fileSize") or 0) * 1024,
                        is_dir=False,
                        modify_time=str(item.get("updTime") or item.get("addTime") or ""),
                    )
                )
        total_page = int(data.get("totalPage") or 1)
        if page < total_page and page < 1000:
            files.extend(await self._list(client, session, folder_id, page + 1))
        return files

    @staticmethod
    def _as_json(r: httpx.Response) -> dict:
        try:
            data = r.json()
        except Exception:
            raise ProviderError(f"优享版返回了非 JSON 内容（HTTP {r.status_code}），可能是风控页")
        if not isinstance(data, dict):
            raise ProviderError("优享版返回格式异常")
        return data
