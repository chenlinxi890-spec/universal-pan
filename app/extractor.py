"""通用网盘 - 解压：调用系统 7z（支持 zip / 7z / rar 等）。"""
from __future__ import annotations

import os
import subprocess


def is_archive(name: str) -> bool:
    low = name.lower()
    return low.endswith((".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".iso"))


def extract_archive(archive_path: str, dest_dir: str) -> tuple[bool, str, int, int]:
    """解压到 dest_dir。返回 (成功, 信息, 文件数, 总字节数)。"""
    os.makedirs(dest_dir, exist_ok=True)
    try:
        proc = subprocess.run(
            ["7z", "x", "-y", f"-o{dest_dir}", archive_path],
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except FileNotFoundError:
        return False, "服务器未安装 7z，无法解压", 0, 0
    except subprocess.TimeoutExpired:
        return False, "解压超时", 0, 0
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-500:]
        return False, f"解压失败：{tail}", 0, 0
    count = 0
    total = 0
    for root, _dirs, files in os.walk(dest_dir):
        for fn in files:
            p = os.path.join(root, fn)
            try:
                total += os.path.getsize(p)
                count += 1
            except OSError:
                pass
    return True, "解压完成", count, total


def list_tree(base_dir: str) -> list[dict]:
    """列出目录下所有文件（相对路径、大小）。"""
    out: list[dict] = []
    if not os.path.isdir(base_dir):
        return out
    for root, dirs, files in os.walk(base_dir):
        dirs.sort()
        for fn in sorted(files):
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, base_dir)
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            out.append({"path": rel, "name": fn, "size": size, "is_dir": False})
    return out
