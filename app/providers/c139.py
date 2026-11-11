"""通用网盘 - 139云盘（和彩云）提供方（游客模式：只列文件，下载需登录）。

流程（端口自 YunX-Desktop C139Api.kt 的游客链路）：
    POST getOutLinkGeneral（匿名，取标题；服务端还会明文回吐提取码）
    → POST getOutLinkInfoV6（匿名，列目录；caLst 子文件夹 + coLst 文件）

传输加密 AES/CBC/PKCS5Padding，固定密钥 PVGDwmcvfs1uV3d1，
每次随机 16 字节 IV，密文格式 base64(IV ‖ 密文)；响应同样解密，
解密后若为 gzip（0x1f 0x8b）先解压。

游客取下载地址在代码里不存在：dlFromOutLinkV3 要求登录账号（完整手机号），
仓库层无登录即拦截。因此 get_download_url 明确报错，不编造直链。
"""
from __future__ import annotations

import base64
import gzip
import json
import secrets
from typing import Optional

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from ..models import ProviderError, ShareFile, ShareInfo
from .base import DownloadLink, Provider

SHARE_HOST = "https://share-kd-njs.yun.139.com/yun-share/richlifeApp/devapp/IOutLink"
AES_KEY = b"PVGDwmcvfs1uV3d1"
UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Mobile Safari/537.36"
)
HEADERS = {
    "hcy-cool-flag": "1",
    "x-deviceinfo": "||3|12.27.0|||||chrome 150.0.0.0|360X444|zh-cn|||",
    "x-huawei-channelsrc": "10245500",
    "x-mm-source": "0002",
    "Content-Type": "application/json;charset=UTF-8",
    "User-Agent": UA,
    "Origin": "https://yun.139.com",
    "Referer": "https://yun.139.com/",
    "Accept": "application/json, text/plain, */*",
}


def _encrypt(plaintext: str) -> str:
    iv = secrets.token_bytes(16)
    cipher = AES.new(AES_KEY, AES.MODE_CBC, iv)
    ct = cipher.encrypt(pad(plaintext.encode("utf-8"), AES.block_size))
    return base64.b64encode(iv + ct).decode()


def _decrypt(b64: str) -> str:
    raw = base64.b64decode(b64)
    iv, ct = raw[:16], raw[16:]
    cipher = AES.new(AES_KEY, AES.MODE_CBC, iv)
    d = unpad(cipher.decrypt(ct), AES.block_size)
    if len(d) > 2 and d[0] == 0x1F and d[1] == 0x8B:
        d = gzip.decompress(d)
    return d.decode("utf-8")


class C139Provider(Provider):
    platform = "c139"
    display_name = "139云盘"
    guest_download = False  # 游客只能列文件，下载需登录账号

    # ---------- 解析 ----------

    async def resolve(self, share_id: str, pwd: Optional[str]) -> tuple[ShareInfo, dict]:
        client = self.new_client(headers={"User-Agent": UA})
        session: dict = {"linkID": share_id, "pwd": pwd or "", "client": client}
        try:
            title = ""
            try:
                general = await self._share_post(
                    client, f"{SHARE_HOST}/getOutLinkGeneral",
                    {"getOutLinkGeneralReq": {"linkID": share_id, "isPasswd": 1, "account": ""}},
                )
                arr = (general.get("data") or {}).get("getOutLinkGeneralResp", {}).get("outLinkGeneral") or []
                if arr:
                    title = arr[0].get("lkName", "")
                    # 服务端明文回吐提取码：用户没给提取码时自动补
                    if not pwd and arr[0].get("passwd"):
                        session["pwd"] = arr[0]["passwd"]
            except ProviderError:
                pass
            files = await self._list(client, session, "root")
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
            "139云盘的分享下载需要登录账号（完整手机号）后才能取直链，"
            "本工具暂不支持登录，请用139云盘客户端下载该分享"
        )

    # ---------- 内部 ----------

    async def _share_post(
        self, client: httpx.AsyncClient, url: str, plain: dict
    ) -> dict:
        body = _encrypt(json.dumps(plain, separators=(",", ":"), ensure_ascii=False))
        r = await client.post(url, content=body, headers=HEADERS)
        text = r.text.strip()
        try:
            data = json.loads(_decrypt(text))
        except Exception:
            try:
                data = json.loads(text)
            except Exception:
                raise ProviderError(f"139 返回内容无法解析（HTTP {r.status_code}）")
        if not isinstance(data, dict):
            raise ProviderError("139 返回格式异常")
        result_code = str(data.get("resultCode", ""))
        if result_code and result_code != "0":
            desc = data.get("desc", "")
            if result_code == "9188":
                raise ProviderError("提取码错误", "9188")
            if result_code == "9530":
                raise ProviderError("139 接口请求失败（9530），请稍后重试", "9530")
            raise ProviderError(f"139 接口返回错误：{desc or result_code}", result_code)
        if data.get("success") is False:
            raise ProviderError(data.get("desc") or "获取文件列表失败")
        return data

    async def _list(
        self, client: httpx.AsyncClient, session: dict, pca_id: str
    ) -> list[ShareFile]:
        files: list[ShareFile] = []
        begin = 1
        while True:
            plain = {
                "getOutLinkInfoReq": {
                    "account": "",
                    "linkID": session["linkID"],
                    "passwd": session.get("pwd", ""),
                    "caSrt": 1,
                    "coSrt": 1,
                    "srtDr": 0,
                    "bNum": begin,
                    "pCaID": pca_id,
                    "eNum": begin + 199,
                }
            }
            data = await self._share_post(client, f"{SHARE_HOST}/getOutLinkInfoV6", plain)
            d = data.get("data") or {}
            batch = 0
            for item in d.get("caLst") or []:
                files.append(
                    ShareFile(
                        fid=str(item.get("caID", "")),
                        name=item.get("caName", ""),
                        size=0,
                        is_dir=True,
                        path=pca_id,
                        modify_time=str(item.get("udTime") or item.get("ctTime") or ""),
                    )
                )
                batch += 1
            for item in d.get("coLst") or []:
                isdir = bool(item.get("isdir", item.get("coType") == 2))
                files.append(
                    ShareFile(
                        fid=str(item.get("coID", "")),
                        name=item.get("coName", ""),
                        size=int(item.get("coSize") or 0),
                        is_dir=isdir,
                        path=pca_id,
                        modify_time=str(item.get("udTime") or item.get("ctTime") or ""),
                    )
                )
                batch += 1
            if batch < 200 or begin > 20000:
                break
            begin += 200
        return files
