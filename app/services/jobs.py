"""Background job runner supporting MULTIPLE concurrent jobs per task, keyed by
an arbitrary job key, so e.g. several per-segment storyboard images can generate
in parallel within one project, while batch operations (render all / merge) stay
exclusive.

State for a task lives in <task_dir>/jobs.json as {job_key: status}. A job runs
in a daemon thread. Threads do not survive a process/container restart — such a
job is detected as stale (no live thread) and marked interrupted so the UI
recovers immediately.
"""

import json
import os
import threading
import time
import traceback

from loguru import logger

from app.utils import utils

_lock = threading.Lock()
# (task_id, key) -> Thread, for the current process's live jobs.
_threads = {}


def _jobs_file(task_id: str) -> str:
    return os.path.join(utils.task_dir(task_id), "jobs.json")


def _read_raw(task_id: str) -> dict:
    try:
        with open(_jobs_file(task_id), "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_all(task_id: str, d: dict):
    path = _jobs_file(task_id)
    if not d:
        try:
            os.remove(path)
        except Exception:
            pass
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, path)


def read_all(task_id: str) -> dict:
    """Return {key: status}. Any 'running' job with no live thread (e.g. after a
    restart) is marked as interrupted so the UI won't hang."""
    d = _read_raw(task_id)
    changed = False
    for key, s in list(d.items()):
        if s.get("status") == "running":
            th = _threads.get((task_id, key))
            if th is None or not th.is_alive():
                s["status"] = "error"
                s["error"] = "背景任務已中斷（伺服器重啟過），請重新產製（已完成的素材會沿用）"
                changed = True
    if changed:
        with _lock:
            _write_all(task_id, d)
    return d


def read_status(task_id: str, key: str):
    return read_all(task_id).get(key)


def is_running(task_id: str, key: str = None) -> bool:
    """key=None → any job running for the task; else that specific job."""
    d = read_all(task_id)
    if key is not None:
        s = d.get(key)
        return bool(s and s.get("status") == "running")
    return any(s.get("status") == "running" for s in d.values())


def running_keys(task_id: str) -> list:
    return [k for k, s in read_all(task_id).items() if s.get("status") == "running"]


def update_progress(task_id: str, key: str, done: int, total: int, note: str = ""):
    with _lock:
        d = _read_raw(task_id)
        s = d.get(key, {})
        s.update({"progress_done": done, "progress_total": total, "note": note,
                  "heartbeat": time.time()})
        d[key] = s
        _write_all(task_id, d)


def submit(task_id: str, key: str, kind: str, fn, total: int = 0) -> bool:
    """Run fn() in a background thread under job `key`. Returns False if a job
    with the same key is already running (different keys run concurrently)."""
    if is_running(task_id, key):
        return False
    with _lock:
        d = _read_raw(task_id)
        d[key] = {"status": "running", "kind": kind, "started_at": time.time(),
                  "heartbeat": time.time(), "progress_done": 0,
                  "progress_total": total, "note": ""}
        _write_all(task_id, d)

    def _run():
        try:
            result = fn()
            with _lock:
                d = _read_raw(task_id)
                s = d.get(key, {})
                s.update({"status": "done", "kind": kind, "finished_at": time.time(),
                          "result": result if isinstance(result, dict) else {}})
                d[key] = s
                _write_all(task_id, d)
            logger.success(f"background job done: task={task_id} key={key} kind={kind}")
        except Exception as e:
            with _lock:
                d = _read_raw(task_id)
                s = d.get(key, {})
                s.update({"status": "error", "kind": kind, "finished_at": time.time(),
                          "error": str(e), "trace": traceback.format_exc()[-1000:]})
                d[key] = s
                _write_all(task_id, d)
            logger.error(f"background job failed: task={task_id} key={key}: {e}")
        finally:
            _threads.pop((task_id, key), None)

    th = threading.Thread(target=_run, daemon=True)
    _threads[(task_id, key)] = th
    th.start()
    return True


def clear(task_id: str, key: str):
    with _lock:
        d = _read_raw(task_id)
        d.pop(key, None)
        _write_all(task_id, d)
    _threads.pop((task_id, key), None)
