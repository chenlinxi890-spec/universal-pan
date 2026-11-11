"""通用网盘 - 持久化存储。

数据存在 DATA_DIR/store.json，只存元数据，绝不存任何登录态 / session。
每次变更后原子写回（先写 tmp 再重命名），进程重启后记录仍在。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


class Store:
    """三类记录：uploads / tasks / jobs，均为 {id: dict}。"""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "store.json"
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {"uploads": {}, "tasks": {}, "jobs": {}}
        self._load()

    # ---------- 基础 ----------

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for k in ("uploads", "tasks", "jobs"):
                    if isinstance(raw.get(k), dict):
                        self._data[k] = raw[k]
        except (OSError, ValueError):
            # 损坏的 store 不阻塞启动；下次保存时重建
            self._data = {"uploads": {}, "tasks": {}, "jobs": {}}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def _upsert(self, kind: str, rec: dict) -> dict:
        with self._lock:
            rec.setdefault("updated_at", time.time())
            self._data[kind][rec["id"]] = rec
            self._save()
        return rec

    def _delete(self, kind: str, rec_id: str) -> bool:
        with self._lock:
            existed = self._data[kind].pop(rec_id, None) is not None
            if existed:
                self._save()
        return existed

    # ---------- uploads ----------

    def upsert_upload(self, rec: dict) -> dict:
        return self._upsert("uploads", rec)

    def get_upload(self, upload_id: str) -> dict | None:
        return self._data["uploads"].get(upload_id)

    def get_upload_by_share(self, share_id: str) -> dict | None:
        for rec in self._data["uploads"].values():
            if rec.get("share_id") == share_id:
                return rec
        return None

    def delete_upload(self, upload_id: str) -> bool:
        return self._delete("uploads", upload_id)

    def all_uploads(self) -> list[dict]:
        return sorted(
            self._data["uploads"].values(),
            key=lambda r: r.get("created_at", 0),
            reverse=True,
        )

    # ---------- tasks ----------

    def upsert_task(self, rec: dict) -> dict:
        return self._upsert("tasks", rec)

    def get_task(self, task_id: str) -> dict | None:
        return self._data["tasks"].get(task_id)

    def delete_task(self, task_id: str) -> bool:
        return self._delete("tasks", task_id)

    def all_tasks(self) -> list[dict]:
        return sorted(
            self._data["tasks"].values(),
            key=lambda r: r.get("created_at", 0),
            reverse=True,
        )

    # ---------- jobs ----------

    def upsert_job(self, rec: dict) -> dict:
        return self._upsert("jobs", rec)

    def get_job(self, job_id: str) -> dict | None:
        return self._data["jobs"].get(job_id)

    def all_jobs(self) -> list[dict]:
        return sorted(
            self._data["jobs"].values(),
            key=lambda r: r.get("created_at", 0),
            reverse=True,
        )
