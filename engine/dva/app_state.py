"""The engine's services wired together (one instance per running app)."""
import asyncio
import time
from datetime import datetime

from .db import DB, new_id, now
from .events import EventBus
from .harness import Harnesses
from .jobs import Jobs
from .llm import LLM
from .mcp import Connectors
from .memory import Memory
from .models import Models
from .paths import Paths
from .plugins import Plugins
from .runtime import Runtime
from .scheduler import Scheduler
from .secrets import Secrets
from .services import Services
from .skills import Skills

STATE = None

DEFAULT_SETTINGS = {
    "default_harness": "agent", "default_compute": "auto", "default_ctx": 8192, "max_reply_tokens": 2048,
    "unload_after_minutes": 20, "memory_enabled": True, "learn_enabled": True, "learn_delay_seconds": 45,
    "allow_python": True, "approval_timeout_minutes": 360, "max_native_tools": 28, "python_timeout_seconds": 180,
}


class App:
    def __init__(self, data_dir=None, resources_dir=None):
        global STATE
        STATE = self
        self.started = time.time()
        self.paths = Paths(data_dir, resources_dir)
        self.db = DB(self.paths.db)
        self.bus = EventBus()
        self.secrets = Secrets(self.paths.data)
        self.runtime = Runtime(self.paths, self.db, self.bus)
        self.llm = LLM(self.runtime)
        self.plugins = Plugins(self.paths, self.db, self.secrets, self.bus)
        self.skills = Skills(self.paths, self.db, self.plugins)
        self.harnesses = Harnesses(self.paths, self.db, self.plugins)
        self.connectors = Connectors(self.db, self.secrets, self.plugins, self.bus)
        self.models = Models(self.paths, self.db, self.bus, self.runtime)
        self.memory = Memory(self)
        self.jobs = Jobs(self)
        self.scheduler = Scheduler(self)
        self.services = Services(self)
        self.approvals = {}
        self._bg = []
        for key, value in DEFAULT_SETTINGS.items():
            if self.db.setting(key) is None:
                self.db.set_setting(key, value)

    def log(self, message):
        line = f"{datetime.now().isoformat(timespec='seconds')} {message}\n"
        with (self.paths.data / "logs" / "engine.log").open("a", encoding="utf-8") as f:
            f.write(line)

    async def start(self):
        self.bus.loop = asyncio.get_running_loop()
        self._bg = [asyncio.create_task(self.scheduler.loop()), asyncio.create_task(self.runtime.idle_watch())]
        self.log(f"engine started; data {self.paths.data}; secrets backend {self.secrets.backend}")

    async def stop(self):
        for t in self._bg:
            t.cancel()
        await self.jobs.shutdown()
        await self.connectors.shutdown()
        await self.services.shutdown()
        await self.runtime.shutdown()
        self.log("engine stopped")

    # ------------------------------------------------------------------ chats
    def create_chat(self, project_id=None, title="", model=None, harness=None, connectors=None, folders=None,
                    origin="user", schedule_id=None, thinking=False):
        project = self.db.get("projects", project_id) if project_id else None
        chat_id = new_id("chat_")
        self.db.insert("chats", {
            "id": chat_id, "project_id": project_id, "title": title,
            "model": model or (project or {}).get("default_model") or None,
            "harness": harness or (project or {}).get("default_harness") or None,
            "connectors": connectors if connectors is not None else ((project or {}).get("connectors") or []),
            "folders": folders or [], "origin": origin, "schedule_id": schedule_id, "thinking": int(bool(thinking)),
            "created": now(), "updated": now()})
        self.bus.publish("chats_changed", project_id=project_id)
        return self.db.get("chats", chat_id)

    def start_turn(self, chat_id, text, attachments=None, kind="chat", schedule_id=None, notify=False, payload=None):
        from .runner import Turn
        chat = self.db.get("chats", chat_id)
        if not chat:
            raise KeyError(chat_id)
        if self.jobs.running_for_chat(chat_id):
            raise ValueError("This chat is still working on the previous message. Wait, or press Stop.")
        msg_id = new_id("msg_")
        self.db.insert("messages", {"id": msg_id, "chat_id": chat_id, "role": "user", "content": text,
                                    "meta": {"attachments": attachments or []}, "created": now()})
        values = {"updated": now()}
        if not chat.get("title"):
            values["title"] = " ".join(text.split())[:70] or "New chat"
        self.db.update("chats", chat_id, values)
        self.bus.publish("chat", chat_id=chat_id, type="user_message", message_id=msg_id, content=text,
                         meta={"attachments": attachments or []})
        self.bus.publish("chats_changed", project_id=chat.get("project_id"))
        harness = chat.get("harness") or self.harnesses.default_id()
        title = (values.get("title") or chat.get("title") or "Chat")
        return self.jobs.create(kind, title, lambda row: Turn(self, chat_id, row).run(), chat_id=chat_id,
                                schedule_id=schedule_id, harness=harness, model=chat.get("model"),
                                payload=payload or {}, notify=notify)
