"""通用网盘 - 百度网盘提供方（游客模式，无需登录）。

流程（端口自 YunX-Desktop BaiduApi.kt + LinkSwift 的百度分享逻辑）：
    分享页 GET（拿 BAIDUID 会话 Cookie）
    → POST /share/verify（提取码 → randsk，即 sekey；同时服务端下发 BDCLND）
    → GET /rest/2.0/xpan/share?method=list（文件列表；子目录需 BDCLND）
    → GET /share/tplconfig?fields=sign,timestamp（下载签名；取不到时从分享页 yunData 补）
    → POST /api/sharedownload（取 dlink 直链）

关键细节（2026-11-11 实测）：
    - verify 返回的 randsk 本身已是 URL 编码形式（%2B/%3D），BDCLND Cookie 与
      list 的 sekey 参数都用它原样；但 sharedownload 的 extra.sekey 必须用
      URL 解码后的值，否则报 errno=118「违规操作」。
    - tplconfig 与 sharedownload 都要带 logid = base64(BAIDUID)。
    - 免登录下载已被百度服务端拦住（2026-11-11 多轮实测）：sharedownload
      虽然返回 errno 0 与 dlink，但 dlink 的 dstime = 签名时间 - 3600、
      expires=1h，即签发即过期；用它下载一律 403
      {"error_code":31360,"error_msg":"expire time out error"}，
      与 UA/Cookie/Referer/clienttype/app_id 无关，各百度服务器时钟与本地时钟
      相差 <1s、非时钟问题；sign 绑定 tplconfig 时间戳，改时间戳报 113。
      filemetas 需登录（errno -6）。结论：游客只能解析列表，下载需登录。
      本工具如实报错，不走第三方代下服务。

红线：只用游客分享接口；不登录、不转存、不碰破解限速。百度非会员下载本身限速，
直链如实返回，不绕过。
"""
from __future__ import annotations

import base64
import json
import re
import urllib.parse
from typing import Optional

import httpx

from ..models import ProviderError, ShareFile, ShareInfo
from .base import UA_WEB, DownloadLink, Provider

APP_ID = "250528"
BASE = "https://pan.baidu.com"


class BaiduProvider(Provider):
    platform = "baidu"
    display_name = "百度网盘"
    # 2026-11-11 实测：免登录直链签发即过期（31360），下载需登录
    guest_download = False

    # ---------- 解析 ----------

    async def resolve(self, share_id: str, pwd: Optional[str]) -> tuple[ShareInfo, dict]:
        surl = share_id
        client = self.new_client()
        session: dict = {"surl": surl, "pwd": pwd or "", "client": client}
        try:
            # 1. 先 GET 分享页，拿会话 Cookie（BAIDUID）
            await self._get_share_page(client, surl)
            # 2. 验证提取码 → randsk（sekey）
            sekey = ""
            if pwd:
                sekey = await self._verify(client, surl, pwd)
                session["sekey"] = sekey
                # BDCLND = randsk；先清掉分享页已下发的同名 Cookie，
                # 否则 jar 里两个 BDCLND 会让 httpx 抛 CookieConflict
                for ck in list(client.cookies.jar):
                    if ck.name == "BDCLND":
                        client.cookies.delete(ck.name, domain=ck.domain, path=ck.path)
                client.cookies.set("BDCLND", sekey, domain="pan.baidu.com")
            # 3. 列根目录
            info = await self._list_share(client, session, "/")
            title = info["title"]
            files = info["files"]
            session.update({"uk": info["uk"], "shareid": info["shareid"]})
            # 4. 取下载签名（失败不致命，取链时再试）
            try:
                sign, ts = await self._get_sign(client, surl)
                session["sign"], session["timestamp"] = sign, ts
            except ProviderError:
                session["sign"], session["timestamp"] = "", ""
            return (
                ShareInfo(
                    platform=self.platform,
                    platform_name=self.display_name,
                    share_id=surl,
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
        info = await self._list_share(client, session, dir_ref)
        return info["files"]

    # ---------- 取链 ----------

    async def get_download_url(self, session: dict, file: ShareFile) -> DownloadLink:
        client: httpx.AsyncClient = session["client"]
        surl = session["surl"]
        if not session.get("sign"):
            sign, ts = await self._get_sign(client, surl)
            session["sign"], session["timestamp"] = sign, ts
        sign, ts = session["sign"], session["timestamp"]
        logid = self._logid(client)
        url = (
            f"{BASE}/api/sharedownload?channel=chunlei&clienttype=0&web=1"
            f"&app_id={APP_ID}&sign={sign}&timestamp={ts}&logid={logid}"
        )
        body: dict[str, str] = {
            "encrypt": "0",
            "product": "share",
            "uk": session["uk"],
            "primaryid": session["shareid"],
            "fid_list": json.dumps([int(file.fid)]),
        }
        if session.get("sekey"):
            # extra.sekey 必须用 URL 解码后的 randsk（原样传会报 errno=118）
            body["extra"] = json.dumps({"sekey": urllib.parse.unquote(session["sekey"])})
        r = await client.post(
            url,
            data=body,
            headers={
                "Referer": f"{BASE}/s/1{surl}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        data = self._as_json(r)
        errno = data.get("errno", 0)
        if errno != 0:
            raise ProviderError(self._err_text(data, errno, "获取下载地址失败"))
        lst = data.get("list")
        if isinstance(lst, str):
            # 服务端返回字符串：文件过大，需保存到网盘后才能下载
            raise ProviderError("该文件过大，百度要求先保存到网盘才能下载（游客直链不可用）")
        if not isinstance(lst, list) or not lst:
            raise ProviderError("百度未返回下载地址")
        dlink = lst[0].get("dlink", "")
        if not dlink:
            raise ProviderError("百度未返回下载地址")
        # 直接扫 jar（同名取最后一个），不用 .items()（同名会抛 CookieConflict）
        cookies: dict[str, str] = {}
        for ck in client.cookies.jar:
            cookies[ck.name] = ck.value
        return DownloadLink(
            url=dlink,
            headers={"User-Agent": UA_WEB, "Referer": f"{BASE}/s/1{surl}"},
            cookies=cookies or None,
        )

    # ---------- 内部 ----------

    async def _get_share_page(self, client: httpx.AsyncClient, surl: str) -> str:
        r = await client.get(
            f"{BASE}/s/1{surl}",
            headers={"Referer": f"{BASE}/"},
        )
        return r.text

    async def _verify(self, client: httpx.AsyncClient, surl: str, pwd: str) -> str:
        body = f"pwd={urllib.parse.quote(pwd)}&vcode_str=&vcode="
        r = await client.post(
            f"{BASE}/share/verify?surl={surl}",
            content=body,
            headers={
                "Referer": f"{BASE}/s/1{surl}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        data = self._as_json(r)
        errno = data.get("errno", 0)
        if errno != 0:
            if errno == -12:
                raise ProviderError("提取码错误", "-12")
            raise ProviderError(self._err_text(data, errno, "验证提取码失败"))
        randsk = data.get("randsk", "")
        if not randsk:
            raise ProviderError("验证提取码失败：未返回分享密钥")
        return randsk

    async def _list_share(
        self, client: httpx.AsyncClient, session: dict, dir_path: str
    ) -> dict:
        surl = session["surl"]
        sekey = session.get("sekey", "")
        is_root = not dir_path or dir_path == "/"
        root = "1" if is_root else "0"
        # sekey（randsk）本身已是 URL 编码形式，直接拼进 URL，不再二次编码
        sekey_part = f"&sekey={sekey}" if sekey else ""
        url = (
            f"{BASE}/rest/2.0/xpan/share?method=list"
            f"&shorturl={surl}&page=1&num=100&root={root}"
            f"&dir={urllib.parse.quote(dir_path if dir_path else '/', safe='')}"
            f"{sekey_part}"
        )
        r = await client.get(url, headers={"Referer": f"{BASE}/s/1{surl}"})
        data = self._as_json(r)
        errno = data.get("errno", 0)
        if errno != 0:
            if not sekey:
                if errno == -6:
                    raise ProviderError("该分享需要提取码，或需要登录百度网盘", "-6")
                raise ProviderError(f"该分享需要提取码（errno={errno}）", str(errno))
            raise ProviderError(self._err_text(data, errno, "获取分享文件列表失败"))
        files: list[ShareFile] = []
        for item in data.get("list") or []:
            isdir = str(item.get("isdir")) == "1"
            path = item.get("path", "")
            files.append(
                ShareFile(
                    fid=str(item.get("fs_id", "")) if not isdir else path,
                    name=item.get("server_filename", ""),
                    size=int(item.get("size") or 0),
                    is_dir=isdir,
                    path=path,
                    modify_time=str(item.get("server_mtime", "")),
                )
            )
        return {
            "title": data.get("title", ""),
            "shareid": str(data.get("share_id", "")),
            "uk": str(data.get("uk", "")),
            "files": files,
        }

    @staticmethod
    def _logid(client: httpx.AsyncClient) -> str:
        """logid = base64(BAIDUID)，tplconfig 与 sharedownload 都要带。"""
        baiduid = ""
        for ck in client.cookies.jar:
            if ck.name == "BAIDUID":
                baiduid = ck.value
        if not baiduid:
            return ""
        return base64.b64encode(baiduid.encode()).decode()

    async def _get_sign(self, client: httpx.AsyncClient, surl: str) -> tuple[str, str]:
        # 先走 tplconfig 接口（LinkSwift 同款）
        try:
            logid = self._logid(client)
            tpl = (
                f"{BASE}/share/tplconfig?fields=sign,timestamp&channel=chunlei"
                f"&web=1&app_id={APP_ID}&clienttype=0&view_mode=1&surl=1{surl}"
            )
            if logid:
                tpl += f"&logid={logid}"
            r = await client.get(
                tpl,
                headers={"Referer": f"{BASE}/s/1{surl}"},
            )
            data = self._as_json(r)
            d = data.get("data") or {}
            sign = d.get("sign", "")
            ts = str(d.get("timestamp", ""))
            if sign and ts:
                return sign, ts
        except Exception:
            pass
        # 回退：从分享页 yunData 里取
        html = await self._get_share_page(client, surl)
        m_sign = re.search(r'"sign"\s*:\s*"([^"]+)"', html)
        m_ts = re.search(r'"timestamp"\s*:\s*(\d+)', html)
        if m_sign and m_ts:
            return m_sign.group(1), m_ts.group(1)
        raise ProviderError("获取下载签名失败，百度接口可能已变化")

    @staticmethod
    def _as_json(r: httpx.Response) -> dict:
        try:
            data = r.json()
        except Exception:
            raise ProviderError(f"百度返回了非 JSON 内容（HTTP {r.status_code}），可能是风控页")
        if not isinstance(data, dict):
            raise ProviderError("百度返回格式异常")
        return data

    @staticmethod
    def _err_text(data: dict, errno: int, fallback: str) -> str:
        msg = data.get("err_msg") or data.get("show_msg") or ""
        if errno == 2:
            msg = msg or "分享不存在或已失效"
        elif errno == 112:
            msg = "页面已过期，请刷新后重试"
        elif errno == 9019:
            msg = "访问令牌已过期，请重新解析"
        return f"{msg or fallback}（errno={errno}）"
