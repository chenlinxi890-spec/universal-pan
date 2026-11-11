"""通用网盘 - 123盘提供方（游客模式：只列文件，下载需登录）。

流程（端口自 YunX-Desktop Pan123Api.kt 的游客链路）：
    GET https://yun.123pan.cn/b/api/share/get（匿名、无签名；
    仅带 User-Agent: Dart/3.12 (dart:io)）

游客取下载地址在代码里不存在：POST /b/api/share/download/info 要求
登录 token + CRC32 签名，token 为空即抛错。因此 get_download_url 明确报错，
不编造直链。
"""
from __future__ import annotations

import urllib.parse
from typing import Optional

import httpx

from ..models import ProviderError, ShareFile, ShareInfo
from .base import DownloadLink, Provider

LIST_URL = "https://yun.123pan.cn/b/api/share/get"
UA = "Dart/3.12 (dart:io)"


class Pan123Provider(Provider):
    platform = "pan123"
    display_name = "123盘"
    guest_download = False  # 游客只能列文件，下载需登录并签名

    # ---------- 解析 ----------

    async def resolve(self, share_id: str, pwd: Optional[str]) -> tuple[ShareInfo, dict]:
        client = self.new_client(headers={"User-Agent": UA})
        session: dict = {"shareKey": share_id, "pwd": pwd or "", "client": client}
        try:
            files = await self._list(client, session, "0")
            title = files[0].name if files else share_id
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

    # ---------- 取链（游客不可用） ----------

    async def get_download_url(self, session: dict, file: ShareFile) -> DownloadLink:
        raise ProviderError(
            "123盘的分享下载需要登录账号并签名，本工具暂不支持登录，"
            "请用123盘客户端下载该分享"
        )

    # ---------- 内部 ----------

    async def _list(
        self, client: httpx.AsyncClient, session: dict, parent_id: str
    ) -> list[ShareFile]:
        files: list[ShareFile] = []
        page = 1
        while page <= 50:
            params: dict[str, str] = {
                "limit": "100",
                "next": "0",
                "orderBy": "file_name",
                "orderDirection": "asc",
                "shareKey": session["shareKey"],
                "ParentFileId": parent_id,
                "Page": str(page),
            }
            if session.get("pwd"):
                params["SharePwd"] = session["pwd"]
            url = LIST_URL + "?" + urllib.parse.urlencode(params)
            r = await client.get(url, headers={"User-Agent": UA})
            data = self._as_json(r)
            if data.get("code") != 0:
                msg = data.get("message") or ""
                if data.get("code") == 400 or "Next" in msg:
                    raise ProviderError("123盘接口请求失败：提取码可能有误或分享已失效")
                raise ProviderError(f"123盘接口返回错误：{msg or data.get('code')}")
            d = data.get("data") or {}
            if d.get("Expired"):
                raise ProviderError("该分享已失效")
            for item in d.get("InfoList") or []:
                files.append(
                    ShareFile(
                        fid=str(item.get("FileId", "")),
                        name=item.get("FileName", ""),
                        size=int(item.get("Size") or 0),
                        is_dir=item.get("Type") == 1,
                        path=str(item.get("ParentFileId", "")),
                        fid_token="|".join(
                            [
                                str(item.get("S3KeyFlag", "")),
                                str(item.get("Etag", "")),
                                str(item.get("StorageNode", "")),
                            ]
                        ),
                        modify_time=str(item.get("UpdateAt", "")),
                    )
                )
            nxt = d.get("Next")
            if nxt == "-1" or not d.get("InfoList"):
                break
            page += 1
        return files

    @staticmethod
    def _as_json(r: httpx.Response) -> dict:
        try:
            data = r.json()
        except Exception:
            raise ProviderError(f"123盘返回了非 JSON 内容（HTTP {r.status_code}），可能是风控页")
        if not isinstance(data, dict):
            raise ProviderError("123盘返回格式异常")
        return data
