"""通用网盘 - 下载器：断点续传 + 分片并发。

- 先探测大小与 Range 支持；支持时分 4 片并发下载，单片失败重试 3 次；
- 已下完的分片直接复用（断点续传）；不支持 Range 时单流下载；
- 全部完成后合并为目标文件并校验大小。
"""
from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable, Optional

import httpx

ProgressCb = Callable[[int, int], Awaitable[None]]


async def _head_size(
    client: httpx.AsyncClient, url: str, headers: dict
) -> tuple[Optional[int], bool]:
    """返回 (总字节数或 None, 是否支持 Range)。"""
    try:
        r = await client.head(url, headers=headers)
        if r.status_code in (200, 206):
            size = r.headers.get("Content-Length")
            accept = r.headers.get("Accept-Ranges", "")
            return (int(size) if size else None, accept.lower() == "bytes")
    except Exception:
        pass
    # HEAD 不行时用 Range: bytes=0-0 探测
    try:
        h = dict(headers)
        h["Range"] = "bytes=0-0"
        async with client.stream("GET", url, headers=h) as r:
            if r.status_code == 206:
                cr = r.headers.get("Content-Range", "")  # bytes 0-0/12345
                if "/" in cr:
                    return int(cr.rsplit("/", 1)[1]), True
            length = r.headers.get("Content-Length")
            if length:
                return int(length), False
    except Exception:
        pass
    return None, False


async def _download_range(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    start: int,
    end: int,
    part_path: str,
    sem: asyncio.Semaphore,
    progress: Callable[[int], None],
    retries: int = 3,
) -> None:
    """下载一个分片（[start, end]），写到 part_path。"""
    expected = end - start + 1
    if os.path.exists(part_path) and os.path.getsize(part_path) == expected:
        progress(expected)
        return
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            h = dict(headers)
            h["Range"] = f"bytes={start}-{end}"
            got = 0
            async with sem:
                async with client.stream("GET", url, headers=h) as r:
                    if r.status_code not in (200, 206):
                        raise RuntimeError(f"HTTP {r.status_code}")
                    with open(part_path, "wb") as f:
                        async for chunk in r.aiter_bytes(1024 * 256):
                            f.write(chunk)
                            got += len(chunk)
                            progress(len(chunk))
            if got == expected or r.status_code == 200:
                return
            last_err = RuntimeError(f"分片不完整：{got}/{expected}")
        except Exception as e:  # noqa: BLE001
            last_err = e
            if os.path.exists(part_path):
                try:
                    os.remove(part_path)
                except OSError:
                    pass
            await asyncio.sleep(1 + attempt)
    raise last_err or RuntimeError("分片下载失败")


async def download(
    url: str,
    dest: str,
    headers: dict | None = None,
    cookies: dict | None = None,
    chunks: int = 4,
    progress_cb: ProgressCb | None = None,
) -> int:
    """下载 url 到 dest，返回字节数。"""
    headers = dict(headers or {})
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    downloaded = [0]
    total = [0]

    async def report() -> None:
        if progress_cb:
            await progress_cb(downloaded[0], total[0])

    def bump(n: int) -> None:
        downloaded[0] += n

    async with httpx.AsyncClient(
        headers=headers,
        cookies=cookies or None,
        follow_redirects=True,
        timeout=httpx.Timeout(600.0, connect=30.0),
        proxy=(
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("http_proxy")
            or None
        ),
        # 同 base.py：no_proxy 里的裸 IPv6 会让 httpx 解析环境代理时报错
        trust_env=False,
        # 出口代理 TLS 拦截的 CA（本机）；笔记本上走系统默认 CA
        verify=os.environ.get("SSL_CERT_FILE") or True,
    ) as client:
        size, supports_range = await _head_size(client, url, headers)
        total[0] = size or 0
        await report()
        # 已下载完成则直接返回
        if size and os.path.exists(dest) and os.path.getsize(dest) == size:
            downloaded[0] = size
            await report()
            return size
        if size and supports_range and size > 1024 * 1024:
            # 分片并发
            part_size = size // chunks
            ranges = []
            for i in range(chunks):
                s = i * part_size
                e = size - 1 if i == chunks - 1 else (i + 1) * part_size - 1
                ranges.append((s, e))
            sem = asyncio.Semaphore(chunks)
            # 进度汇报任务
            stop = asyncio.Event()

            async def reporter() -> None:
                while not stop.is_set():
                    await report()
                    await asyncio.sleep(0.5)

            rep_task = asyncio.create_task(reporter())
            try:
                await asyncio.gather(
                    *[
                        _download_range(
                            client, url, headers, s, e,
                            f"{dest}.part{i}", sem, bump,
                        )
                        for i, (s, e) in enumerate(ranges)
                    ]
                )
            finally:
                stop.set()
                await rep_task
            # 合并
            with open(dest, "wb") as out:
                for i in range(chunks):
                    part = f"{dest}.part{i}"
                    with open(part, "rb") as f:
                        out.write(f.read())
                    os.remove(part)
            downloaded[0] = size
            await report()
            return size
        # 单流下载
        got = 0
        async with client.stream("GET", url, headers=headers) as r:
            if r.status_code not in (200, 206):
                # 把服务端返回的错误体带上（百度 31360 这类 JSON 原因就写在这里）
                body = ""
                try:
                    body = (await r.aread()).decode("utf-8", "replace")[:200]
                except Exception:
                    pass
                raise RuntimeError(f"下载失败：HTTP {r.status_code} {body}".strip())
            with open(dest, "wb") as f:
                async for chunk in r.aiter_bytes(1024 * 256):
                    f.write(chunk)
                    got += len(chunk)
                    downloaded[0] = got
                    if total[0] == 0:
                        total[0] = int(r.headers.get("Content-Length") or 0)
                    await report()
        return got
