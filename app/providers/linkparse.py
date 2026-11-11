"""通用网盘 - 分享链接识别（端口自 YunX-Desktop 的 ShareLinkParser.kt）。

从一段文字（链接或分享文案）里找出 URL，识别平台、提取 share_id 与提取码。
提取码来源优先级：链接里的 pwd=/password= 参数 > 文案里的“提取码/访问码/密码：xxxx”。

平台：quark 夸克、uc UC、xunlei 迅雷、baidu 百度、c139 139云、pan123 123盘、
pan115 115、guangya 光鸭、aliyun 阿里云盘、tianyi 天翼云盘、
ilanzou 蓝奏云优享版、lanzou 蓝奏云。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

URL_RE = re.compile(r"https?://[^\s]+")
QUARK_RE = re.compile(r"pan\.quark\.cn/s/([A-Za-z0-9]+)", re.I)
UC_RE = re.compile(r"drive\.uc\.cn/s/([A-Za-z0-9]+)", re.I)
XUNLEI_RE = re.compile(r"pan\.xunlei\.com/s/([A-Za-z0-9_-]+)", re.I)
BAIDU_RE = re.compile(r"pan\.baidu\.com/s/(1[A-Za-z0-9_-]+)", re.I)
C139_RE = re.compile(r"yun\.139\.com/shareweb/.*?/w/i/([A-Za-z0-9_-]+)", re.I)
PAN123_RE = re.compile(r"123(?:865|pan)\.(?:com|cn)/s/([A-Za-z0-9]+-[A-Za-z0-9]+)", re.I)
PAN123_SUB_RE = re.compile(r"share\.123pan\.cn/123pan/([A-Za-z0-9-]+)", re.I)
PAN123_SRR_RE = re.compile(r"api/srr\?sk=([A-Za-z0-9-]+)", re.I)
PAN115_RE = re.compile(r"115(?:cdn|rc)?\.com/s/(sw[A-Za-z0-9]+)", re.I)
PAN115_CMD_RE = re.compile(r"115(?:cdn|rc)?\.com/(sw[A-Za-z0-9]{8,})-([A-Za-z0-9]{4,8})", re.I)
PWD_115_URL_RE = re.compile(r"[?&]password=([A-Za-z0-9]+)")
GUANGYA_RE = re.compile(r"guangyapan\.com/s/([A-Za-z0-9_-]+)", re.I)
ALIYUN_RE = re.compile(r"(?:www\.)?(?:alipan|aliyundrive)\.com/s/([A-Za-z0-9_-]+)", re.I)
TIANYI_RE = re.compile(r"cloud\.189\.cn/t/([A-Za-z0-9]+)", re.I)
TIANYI_WEB_RE = re.compile(r"cloud\.189\.cn/web/share\?code=([A-Za-z0-9]+)", re.I)
ILANZOU_RE = re.compile(r"(?:^|[/.])ilanzou\.(?:com|net|org|cn)/s/([A-Za-z0-9_-]+)", re.I)
LANZOU_RE = re.compile(
    r"(?:^|[/.])(?:lanzou[a-z0-9-]*|lan[zs]o[ux])\.(?:com|net|org|cn)/([A-Za-z0-9]+)", re.I
)
PWD_LANZOU_URL_RE = re.compile(r"[?&](?:pwd|pass|passcode|password)=([A-Za-z0-9]+)", re.I)
PWD_URL_RE = re.compile(r"[?&]pwd=([A-Za-z0-9]+)")
PWD_TEXT_RE = re.compile(r"(?:提取码|访问码|密码)[：:]\s*([A-Za-z0-9]{4,8})")

PLATFORM_NAMES = {
    "quark": "夸克网盘",
    "uc": "UC网盘",
    "xunlei": "迅雷网盘",
    "baidu": "百度网盘",
    "c139": "139云盘",
    "pan123": "123盘",
    "pan115": "115网盘",
    "guangya": "光鸭云盘",
    "aliyun": "阿里云盘",
    "tianyi": "天翼云盘",
    "ilanzou": "蓝奏云优享版",
    "lanzou": "蓝奏云",
}


@dataclass
class ParsedLink:
    platform: str
    share_id: str
    pwd: Optional[str]
    url: str

    @property
    def platform_name(self) -> str:
        return PLATFORM_NAMES.get(self.platform, self.platform)


def _pwd_from_url(url: str, text: str, pattern=PWD_URL_RE) -> Optional[str]:
    m = pattern.search(url)
    if m:
        return m.group(1)
    m = PWD_TEXT_RE.search(text)
    return m.group(1) if m else None


def parse(text: str) -> Optional[ParsedLink]:
    """识别分享链接。返回 None 表示无法识别。"""
    text = (text or "").strip()
    if not text:
        return None
    m = URL_RE.search(text)
    if not m:
        return None
    url = m.group(0).rstrip("。，,；;)]}\"'")

    mm = QUARK_RE.search(url)
    if mm:
        return ParsedLink("quark", mm.group(1), _pwd_from_url(url, text), url)
    mm = UC_RE.search(url)
    if mm:
        return ParsedLink("uc", mm.group(1), _pwd_from_url(url, text), url)
    mm = XUNLEI_RE.search(url)
    if mm:
        return ParsedLink("xunlei", mm.group(1), _pwd_from_url(url, text), url)
    mm = BAIDU_RE.search(url)
    if mm:
        # 百度 surl 不含开头的 "1"（verify/list 接口用 1 后面的部分）
        return ParsedLink("baidu", mm.group(1)[1:], _pwd_from_url(url, text), url)
    mm = C139_RE.search(url)
    if mm:
        return ParsedLink("c139", mm.group(1), _pwd_from_url(url, text), url)
    for rx in (PAN123_RE, PAN123_SUB_RE, PAN123_SRR_RE):
        mm = rx.search(url)
        if mm:
            return ParsedLink("pan123", mm.group(1), _pwd_from_url(url, text), url)
    mm = PAN115_RE.search(url)
    if mm:
        pwd = PWD_115_URL_RE.search(url)
        pwd = pwd.group(1) if pwd else None
        if pwd is None:
            mt = PWD_TEXT_RE.search(text)
            pwd = mt.group(1) if mt else None
        return ParsedLink("pan115", mm.group(1), pwd, url)
    mm = PAN115_CMD_RE.search(url)
    if mm:
        return ParsedLink("pan115", mm.group(1), mm.group(2), url)
    mm = GUANGYA_RE.search(url)
    if mm:
        return ParsedLink("guangya", mm.group(1), _pwd_from_url(url, text), url)
    mm = ALIYUN_RE.search(url)
    if mm:
        return ParsedLink("aliyun", mm.group(1), _pwd_from_url(url, text), url)
    for rx in (TIANYI_WEB_RE, TIANYI_RE):
        mm = rx.search(url)
        if mm:
            return ParsedLink("tianyi", mm.group(1), _pwd_from_url(url, text), url)
    # 优享版先于蓝奏云判断，避免 www.ilanzou.com 被误判
    mm = ILANZOU_RE.search(url)
    if mm:
        return ParsedLink("ilanzou", mm.group(1), _pwd_from_url(url, text, PWD_LANZOU_URL_RE), url)
    mm = LANZOU_RE.search(url)
    if mm:
        # 蓝奏云域名不定（lanzou/lanzoup/lanzous/woozooo…），share_id 直接存完整链接
        return ParsedLink("lanzou", url, _pwd_from_url(url, text, PWD_LANZOU_URL_RE), url)
    return None
