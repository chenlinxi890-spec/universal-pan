"""通用网盘 - 提供方注册表。

11 家全部实现（游客模式）：
    百度、夸克、UC、115、光鸭、蓝奏云、蓝奏云优享版、阿里云盘：解析 + 下载
    迅雷、139、123、天翼云盘：解析（列文件）可用；下载需要登录账号，
    本工具不碰账号密码，取链时明确报错。

各家流程端口自 YunX-Desktop（AGPL-3.0，见 REFERENCES.md）；
阿里云盘流程另见 tickstep/aliyunpan 与阿里云 PDS 公开文档，
天翼云盘流程另见 LinkSwift（AGPL-3.0）与 189 公开接口。
"""
from __future__ import annotations

from typing import Optional

from ..models import ProviderError, ShareFile, ShareInfo
from .aliyun import AliyunProvider
from .baidu import BaiduProvider
from .base import Provider
from .c139 import C139Provider
from .guangya import GuangYaProvider
from .ilanzou import ILanzouProvider
from .lanzou import LanzouProvider
from .linkparse import PLATFORM_NAMES, parse
from .pan115 import Pan115Provider
from .pan123 import Pan123Provider
from .quark import QuarkProvider
from .tianyi import TianyiProvider
from .uc import UCProvider
from .xunlei import XunleiProvider

__all__ = ["PROVIDERS", "detect", "get_provider", "PLATFORM_NAMES", "parse"]

PROVIDERS: dict[str, Provider] = {
    "baidu": BaiduProvider(),
    "quark": QuarkProvider(),
    "uc": UCProvider(),
    "xunlei": XunleiProvider(),
    "c139": C139Provider(),
    "pan123": Pan123Provider(),
    "pan115": Pan115Provider(),
    "guangya": GuangYaProvider(),
    "ilanzou": ILanzouProvider(),
    "lanzou": LanzouProvider(),
    "aliyun": AliyunProvider(),
    "tianyi": TianyiProvider(),
}


def detect(text: str):
    """识别分享链接，返回 ParsedLink 或 None。"""
    return parse(text)


def get_provider(platform: str) -> Optional[Provider]:
    return PROVIDERS.get(platform)
