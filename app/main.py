"""通用网盘 - FastAPI 后端。

接口：
    GET  /                        前端页面
    GET  /api/providers           支持的网盘列表（含是否已实现）
    POST /api/parse               {text} 解析分享链接 → 任务 + 根目录文件列表
    GET  /api/tasks/{id}          任务信息
    POST /api/tasks/{id}/list     {dir} 列分享内子目录
    POST /api/tasks/{id}/download {items:[{fid,name,path,is_dir}...]} 开始下载（后台）
    GET  /api/jobs/{id}           下载/解压任务状态（轮询）
    GET  /api/tasks/{id}/files    已下载 + 已解压文件列表
    GET  /api/tasks/{id}/file     ?dir=download|extracted&path=... 下载单个文件
    GET  /api/tasks/{id}/archive  打包下载全部解压结果（zip）

只下载用户提供的分享链接；不登录、不转存、不破解、不绕限速。
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import zipfile
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import downloader, extractor
from .models import Job, ProviderError, ShareFile, Task, new_id
from .providers import PLATFORM_NAMES, detect, get_provider

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("UNIPAN_DATA", BASE_DIR / "data"))
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="通用网盘", version="0.1.0")

TASKS: dict[str, Task] = {}
JOBS: dict[str, Job] = {}


# ---------- 工具 ----------

def task_dir(task_id: str) -> Path:
    return DATA_DIR / "tasks" / task_id


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip()
    return name or "file"


def safe_rel_path(rel: str) -> Path:
    """把相对路径清洗为安全路径（防目录穿越）。"""
    parts = [safe_name(p) for p in rel.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return Path(*parts) if parts else Path("file")


def dir_ref_for(task: Task, f: ShareFile) -> str:
    """子目录导航参数：百度用 path，夸克用 fid，其余以后按各家补。"""
    if task.platform == "baidu":
        return f.path
    return f.fid


def get_task(task_id: str) -> Task:
    t = TASKS.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在或已过期")
    return t


# ---------- 请求模型 ----------

class ParseReq(BaseModel):
    text: str


class ListReq(BaseModel):
    dir: str = "/"


class DownloadReq(BaseModel):
    items: list[dict]


# ---------- 页面与基础接口 ----------

@app.get("/api/providers")
async def list_providers():
    from .providers import PROVIDERS

    return [
        {
            "platform": p,
            "name": PLATFORM_NAMES.get(p, p),
            "ready": True,
            "guest_download": PROVIDERS[p].guest_download,
        }
        for p in PROVIDERS
    ]


@app.post("/api/parse")
async def parse_link(req: ParseReq):
    parsed = detect(req.text)
    if not parsed:
        raise HTTPException(400, "无法识别的分享链接：请粘贴百度、夸克、UC、迅雷、139、123、115、光鸭或蓝奏云的分享链接")
    provider = get_provider(parsed.platform)
    if not provider:
        raise HTTPException(400, f"暂不支持 {parsed.platform_name}")
    try:
        info, session = await provider.resolve(parsed.share_id, parsed.pwd)
    except ProviderError as e:
        raise HTTPException(400, e.message)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"解析失败：{e}")
    task = Task(
        id=new_id(),
        platform=parsed.platform,
        platform_name=parsed.platform_name,
        share_id=parsed.share_id,
        pwd=parsed.pwd,
        url=parsed.url,
        title=info.title or parsed.url,
        files=info.files,
        session=session,
    )
    TASKS[task.id] = task
    task_dir(task.id).mkdir(parents=True, exist_ok=True)
    return task.to_dict()


@app.get("/api/tasks/{task_id}")
async def get_task_info(task_id: str):
    return get_task(task_id).to_dict()


@app.post("/api/tasks/{task_id}/list")
async def list_subdir(task_id: str, req: ListReq):
    task = get_task(task_id)
    provider = get_provider(task.platform)
    assert provider is not None
    try:
        files = await provider.list_dir(task.session, req.dir)
    except ProviderError as e:
        raise HTTPException(400, e.message)
    return {"dir": req.dir, "files": [f.to_dict() for f in files]}


# ---------- 下载 ----------

async def _expand_items(task: Task, items: list[dict]) -> list[ShareFile]:
    """把选中的条目展开为文件列表（目录递归）。"""
    provider = get_provider(task.platform)
    assert provider is not None
    out: list[ShareFile] = []

    async def walk(f: ShareFile) -> None:
        if not f.is_dir:
            out.append(f)
            return
        ref = dir_ref_for(task, f)
        try:
            children = await provider.list_dir(task.session, ref)
        except ProviderError as e:
            raise ProviderError(f"展开目录 {f.name} 失败：{e.message}")
        for c in children:
            await walk(c)

    for it in items:
        f = ShareFile(
            fid=str(it.get("fid", "")),
            name=it.get("name", ""),
            size=int(it.get("size") or 0),
            is_dir=bool(it.get("is_dir")),
            path=it.get("path", ""),
            fid_token=it.get("fid_token", ""),
        )
        await walk(f)
    return out


async def _run_download(job: Job, task: Task, items: list[dict]) -> None:
    provider = get_provider(task.platform)
    assert provider is not None
    dl_dir = task_dir(task.id) / "download"
    ex_dir = task_dir(task.id) / "extracted"
    dl_dir.mkdir(parents=True, exist_ok=True)
    ex_dir.mkdir(parents=True, exist_ok=True)
    try:
        job.status = "running"
        job.message = "正在展开文件列表…"
        job.updated_at = __import__("time").time()
        files = await _expand_items(task, items)
        if not files:
            raise ProviderError("选中的内容里没有文件")
        job.total_bytes = sum(f.size for f in files)
        job.message = f"共 {len(files)} 个文件，开始下载…"
        downloaded: list[dict] = []
        used_names: set[str] = set()
        for idx, f in enumerate(files):
            job.current_file = f.name
            job.message = f"正在下载 {idx + 1}/{len(files)}：{f.name}"
            link = await provider.get_download_url(task.session, f)
            # 目标路径：百度用分享内路径，其余用文件名（重名加序号）
            if task.platform == "baidu" and f.path:
                rel = safe_rel_path(f.path.lstrip("/"))
            else:
                rel = Path(safe_name(f.name))
            if str(rel) in used_names:
                rel = rel.with_name(f"{rel.stem}({idx + 1}){rel.suffix}")
            used_names.add(str(rel))
            dest = dl_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            start_done = job.done_bytes

            async def _prog(d: int, t: int, _job=job, _start=start_done) -> None:
                _job.done_bytes = _start + d
                if _job.total_bytes:
                    _job.progress = _job.done_bytes / _job.total_bytes

            nbytes = await downloader.download(
                link.url, str(dest),
                headers=link.headers, cookies=link.cookies,
                progress_cb=_prog,
            )
            job.done_bytes = start_done + nbytes
            downloaded.append({"name": f.name, "path": str(rel), "size": nbytes})
        job.progress = 1.0
        job.message = "下载完成，正在解压…"
        # 自动解压
        extracted_count = 0
        extracted_bytes = 0
        extract_notes: list[str] = []
        for d in downloaded:
            if not extractor.is_archive(d["name"]):
                continue
            arc = dl_dir / d["path"]
            stem = Path(d["name"]).stem
            target = ex_dir / safe_name(stem)
            ok, msg, cnt, total = extractor.extract_archive(str(arc), str(target))
            if ok:
                extracted_count += cnt
                extracted_bytes += total
            else:
                extract_notes.append(f"{d['name']}：{msg}")
        job.status = "done"
        job.progress = 1.0
        job.message = "完成" + ("；" + "；".join(extract_notes) if extract_notes else "")
        job.result = {
            "downloaded": downloaded,
            "extracted_count": extracted_count,
            "extracted_bytes": extracted_bytes,
            "extract_notes": extract_notes,
            "extracted": extractor.list_tree(str(ex_dir)),
        }
    except ProviderError as e:
        job.status = "failed"
        job.message = e.message
    except Exception as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        msg = str(e)
        if not msg.startswith("下载失败"):
            msg = f"下载失败：{msg}"
        # 百度 31360：免登录直链签发即过期（dstime=签名时间-1h，expires=1h），
        # 2026-11-11 在笔记本上多轮实测均如此，非本工具的问题
        if "31360" in msg or "expire time out" in msg:
            msg += (
                " —— 百度已限制免登录下载：直链签发即过期，"
                "请登录百度账号后下载，或把文件转存到自己的百度网盘再下"
            )
        job.status = "failed"
        job.message = msg
    finally:
        job.updated_at = __import__("time").time()


@app.post("/api/tasks/{task_id}/download")
async def start_download(task_id: str, req: DownloadReq):
    task = get_task(task_id)
    if not req.items:
        raise HTTPException(400, "请先选择要下载的文件")
    job = Job(id=new_id(), task_id=task_id, kind="download")
    JOBS[job.id] = job
    asyncio.create_task(_run_download(job, task, req.items))
    return {"job_id": job.id}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job.to_dict()


@app.get("/api/tasks/{task_id}/files")
async def list_task_files(task_id: str):
    task = get_task(task_id)
    dl = task_dir(task.id) / "download"
    ex = task_dir(task.id) / "extracted"
    return {
        "downloaded": extractor.list_tree(str(dl)),
        "extracted": extractor.list_tree(str(ex)),
    }


@app.get("/api/tasks/{task_id}/file")
async def download_file(task_id: str, dir: str = "extracted", path: str = ""):
    task = get_task(task_id)
    base = task_dir(task.id) / ("extracted" if dir == "extracted" else "download")
    target = (base / safe_rel_path(path)).resolve()
    if not str(target).startswith(str(base.resolve())) or not target.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(str(target), filename=target.name)


@app.get("/api/tasks/{task_id}/archive")
async def download_archive(task_id: str):
    task = get_task(task_id)
    ex = task_dir(task.id) / "extracted"
    files = extractor.list_tree(str(ex))
    if not files:
        raise HTTPException(404, "还没有解压出的文件")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            p = ex / f["path"]
            zf.write(p, arcname=f["path"])
    buf.seek(0)
    name = (task.title or task.id).strip()[:40] or task.id
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}-解压结果.zip"},
    )


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
