"""Lightweight background job runner for long generations (segment rendering,
merging, Veo clips) so the UI doesn't block and the user can switch projects.

A job runs in a daemon thread and writes its status to <task_dir>/job.json.
The Streamlit UI polls that file (it survives reruns and page switches). Threads
do not survive a process/container restart — an interrupted job is simply marked
stale on next read and can be re-submitted.
"""

import json
import os
import threading
import time
import traceback

from loguru import logger

from app.utils import utils

_lock = threading.Lock()
# In-process registry of live threads, so we can tell "running" from "stale"
# (a job.json left as running after a restart, with no live thread).
_threads = {}


def _job_file(task_id: str) -> str:
    return os.path.join(utils.task_dir(task_id), "job.json")


def read_status(task_id: str):
    try:
        with open(_job_file(task_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write(task_id: str, data: dict):
    with _lock:
        path = _job_file(task_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)


def is_running(task_id: str) -> bool:
    s = read_status(task_id)
    if not s or s.get("status") != "running":
        return False
    # If job.json says running but no live thread exists (e.g. after a restart),
    # treat as stale/failed so the UI won't hang forever.
    th = _threads.get(task_id)
    if th is None:
        # Could be a thread from a previous run we lost track of; if the file is
        # old and no thread, mark stale.
        if time.time() - s.get("heartbeat", s.get("started_at", 0)) > 900:
            s["status"] = "error"
            s["error"] = "背景任務已中斷（伺服器可能重啟過），請重新產製"
            _write(task_id, s)
            return False
        return True
    return th.is_alive()


def update_progress(task_id: str, done: int, total: int, note: str = ""):
    s = read_status(task_id) or {}
    s.update({"progress_done": done, "progress_total": total, "note": note,
              "heartbeat": time.time()})
    _write(task_id, s)


def submit(task_id: str, kind: str, fn, total: int = 0) -> bool:
    """Run fn() in a background thread. fn must be self-contained (no Streamlit
    calls) and return a JSON-serializable result (or None). Returns False if a
    job is already running for this task."""
    if is_running(task_id):
        return False
    _write(task_id, {"status": "running", "kind": kind, "started_at": time.time(),
                     "heartbeat": time.time(), "progress_done": 0,
                     "progress_total": total, "note": ""})

    def _run():
        try:
            result = fn()
            s = read_status(task_id) or {}
            s.update({"status": "done", "kind": kind, "finished_at": time.time(),
                      "result": result if isinstance(result, dict) else {}})
            _write(task_id, s)
            logger.success(f"background job done: task={task_id} kind={kind}")
        except Exception as e:
            s = read_status(task_id) or {}
            s.update({"status": "error", "kind": kind, "finished_at": time.time(),
                      "error": str(e), "trace": traceback.format_exc()[-1200:]})
            _write(task_id, s)
            logger.error(f"background job failed: task={task_id} kind={kind}: {e}")
        finally:
            _threads.pop(task_id, None)

    th = threading.Thread(target=_run, daemon=True)
    _threads[task_id] = th
    th.start()
    return True


def clear(task_id: str):
    try:
        os.remove(_job_file(task_id))
    except Exception:
        pass
    _threads.pop(task_id, None)
