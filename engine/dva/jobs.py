"""Tasks: every chat turn, pipeline run, scheduled run and plugin action runs as a task that keeps going when the
window is closed (the app stays in the tray). Status, progress and a log are kept for the Activity page."""
import asyncio
import json
import time

from .db import new_id, now

ACTIVE = ("queued", "running", "waiting")


class Jobs:
    def __init__(self, app):
        self.app = app
        self.db = app.db
        self.tasks = {}
        self.live = {}       # id -> last progress text
        self._last_pub = {}
        for row in self.db.all("SELECT id FROM jobs WHERE status IN ('queued','running','waiting')"):
            self.db.update("jobs", row["id"], {"status": "interrupted", "finished": now(),
                                               "error": "The app was closed while this was running"})

    def create(self, kind, title, coro_factory, chat_id=None, schedule_id=None, harness=None, model=None,
               payload=None, notify=False):
        job_id = new_id("job_")
        row = {"id": job_id, "kind": kind, "title": title[:200], "status": "queued", "chat_id": chat_id,
               "schedule_id": schedule_id, "harness": harness, "model": model, "payload": payload or {},
               "created": now()}
        self.db.insert("jobs", row)
        row = self.db.get("jobs", job_id)
        self.tasks[job_id] = asyncio.create_task(self._run(row, coro_factory, notify))
        self.app.bus.publish("job", id=job_id, status="queued", title=row["title"], job_kind=kind, chat_id=chat_id)
        return row

    async def _run(self, row, coro_factory, notify):
        job_id = row["id"]
        self.set_status(job_id, "running", started=now())
        status, error, result = "done", None, {}
        try:
            result = await coro_factory(row) or {}
        except asyncio.CancelledError:
            status, error = "cancelled", "Stopped"
        except Exception as exc:
            status, error = "failed", f"{type(exc).__name__}: {str(exc)[:500]}"
            self.app.log(f"job {job_id} failed: {error}")
        finally:
            self.tasks.pop(job_id, None)
            self.live.pop(job_id, None)
        self.db.update("jobs", job_id, {"status": status, "error": error, "finished": now(),
                                        "result": result if isinstance(result, dict) else {"value": str(result)}})
        self.app.bus.publish("job", id=job_id, status=status, title=row["title"], job_kind=row["kind"],
                             chat_id=row["chat_id"], error=error)
        if notify:
            self.app.bus.publish("notify", title=("Task finished: " if status == "done" else "Task " + status + ": ")
                                 + row["title"], body=(error or "Open it to see the result."), chat_id=row["chat_id"])

    def set_status(self, job_id, status, **extra):
        self.db.update("jobs", job_id, {"status": status, **extra})
        row = self.db.get("jobs", job_id)
        self.app.bus.publish("job", id=job_id, status=status, title=row["title"], job_kind=row["kind"],
                             chat_id=row["chat_id"], progress=self.live.get(job_id))

    def progress(self, job_id, text):
        self.live[job_id] = text
        t = time.monotonic()
        if t - self._last_pub.get(job_id, 0) > 0.7:
            self._last_pub[job_id] = t
            self.app.bus.publish("job_progress", id=job_id, text=text)

    def log(self, job_id, line):
        path = self.app.paths.data / "jobs" / f"{job_id}.log"
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def log_tail(self, job_id, lines=200):
        path = self.app.paths.data / "jobs" / f"{job_id}.log"
        if not path.exists():
            return ""
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])

    def cancel(self, job_id):
        task = self.tasks.get(job_id)
        if task:
            task.cancel()
            for ap_id, ap in list(self.app.approvals.items()):
                if ap["job_id"] == job_id and not ap["future"].done():
                    ap["future"].set_result({"allow": False})
            return True
        return False

    def running_for_chat(self, chat_id):
        return next((jid for jid, _ in self.tasks.items()
                     if (self.db.get("jobs", jid) or {}).get("chat_id") == chat_id), None)

    def busy_with_model(self):
        return any((self.db.get("jobs", jid) or {}).get("kind") in ("chat", "schedule") for jid in self.tasks)

    def list(self, limit=100, status=None):
        sql = "SELECT * FROM jobs"
        params = []
        if status == "active":
            sql += " WHERE status IN ('queued','running','waiting')"
        sql += " ORDER BY created DESC LIMIT ?"
        params.append(limit)
        rows = self.db.all(sql, params)
        for r in rows:
            r["progress"] = self.live.get(r["id"])
        return rows

    def get(self, job_id):
        row = self.db.get("jobs", job_id)
        if row:
            row["progress"] = self.live.get(job_id)
            row["log"] = self.log_tail(job_id)
        return row

    async def shutdown(self):
        for task in list(self.tasks.values()):
            task.cancel()
        if self.tasks:
            await asyncio.wait(list(self.tasks.values()), timeout=10)
