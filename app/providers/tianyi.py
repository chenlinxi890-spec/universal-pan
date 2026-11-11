"""通用网盘 - 天翼云盘（189）提供方（游客模式：只列文件，下载需登录）。

流程（公开接口知识；LinkSwift 油猴脚本只给出登录态取链，未用其代码）：
    GET /open/share/getShareInfoByCode.action?shareCode=xxx（匿名，
    分享不存在返回 XML <error><code>ShareInfoNotFound</code>）
    → 私密分享（shareMode=2）时 GET /open/share/checkAccessCode.action
      ?shareId=..&accessCode=..（访问码即提取码）
    → GET /open/share/listShareDir.action?shareId=..&fileId=..&pageNum=..&pageSize=..

游客取下载地址在公开链路里不存在：取链接口
/open/file/getFileDownloadUrl.action 要求登录态 accessToken +
MD5 签名（Sign-Type: 1 / Accesstoken / Timestamp / Signature），
无签名请求直接返回 {"errorCode":"InvalidArgument",
"errorMsg":"date/signature is null"}；LinkSwift 本身在未登录时也只会
提示“请先登录网盘~”。因此 get_download_url 明确报错，不编造直链。

注意：天翼的错误响应常为 XML（<error><code>..</code><message>..</message></error>），
成功响应为 JSON；两者都要能解析。
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Optional

import httpx

from ..models import ProviderError, ShareFile, ShareInfo
from .base import DownloadLink, Provider

API_BASE = "https://cloud.189.cn/api/open"
REFERER = "https://cloud.189.cn/"
# 分享根目录的 fileId 由 getShareInfoByCode 返回；list_dir 收到 "/" 时回落到它
ROOT_REF = "/"

_XML_CODE_RE = re.compile(r"<code>(.*?)</code>", re.S)
_XML_MSG_RE = re.compile(r"<message>(.*?)</message>", re.S)


class TianyiProvider(Provider):
    platform = "tianyi"
    display_name = "天翼云盘"
    guest_download = False  # 游客只能列文件，下载需登录账号取签名直链

    # ---------- 解析 ----------

    async def resolve(self, share_id: str, pwd: Optional[str]) -> tuple[ShareInfo, dict]:
        client = self.new_client()
        session: dict = {"share_code": share_id, "pwd": pwd or "", "client": client}
        try:
            info = await self._get_share_info(client, share_id)
            share_id_num = info.get("shareId")
            file_id = str(info.get("fileId") or "")
            if share_id_num is None or not file_id:
                raise ProviderError("天翼云盘未返回分享信息，分享可能已失效")
            session["share_id"] = str(share_id_num)
            session["root_file_id"] = file_id
            # shareMode：1 公开，2 私密（需访问码）
            if int(info.get("shareMode") or 1) == 2:
                if not pwd:
                    raise ProviderError(
                        "该天翼分享是私密分享，需要访问码（提取码），请提供后重试"
                    )
                checked = await self._check_access_code(
                    client, str(share_id_num), pwd
                )
                if checked.get("fileId"):
                    session["root_file_id"] = str(checked["fileId"])
            files = await self._list(client, session, session["root_file_id"])
            return (
                ShareInfo(
                    platform=self.platform,
                    platform_name=self.display_name,
                    share_id=share_id,
                    title=str(info.get("fileName") or ""),
                    files=files,
                ),
                session,
            )
        except Exception:
            await client.aclose()
            raise

    async def list_dir(self, session: dict, dir_ref: str) -> list[ShareFile]:
        client: httpx.AsyncClient = session["client"]
        file_id = dir_ref
        if file_id in ("", ROOT_REF, "root"):
            file_id = session.get("root_file_id", "")
        return await self._list(client, session, file_id)

    # ---------- 取链（游客不可用） ----------

    async def get_download_url(self, session: dict, file: ShareFile) -> DownloadLink:
        raise ProviderError(
            "天翼云盘的分享下载需要登录账号后才能取直链"
            "（取链接口要求登录态 accessToken + 签名），"
            "本工具暂不支持登录，请用天翼云盘客户端下载该分享"
        )

    # ---------- 内部 ----------

    def _headers(self) -> dict:
        return {"Referer": REFERER, "Accept": "application/json"}

    async def _get_share_info(
        self, client: httpx.AsyncClient, share_code: str
    ) -> dict:
        url = (
            f"{API_BASE}/share/getShareInfoByCode.action?"
            + urllib.parse.urlencode({"shareCode": share_code})
        )
        r = await client.get(url, headers=self._headers())
        return self._as_json(r)

    async def _check_access_code(
        self, client: httpx.AsyncClient, share_id: str, access_code: str
    ) -> dict:
        url = (
            f"{API_BASE}/share/checkAccessCode.action?"
            + urllib.parse.urlencode(
                {"shareId": share_id, "accessCode": access_code}
            )
        )
        r = await client.get(url, headers=self._headers())
        data = self._as_json(r)
        if str(data.get("res_code")) not in ("0", "None"):
            raise ProviderError(
                f"访问码错误或分享已失效（{data.get('res_message') or data.get('res_code')}）",
                str(data.get("res_code") or ""),
            )
        return data

    async def _list(
        self, client: httpx.AsyncClient, session: dict, file_id: str
    ) -> list[ShareFile]:
        files: list[ShareFile] = []
        page = 1
        while page <= 50:
            params = {
                "shareId": session["share_id"],
                "fileId": file_id,
                "pageNum": str(page),
                "pageSize": "60",
                "orderBy": "filename",
                "descending": "false",
            }
            url = f"{API_BASE}/share/listShareDir.action?" + urllib.parse.urlencode(params)
            r = await client.get(url, headers=self._headers())
            data = self._as_json(r)
            ao = data.get("fileListAO") or data
            for item in ao.get("folderList") or []:
                files.append(
                    ShareFile(
                        fid=str(item.get("id", "")),
                        name=item.get("name", ""),
                        size=0,
                        is_dir=True,
                        path=file_id,
                        modify_time=str(item.get("lastOpTime", "")),
                    )
                )
            for item in ao.get("fileList") or []:
                files.append(
                    ShareFile(
                        fid=str(item.get("id", "")),
                        name=item.get("name", ""),
                        size=int(item.get("size") or 0),
                        is_dir=False,
                        path=file_id,
                        modify_time=str(item.get("lastOpTime", "")),
                    )
                )
            count = ao.get("count")
            if count is None or len(files) >= int(count):
                break
            page += 1
        return files

    @staticmethod
    def _as_json(r: httpx.Response) -> dict:
        text = r.text.strip()
        if text.startswith("<?xml") or text.startswith("<error>"):
            code = _XML_CODE_RE.search(text)
            msg = _XML_MSG_RE.search(text)
            code_s = code.group(1).strip() if code else ""
            msg_s = msg.group(1).strip() if msg else ""
            raise ProviderError(
                TianyiProvider._xml_err(code_s, msg_s), code_s
            )
        try:
            data = r.json()
        except Exception:
            raise ProviderError(f"天翼云盘返回内容无法解析（HTTP {r.status_code}）")
        if not isinstance(data, dict):
            raise ProviderError("天翼云盘返回格式异常")
        res_code = data.get("res_code")
        if res_code is not None and str(res_code) != "0":
            raise ProviderError(
                str(data.get("res_message") or data.get("errorMsg") or "天翼云盘接口返回错误"),
                str(res_code),
            )
        err_code = str(data.get("errorCode") or "")
        if err_code:
            raise ProviderError(
                str(data.get("errorMsg") or err_code), err_code
            )
        return data

    @staticmethod
    def _xml_err(code: str, message: str) -> str:
        if code in ("ShareInfoNotFound", "ShareNotFound"):
            return "分享不存在或已失效"
        if code == "InvalidArgument":
            return f"天翼云盘接口参数无效（{message or code}），分享可能已失效"
        if code == "InvalidSessionKey":
            return "天翼云盘要求登录后才能继续"
        return message or f"天翼云盘接口返回错误（{code}）"
