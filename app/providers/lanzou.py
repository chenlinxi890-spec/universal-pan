"""通用网盘 - 蓝奏云提供方（游客模式）。

流程（端口自 YunX-Desktop LanzouApi.kt 的游客链路）：
    GET 分享页 HTML（手动跟随跳转 ≤8 次，host 必须仍是蓝奏云域名；
    遇 acw_sc__v2 人机校验页时本地算 Cookie 后重放）
    → 文件夹：POST {origin}/filemoreajax.php（lx/fid/t/k/pwd/pg）
    → 取链：GET iframe 下载页 → POST ajax.php downprocess（sign；有码 p=码，
       无码 signs/websignkey/websign/kd/ves）→ dom + url 拼跳转地址
    → HEAD 探测/验证直链（Cookie down_ip=1；遇「验证并下载」确认页再 POST 一次）

直链下载时带 Cookie: down_ip=1 + Referer: {分享页 origin}/。
"""
from __future__ import annotations

import asyncio
import re
import urllib.parse
from typing import Optional

import httpx

from ..models import ProviderError, ShareFile, ShareInfo
from .base import DownloadLink, Provider

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HOST_RE = re.compile(r"(lanzou[a-z]?|lan[zs]o[ux])\.(com|net|org|cn)$", re.I)
ACW_POS = [
    15, 35, 29, 24, 33, 16, 1, 38, 10, 9, 19, 31, 40, 27, 22, 23, 25, 13, 6, 11,
    39, 18, 20, 8, 14, 21, 32, 26, 2, 30, 7, 4, 17, 5, 3, 28, 34, 37, 12, 36,
]
ACW_MASK = "3000176000856006061501533003690027800375"


def _acw_cookie(arg1: str) -> str:
    """acw_sc__v2：arg1 按位置表重排，再与固定掩码逐字节异或。"""
    rearranged = "".join(arg1[p - 1] for p in ACW_POS)
    a = bytes.fromhex(rearranged)
    b = bytes.fromhex(ACW_MASK)
    return bytes(x ^ y for x, y in zip(a, b)).hex()


def _parse_size(text: str) -> int:
    m = re.search(r"([\d.]+)\s*([KMGT])", text or "", re.I)
    if not m:
        return 0
    num = float(m.group(1))
    unit = m.group(2).upper()
    mult = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}[unit]
    return int(num * mult)


class LanzouProvider(Provider):
    platform = "lanzou"
    display_name = "蓝奏云"

    # ---------- 解析 ----------

    async def resolve(self, share_id: str, pwd: Optional[str]) -> tuple[ShareInfo, dict]:
        # 蓝奏云域名不定，linkparse 把完整分享链接存在 share_id 里
        url = share_id
        client = self.new_client(headers={"User-Agent": UA})
        session: dict = {"url": url, "pwd": pwd or "", "client": client}
        try:
            page = await self._get_page(client, url)
            session["origin"] = self._origin(url)
            session["page"] = page
            title = self._extract_title(page)
            session["title"] = title
            if "文件已取消" in page or "不存在" in page:
                raise ProviderError("该蓝奏云分享不存在或已被取消")
            if self._needs_password(page):
                if not pwd:
                    raise ProviderError("该蓝奏云分享需要提取码")
            if self._is_folder(page):
                files = await self._list_folder(client, session, 1)
                # 分页
                pg = 2
                while pg <= 500:
                    batch = await self._list_folder(client, session, pg)
                    if not batch:
                        break
                    seen = {f.fid for f in files}
                    new = [f for f in batch if f.fid not in seen]
                    if not new:
                        break
                    files.extend(new)
                    pg += 1
            else:
                fid = self._extract_file_id(page)
                size = 0
                m = re.search(r"文件大小[：:]\s*([^\s<]+(?:\s*[KMGT])?)", page)
                if m:
                    size = _parse_size(m.group(1))
                files = [ShareFile(fid=fid or "", name=title or "file", size=size, is_dir=False)]
            return (
                ShareInfo(
                    platform=self.platform,
                    platform_name=self.display_name,
                    share_id=url,
                    title=title,
                    files=files,
                ),
                session,
            )
        except Exception:
            await client.aclose()
            raise

    async def list_dir(self, session: dict, dir_ref: str) -> list[ShareFile]:
        raise ProviderError("蓝奏云分享的子目录暂不支持下钻，只能下载顶层文件")

    # ---------- 取链 ----------

    async def get_download_url(self, session: dict, file: ShareFile) -> DownloadLink:
        client: httpx.AsyncClient = session["client"]
        url: str = session["url"]
        origin = session["origin"]
        pwd = session.get("pwd", "")
        page = session.get("page", "")
        # 1. 取 iframe 下载页
        iframe = self._extract_iframe(page)
        dl_page = page
        if iframe:
            iframe_abs = urllib.parse.urljoin(url, iframe)
            host = urllib.parse.urlparse(iframe_abs).hostname or ""
            if not HOST_RE.search(host):
                raise ProviderError("蓝奏云下载页跳转到了站外地址，已中止")
            dl_page = await self._get_page(client, iframe_abs)
        file_id = self._extract_file_id(dl_page) or file.fid
        if not file_id:
            raise ProviderError("蓝奏云未找到文件 ID，分享页结构可能已变化")
        # 2. 确定 ajax URL 与签名
        ajax_url = self._find_ajax_url(dl_page, origin, bool(pwd), file_id)
        sign = self._extract_js_var(dl_page, "wp_sign") or self._extract_js_var(dl_page, "sign")
        if not sign:
            m = re.search(r'<input[^>]*name=["\']sign["\'][^>]*value=["\']([^"\']+)', dl_page)
            sign = m.group(1) if m else ""
        if not sign:
            raise ProviderError("蓝奏云未找到下载签名，分享页结构可能已变化")
        # 3. downprocess
        form: dict[str, str] = {"action": "downprocess", "sign": sign}
        if pwd:
            form["p"] = pwd
            form["kd"] = "1"
        else:
            form["signs"] = self._extract_js_var(dl_page, "signs") or self._extract_js_var(dl_page, "ajaxdata") or ""
            form["websignkey"] = self._extract_js_var(dl_page, "ajaxdata") or self._extract_js_var(dl_page, "websignkey") or ""
            form["websign"] = self._extract_js_var(dl_page, "websign") or ""
            form["kd"] = self._extract_js_var(dl_page, "kdns", quoted=False) or "1"
            form["ves"] = "1"
        r = await client.post(
            ajax_url,
            data=form,
            headers={
                "User-Agent": UA,
                "Referer": url,
                "Origin": origin,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        data = self._as_json(r)
        if data.get("zt") != 1:
            inf = str(data.get("inf", ""))
            if any(k in inf for k in ("密码", "提取码")):
                raise ProviderError("蓝奏云提取码错误")
            if any(k in inf for k in ("不存在", "取消", "过期", "删除")):
                raise ProviderError("该蓝奏云分享不存在或已失效")
            if any(k in inf for k in ("频繁", "次数", "稍后")):
                raise ProviderError("蓝奏云请求过于频繁，请稍后重试")
            raise ProviderError(f"蓝奏云取链失败：{inf or data}")
        dom = str(data.get("dom", ""))
        rel = str(data.get("url", ""))
        if not dom or not rel or rel.startswith("/") or "://" in rel:
            raise ProviderError("蓝奏云返回的下载地址无效")
        parsed = urllib.parse.urlparse(dom)
        jump = f"{parsed.scheme}://{parsed.hostname}/file/{rel}"
        # 4. 探测/验证直链
        final_url, size = await self._probe(client, jump, origin)
        return DownloadLink(
            url=final_url,
            headers={"User-Agent": UA, "Referer": f"{origin}/"},
            cookies={"down_ip": "1"},
        )

    # ---------- 内部 ----------

    async def _get_page(self, client: httpx.AsyncClient, url: str) -> str:
        """GET 页面，手动跟随跳转（≤8 次，host 须仍是蓝奏云域名），处理 acw 校验。"""
        cur = url
        for _ in range(8):
            r = await client.get(
                cur,
                headers={"User-Agent": UA, "Referer": cur},
                follow_redirects=False,
            )
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location", "")
                if not loc:
                    break
                nxt = urllib.parse.urljoin(cur, loc)
                host = urllib.parse.urlparse(nxt).hostname or ""
                if not HOST_RE.search(host):
                    # 无效分享 ID 会被蓝奏云 302 到上游域名 up.woozooo.com（首页）
                    if host.endswith("woozooo.com"):
                        raise ProviderError("该蓝奏云分享不存在或已失效")
                    raise ProviderError("分享链接跳转到了站外地址，已中止")
                cur = nxt
                continue
            text = r.text
            # acw_sc__v2 校验页
            if "acw_sc__v2" in text:
                m = re.search(r"arg1\s*=\s*['\"]([0-9a-fA-F]{40})['\"]", text)
                if m:
                    client.cookies.set(
                        "acw_sc__v2", _acw_cookie(m.group(1)),
                        domain=urllib.parse.urlparse(cur).hostname or "",
                    )
                    r2 = await client.get(cur, headers={"User-Agent": UA, "Referer": cur}, follow_redirects=False)
                    if r2.status_code == 200:
                        return r2.text
                    raise ProviderError("蓝奏云人机校验未通过，请稍后重试")
                raise ProviderError("蓝奏云人机校验未通过，请稍后重试")
            return text
        raise ProviderError("蓝奏云分享链接跳转次数过多或无效")

    @staticmethod
    def _origin(url: str) -> str:
        p = urllib.parse.urlparse(url)
        return f"{p.scheme}://{p.netloc}"

    @staticmethod
    def _extract_title(page: str) -> str:
        m = re.search(r"<title>(.*?)</title>", page, re.S)
        if m:
            t = re.sub(r"\s*-\s*蓝奏云\s*$", "", m.group(1).strip())
            t = t.replace("蓝奏云", "").strip()
            if t:
                return t
        m = re.search(r'class="n"[^>]*>([^<]+)<', page)
        if m:
            return m.group(1).strip()
        m = re.search(r"var\s+filename\s*=\s*'([^']+)'", page)
        if m:
            return m.group(1)
        return ""

    @staticmethod
    def _needs_password(page: str) -> bool:
        return ("请输入密码" in page) or ("pwbox" in page) or ("input_password" in page) or ("密码不正确" in page)

    @staticmethod
    def _is_folder(page: str) -> bool:
        return ("filemoreajax" in page) or ('class="filemore"' in page) or ("file_more" in page)

    @staticmethod
    def _extract_iframe(page: str) -> str:
        m = re.search(r'<iframe[^>]*src=["\']([^"\']+)', page)
        return m.group(1) if m else ""

    @staticmethod
    def _extract_file_id(page: str) -> str:
        m = re.search(r"(?:ajaxm|ajaxfile)\.php\?file=(\d+)", page)
        if m:
            return m.group(1)
        m = re.search(r'<input[^>]*name=["\']file["\'][^>]*value=["\'](\d+)', page)
        if m:
            return m.group(1)
        m = re.search(r"file=(\d+)", page)
        return m.group(1) if m else ""

    @staticmethod
    def _extract_js_var(page: str, name: str, quoted: bool = True) -> str:
        if quoted:
            m = re.search(rf"var\s+{re.escape(name)}\s*=\s*['\"]([^'\"]*)['\"]", page)
            if m:
                return m.group(1)
            m = re.search(rf"{re.escape(name)}\s*=\s*['\"]([^'\"]*)['\"]", page)
            return m.group(1) if m else ""
        m = re.search(rf"var\s+{re.escape(name)}\s*=\s*(\d+)", page)
        return m.group(1) if m else ""

    async def _list_folder(self, client: httpx.AsyncClient, session: dict, pg: int) -> list[ShareFile]:
        page = session["page"]
        origin = session["origin"]
        url = session["url"]
        lx = self._extract_js_var(page, "lx")
        fid = self._extract_js_var(page, "fid") or urllib.parse.urlparse(url).path.strip("/").split("/")[-1]
        t = self._extract_js_var(page, "t")
        k = self._extract_js_var(page, "k")
        form: dict[str, str] = {
            "lx": lx, "fid": fid, "t": t, "k": k,
            "pwd": session.get("pwd", ""), "pg": str(pg),
        }
        last_err = None
        for attempt in range(3):
            r = await client.post(
                f"{origin}/filemoreajax.php",
                data=form,
                headers={
                    "User-Agent": UA,
                    "Referer": url,
                    "Origin": origin,
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            data = self._as_json(r)
            zt = data.get("zt")
            if zt == 1:
                out: list[ShareFile] = []
                for item in data.get("text") or []:
                    name = item.get("name_all") or item.get("name", "")
                    out.append(
                        ShareFile(
                            fid=str(item.get("id", "")),
                            name=name,
                            size=_parse_size(str(item.get("size", ""))),
                            is_dir=False,
                            modify_time=str(item.get("time", "")),
                        )
                    )
                return out
            if zt == 2:
                return []
            if zt == 3:
                raise ProviderError("蓝奏云提取码错误")
            if zt == 4:
                last_err = ProviderError("蓝奏云请求过于频繁，请稍后重试")
                await asyncio.sleep(2)
                continue
            raise ProviderError(f"蓝奏云列表接口返回异常（zt={zt}）")
        raise last_err or ProviderError("蓝奏云请求过于频繁，请稍后重试")

    def _find_ajax_url(self, page: str, origin: str, has_pwd: bool, file_id: str) -> str:
        for var in ("domain1", "domain2"):
            v = self._extract_js_var(page, var)
            if v and "/ajax" in v:
                return v
        m = re.search(r"(https?://[^\"'\s]+(?:ajaxm|ajaxfile)\.php\?file=\d+)", page)
        if m:
            return m.group(1)
        if has_pwd:
            return f"{origin}/ajaxfile.php?file={file_id}"
        return f"{origin}/ajaxm.php?file={file_id}"

    async def _probe(self, client: httpx.AsyncClient, url: str, origin: str) -> tuple[str, int]:
        """HEAD 探测直链，跟随 3xx；遇确认页则 POST 验证一次。"""
        cur = url
        for _ in range(8):
            r = await client.head(
                cur,
                headers={"User-Agent": UA, "Referer": f"{origin}/", "Cookie": "down_ip=1"},
                follow_redirects=False,
            )
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location", "")
                if not loc:
                    raise ProviderError("蓝奏云下载地址跳转异常")
                cur = urllib.parse.urljoin(cur, loc)
                continue
            if r.status_code in (200, 206):
                size = 0
                cr = r.headers.get("Content-Range", "")
                if "/" in cr:
                    try:
                        size = int(cr.rsplit("/", 1)[1])
                    except ValueError:
                        pass
                if not size:
                    try:
                        size = int(r.headers.get("Content-Length") or 0)
                    except ValueError:
                        pass
                ct = r.headers.get("Content-Type", "")
                if "text/html" in ct:
                    # 确认页：GET 正文再处理
                    return await self._handle_confirm(client, cur, origin)
                return cur, size
            if r.status_code in (403, 404, 410):
                raise ProviderError("蓝奏云下载地址已失效，请重新解析")
            if r.status_code == 429:
                raise ProviderError("蓝奏云请求过于频繁，请稍后重试")
            # HEAD 无大小/无类型时回退 GET Range 探测
            r2 = await client.get(
                cur,
                headers={"User-Agent": UA, "Referer": f"{origin}/", "Cookie": "down_ip=1", "Range": "bytes=0-8191"},
                follow_redirects=False,
            )
            if r2.status_code in (200, 206):
                ct = r2.headers.get("Content-Type", "")
                if "text/html" in ct:
                    return await self._handle_confirm(client, cur, origin, html=r2.text)
                size = 0
                cr = r2.headers.get("Content-Range", "")
                if "/" in cr:
                    try:
                        size = int(cr.rsplit("/", 1)[1])
                    except ValueError:
                        pass
                return cur, size
            raise ProviderError(f"蓝奏云下载地址探测失败（HTTP {r2.status_code}）")
        raise ProviderError("蓝奏云下载地址跳转次数过多")

    async def _handle_confirm(
        self, client: httpx.AsyncClient, url: str, origin: str, html: str = ""
    ) -> tuple[str, int]:
        if not html:
            r = await client.get(
                url, headers={"User-Agent": UA, "Referer": f"{origin}/", "Cookie": "down_ip=1"}
            )
            html = r.text
        if "acw_sc__v2" in html:
            m = re.search(r"arg1\s*=\s*['\"]([0-9a-fA-F]{40})['\"]", html)
            if m:
                client.cookies.set(
                    "acw_sc__v2", _acw_cookie(m.group(1)),
                    domain=urllib.parse.urlparse(url).hostname or "",
                )
                return await self._probe(client, url, origin)
            raise ProviderError("蓝奏云人机校验未通过，请稍后重试")
        # 「验证并下载」确认页
        m_file = re.search(r"['\"]file['\"]\s*:\s*['\"]([A-Za-z0-9+/=]+)['\"]", html)
        m_sign = re.search(r"['\"]sign['\"]\s*:\s*['\"]([^'\"]+)['\"]", html)
        if not m_file or not m_sign:
            raise ProviderError("蓝奏云下载需要人工验证，无法自动下载")
        await asyncio.sleep(2)
        node = f"{urllib.parse.urlparse(url).scheme}://{urllib.parse.urlparse(url).netloc}"
        r = await client.post(
            f"{node}/ajax.php",
            data={"file": m_file.group(1), "el": "2", "sign": m_sign.group(1)},
            headers={"User-Agent": UA, "Referer": f"{origin}/", "Cookie": "down_ip=1"},
        )
        data = self._as_json(r)
        if data.get("zt") != 1 or not data.get("url"):
            if data.get("url") == "?SignError":
                raise ProviderError("蓝奏云验证已过期，请重新解析")
            raise ProviderError("蓝奏云下载验证失败")
        return data["url"], 0

    @staticmethod
    def _as_json(r: httpx.Response) -> dict:
        try:
            data = r.json()
        except Exception:
            raise ProviderError(f"蓝奏云返回了非 JSON 内容（HTTP {r.status_code}），可能是风控页")
        if not isinstance(data, dict):
            raise ProviderError("蓝奏云返回格式异常")
        return data
