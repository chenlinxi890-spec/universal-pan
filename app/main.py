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
import html
import io
import os
import re
import shutil
import time
import urllib.parse
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import downloader, extractor
from .models import Job, ProviderError, ShareFile, Task, Upload, new_id
from .providers import PLATFORM_NAMES, detect, get_provider
from .store import Store

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("UNIPAN_DATA", BASE_DIR / "data"))
STATIC_DIR = BASE_DIR / "static"

STORE = Store(DATA_DIR)

CHUNK_SIZE = 4 * 1024 * 1024  # 分片大小 4MB

app = FastAPI(title="通用网盘", version="0.1.0")

TASKS: dict[str, Task] = {}
JOBS: dict[str, Job] = {}


# ---------- 工具 ----------

def task_dir(task_id: str) -> Path:
    return DATA_DIR / "tasks" / task_id


def upload_dir(upload_id: str) -> Path:
    return DATA_DIR / "uploads" / upload_id


def upload_tmp_dir(upload_id: str) -> Path:
    return DATA_DIR / "uploads" / ".tmp" / upload_id


def _sync_task_store(task: Task) -> None:
    STORE.upsert_task({
        "id": task.id,
        "platform": task.platform,
        "platform_name": task.platform_name,
        "title": task.title,
        "url": task.url,
        "created_at": task.created_at,
        "status": task.status,
        "error": task.error,
    })


def _sync_job_store(job: Job) -> None:
    STORE.upsert_job({
        "id": job.id,
        "task_id": job.task_id,
        "kind": job.kind,
        "status": job.status,
        "message": job.message,
        "progress": round(job.progress, 4),
        "total_bytes": job.total_bytes,
        "done_bytes": job.done_bytes,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    })


def fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{n / 1024 / 1024 / 1024:.2f} GB"


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
        raise HTTPException(400, "无法识别的分享链接：请粘贴百度、夸克、UC、迅雷、139、123、115、光鸭、蓝奏云、阿里云盘或天翼云盘的分享链接")
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
    _sync_task_store(task)
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
        job.updated_at = time.time()
        _sync_job_store(job)
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
        # 多轮实测均如此，非本工具的问题
        if "31360" in msg or "expire time out" in msg:
            msg += (
                " —— 百度已限制免登录下载：直链签发即过期，"
                "请登录百度账号后下载，或把文件转存到自己的百度网盘再下"
            )
        job.status = "failed"
        job.message = msg
    finally:
        job.updated_at = time.time()
        _sync_job_store(job)
        _sync_task_store(task)


@app.post("/api/tasks/{task_id}/download")
async def start_download(task_id: str, req: DownloadReq):
    task = get_task(task_id)
    if not req.items:
        raise HTTPException(400, "请先选择要下载的文件")
    job = Job(id=new_id(), task_id=task_id, kind="download")
    JOBS[job.id] = job
    _sync_job_store(job)
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
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(name + '-解压结果.zip')}"},
    )


# ---------- 上传（传到本站服务器，不是传进百度/夸克） ----------

class UploadInitReq(BaseModel):
    name: str
    size: int
    total_chunks: int


def _received_chunks(upload_id: str, total_chunks: int) -> list[int]:
    tmp = upload_tmp_dir(upload_id)
    out = []
    for i in range(total_chunks):
        if (tmp / f"chunk-{i}").is_file():
            out.append(i)
    return out


def _get_upload_rec(upload_id: str) -> dict:
    rec = STORE.get_upload(upload_id)
    if not rec:
        raise HTTPException(404, "上传不存在或已过期")
    return rec


async def _finalize_upload(rec: dict) -> dict:
    """原文件已落盘：生成 share_id，压缩包则自动解压，更新记录。"""
    upload_id = rec["id"]
    file_dir = upload_dir(upload_id) / "file"
    files = [p for p in file_dir.iterdir() if p.is_file()] if file_dir.is_dir() else []
    if not files:
        rec["status"] = "failed"
        rec["error"] = "文件丢失，无法完成上传"
        STORE.upsert_upload(rec)
        raise HTTPException(400, "文件丢失，无法完成上传")
    arc = files[0]
    rec["name"] = arc.name
    rec["size"] = arc.stat().st_size
    if not rec.get("share_id"):
        rec["share_id"] = new_id()
    if extractor.is_archive(arc.name):
        ex_dir = upload_dir(upload_id) / "extracted"
        ok, msg, cnt, total = await asyncio.to_thread(
            extractor.extract_archive, str(arc), str(ex_dir)
        )
        if ok:
            rec["extracted_count"] = cnt
            rec["extracted_bytes"] = total
        else:
            rec["error"] = msg
    rec["status"] = "done"
    STORE.upsert_upload(rec)
    # 合并完成，清理临时分片
    shutil.rmtree(upload_tmp_dir(upload_id), ignore_errors=True)
    return {
        "upload_id": upload_id,
        "share_id": rec["share_id"],
        "share_url": f"/share/{rec['share_id']}",
        "name": rec["name"],
        "size": rec["size"],
        "extracted_count": rec["extracted_count"],
    }


@app.post("/api/uploads/init")
async def init_upload(req: UploadInitReq):
    name = safe_name(req.name)
    if req.size <= 0 or req.total_chunks < 1:
        raise HTTPException(400, "文件大小或分片数无效")
    upload_id = new_id()
    (upload_dir(upload_id) / "file").mkdir(parents=True, exist_ok=True)
    upload_tmp_dir(upload_id).mkdir(parents=True, exist_ok=True)
    rec = {
        "id": upload_id,
        "name": name,
        "size": req.size,
        "created_at": time.time(),
        "status": "uploading",
        "share_id": "",
        "total_chunks": req.total_chunks,
        "extracted_count": 0,
        "extracted_bytes": 0,
        "error": "",
    }
    STORE.upsert_upload(rec)
    return {"upload_id": upload_id}


@app.post("/api/uploads/{upload_id}/chunk")
async def upload_chunk(upload_id: str, index: int = Form(...), chunk: UploadFile = File(...)):
    rec = _get_upload_rec(upload_id)
    if rec["status"] == "done":
        raise HTTPException(400, "该上传已完成")
    total = int(rec.get("total_chunks") or 0)
    if index < 0 or index >= total:
        raise HTTPException(400, "分片序号无效")
    dest = upload_tmp_dir(upload_id) / f"chunk-{index}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        while True:
            data = await chunk.read(1024 * 1024)
            if not data:
                break
            f.write(data)
    return {"received": _received_chunks(upload_id, total), "total_chunks": total}


@app.get("/api/uploads/{upload_id}/chunks")
async def get_received_chunks(upload_id: str):
    rec = _get_upload_rec(upload_id)
    total = int(rec.get("total_chunks") or 0)
    return {
        "received": _received_chunks(upload_id, total),
        "total_chunks": total,
        "status": rec["status"],
    }


@app.post("/api/uploads/{upload_id}/complete")
async def complete_upload(upload_id: str):
    rec = _get_upload_rec(upload_id)
    if rec["status"] == "done":
        return {
            "upload_id": upload_id,
            "share_id": rec["share_id"],
            "share_url": f"/share/{rec['share_id']}",
            "name": rec["name"],
            "size": rec["size"],
            "extracted_count": rec["extracted_count"],
        }
    total = int(rec.get("total_chunks") or 0)
    received = _received_chunks(upload_id, total)
    if len(received) != total:
        missing = [i for i in range(total) if i not in received]
        raise HTTPException(400, f"分片不完整，缺 {missing}")
    dest = upload_dir(upload_id) / "file" / safe_name(rec["name"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as out:
        for i in range(total):
            with open(upload_tmp_dir(upload_id) / f"chunk-{i}", "rb") as f:
                shutil.copyfileobj(f, out)
    if dest.stat().st_size != int(rec["size"]):
        rec["status"] = "failed"
        rec["error"] = f"合并后大小不符（期望 {rec['size']}，实际 {dest.stat().st_size}）"
        STORE.upsert_upload(rec)
        raise HTTPException(400, rec["error"])
    return await _finalize_upload(rec)


@app.post("/api/uploads")
async def upload_single(file: UploadFile = File(...)):
    name = safe_name(file.filename or "file")
    upload_id = new_id()
    dest_dir = upload_dir(upload_id) / "file"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    size = 0
    with open(dest, "wb") as f:
        while True:
            data = await file.read(1024 * 1024)
            if not data:
                break
            size += len(data)
            f.write(data)
    rec = {
        "id": upload_id,
        "name": name,
        "size": size,
        "created_at": time.time(),
        "status": "uploading",
        "share_id": "",
        "total_chunks": 1,
        "extracted_count": 0,
        "extracted_bytes": 0,
        "error": "",
    }
    STORE.upsert_upload(rec)
    return await _finalize_upload(rec)


# ---------- 公开分享 ----------

def _upload_by_share(share_id: str) -> dict:
    rec = STORE.get_upload_by_share(share_id)
    if not rec or rec.get("status") != "done":
        raise HTTPException(404, "分享不存在或已删除")
    return rec


def _original_file(rec: dict) -> Path | None:
    file_dir = upload_dir(rec["id"]) / "file"
    if not file_dir.is_dir():
        return None
    files = [p for p in file_dir.iterdir() if p.is_file()]
    return files[0] if files else None


@app.get("/share/{share_id}", response_class=HTMLResponse)
async def share_page(share_id: str):
    rec = _upload_by_share(share_id)
    orig = _original_file(rec)
    extracted = extractor.list_tree(str(upload_dir(rec["id"]) / "extracted"))
    rows = "".join(
        f'<li><a href="/share/{html.escape(share_id)}/file?path={urllib.parse.quote(f["path"])}">{html.escape(f["path"])}</a>（{fmt_size(f["size"])}）</li>'
        for f in extracted
    )
    pack_link = (
        f'<p><a href="/share/{html.escape(share_id)}/archive">⬇ 打包下载全部（zip）</a></p>'
        if extracted else ""
    )
    extracted_block = (
        f"<h3>已解压文件（共 {len(extracted)} 个）</h3><ul>{rows}</ul>"
        if extracted else '<p class="muted">该文件不是压缩包，没有解压内容。</p>'
    )
    body = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(rec["name"])} - 通用网盘分享</title>
<style>body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f4f6fa;color:#222;margin:0}}
.wrap{{max-width:720px;margin:0 auto;padding:24px 16px}}.card{{background:#fff;border-radius:12px;padding:20px;box-shadow:0 1px 4px rgba(0,0,0,.06)}}
a{{color:#1a73e8}}ul{{line-height:2}}h1{{font-size:20px;word-break:break-all}}.muted{{color:#888;font-size:13px}}</style></head>
<body><div class="wrap"><div class="card">
<h1>📄 {html.escape(rec["name"])}</h1>
<p class="muted">大小：{fmt_size(int(rec["size"]))}　上传时间：{time.strftime("%Y-%m-%d %H:%M", time.localtime(rec["created_at"]))}</p>
<p><a href="/share/{html.escape(share_id)}/file">⬇ 下载原文件</a></p>
{pack_link}
{extracted_block}
<p class="muted">通用网盘分享 · 打开链接即可下载，不用登录</p>
</div></div></body></html>"""
    return HTMLResponse(body)


@app.get("/share/{share_id}/file")
async def share_download_file(share_id: str, path: str = ""):
    rec = _upload_by_share(share_id)
    if not path:
        orig = _original_file(rec)
        if not orig:
            raise HTTPException(404, "文件不存在")
        return FileResponse(str(orig), filename=orig.name)
    base = (upload_dir(rec["id"]) / "extracted").resolve()
    target = (base / safe_rel_path(path)).resolve()
    if not str(target).startswith(str(base)) or not target.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(str(target), filename=target.name)


@app.get("/share/{share_id}/archive")
async def share_download_archive(share_id: str):
    rec = _upload_by_share(share_id)
    ex = upload_dir(rec["id"]) / "extracted"
    files = extractor.list_tree(str(ex))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if files:
            for f in files:
                zf.write(ex / f["path"], arcname=f["path"])
        else:
            orig = _original_file(rec)
            if not orig:
                raise HTTPException(404, "没有可下载的文件")
            zf.write(orig, arcname=orig.name)
    buf.seek(0)
    name = (rec["name"] or rec["id"]).strip()[:40]
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(name + '-分享.zip')}"},
    )


# ---------- 文件管理 / 任务历史 ----------

@app.get("/api/library")
async def list_library():
    uploads = []
    for rec in STORE.all_uploads():
        uid = rec["id"]
        base = upload_dir(uid)
        orig = _original_file(rec)
        uploads.append({
            **rec,
            "share_url": f"/share/{rec['share_id']}" if rec.get("share_id") else "",
            "original": {"path": orig.name, "size": orig.stat().st_size} if orig else None,
            "extracted": extractor.list_tree(str(base / "extracted")),
        })
    tasks = []
    tasks_root = DATA_DIR / "tasks"
    if tasks_root.is_dir():
        for d in sorted(tasks_root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not d.is_dir():
                continue
            meta = STORE.get_task(d.name) or {}
            mem = TASKS.get(d.name)
            tasks.append({
                "id": d.name,
                "platform": meta.get("platform") or (mem.platform if mem else ""),
                "platform_name": meta.get("platform_name") or (mem.platform_name if mem else ""),
                "title": meta.get("title") or (mem.title if mem else d.name),
                "url": meta.get("url") or (mem.url if mem else ""),
                "created_at": meta.get("created_at") or d.stat().st_mtime,
                "status": meta.get("status") or "done",
                "downloaded": extractor.list_tree(str(d / "download")),
                "extracted": extractor.list_tree(str(d / "extracted")),
            })
    return {"uploads": uploads, "tasks": tasks}


@app.delete("/api/uploads/{upload_id}")
async def delete_upload(upload_id: str):
    rec = STORE.get_upload(upload_id)
    if not rec:
        raise HTTPException(404, "上传不存在")
    shutil.rmtree(upload_dir(upload_id), ignore_errors=True)
    shutil.rmtree(upload_tmp_dir(upload_id), ignore_errors=True)
    STORE.delete_upload(upload_id)
    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    if task_id not in TASKS and not (DATA_DIR / "tasks" / task_id).is_dir() and not STORE.get_task(task_id):
        raise HTTPException(404, "任务不存在")
    shutil.rmtree(task_dir(task_id), ignore_errors=True)
    TASKS.pop(task_id, None)
    STORE.delete_task(task_id)
    return {"ok": True}


@app.get("/api/history")
async def list_history():
    return {"tasks": STORE.all_tasks(), "jobs": STORE.all_jobs()}


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
