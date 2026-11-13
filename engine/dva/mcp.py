"""Connectors: MCP servers the agent can use (remote over HTTPS, or local programs over stdio).

Each connector has one long-lived session owned by its own background task (MCP sessions must be opened and
closed in the same task), restarted automatically after an error. Tool lists are cached."""
import asyncio
import json
import os
import re
import sys
import time

from .db import new_id, now

READONLY_NAME = re.compile(r"^(get|list|search|find|read|inspect|validate|whoami|suggest|describe|query|fetch|show|"
                           r"browser_(snapshot|take_screenshot|console|network|tabs))", re.I)
RESULT_LIMIT = 12_000

PRESETS = [
    {"key": "browser", "name": "Browser (Playwright)", "transport": "stdio", "command": "{node}",
     "args": ["{playwright_mcp}", "--browser", "msedge", "--isolated"],
     "description": "Lets the agent open and read web pages in a private Microsoft Edge window "
                    "(clicks, forms, screenshots). Many tools: best with larger models."},
    {"key": "custom-http", "name": "Custom (HTTPS URL)", "transport": "http", "url": "", "auth": "none",
     "description": "Any MCP server reachable at an https:// address."},
    {"key": "custom-stdio", "name": "Custom (local program)", "transport": "stdio", "command": "", "args": [],
     "description": "A local MCP server started with a command, e.g. npx or uvx (Node.js / uv must be installed)."},
]


def safe_name(text):
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", text)[:40].strip("_") or "c"


def tool_key(connector_id, tool_name):
    return f"mcp__{safe_name(connector_id)}__{safe_name(tool_name)}"[:64]


def result_text(result):
    if getattr(result, "structured_content", None):
        text = json.dumps(result.structured_content, ensure_ascii=False, default=str)
    else:
        parts = []
        for c in getattr(result, "content", None) or []:
            if getattr(c, "type", "") == "text":
                parts.append(c.text)
            elif getattr(c, "type", "") == "image":
                parts.append("[image returned]")
            else:
                parts.append(str(c)[:500])
        text = "\n".join(parts)
    if len(text) > RESULT_LIMIT:
        text = text[:RESULT_LIMIT] + f"\n…(trimmed {len(text) - RESULT_LIMIT:,} characters)"
    return text


class Session:
    def __init__(self, conf):
        self.conf = conf
        self.queue = asyncio.Queue()
        self.task = None
        self.ready = asyncio.Event()
        self.error = None

    def _client(self):
        from fastmcp import Client
        c = self.conf
        if c["transport"] == "http":
            if not str(c.get("url", "")).startswith(("https://", "http://127.0.0.1", "http://localhost")):
                raise ValueError("Remote connectors must use https://")
            return Client(c["url"], auth=c.get("token") or None, timeout=120)
        from fastmcp.client.transports import StdioTransport
        env = {**os.environ, **(c.get("env") or {})}
        env.pop("DVA_TOKEN", None)
        command, args = expand(c.get("command") or ""), [expand(a) for a in (c.get("args") or [])]
        if not command:
            raise ValueError("This connector has no command")
        if os.environ.get("DVA_NODE_IS_ELECTRON") and command == os.environ.get("DVA_NODE"):
            env["ELECTRON_RUN_AS_NODE"] = "1"  # the app's own Electron runs Node programs; no Node.js install needed
        return Client(StdioTransport(command=command, args=args, env=env, keep_alive=False), timeout=120)

    async def _run(self):
        try:
            async with self._client() as client:
                self.error = None
                self.ready.set()
                while True:
                    fut, fn = await self.queue.get()
                    if fn is None:
                        fut.set_result(None)
                        return
                    try:
                        fut.set_result(await fn(client))
                    except Exception as exc:  # tool errors stay inside the session
                        if not fut.done():
                            fut.set_exception(exc)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {str(exc)[:300]}"
            self.ready.set()
            while not self.queue.empty():
                fut, _ = self.queue.get_nowait()
                if not fut.done():
                    fut.set_exception(ConnectionError(self.error))

    async def call(self, fn, timeout=180):
        if not self.task or self.task.done():
            self.ready = asyncio.Event()
            self.task = asyncio.create_task(self._run())
        try:
            await asyncio.wait_for(self.ready.wait(), timeout=90)
        except asyncio.TimeoutError:
            raise ConnectionError("The connector did not start within 90 seconds")
        if self.error and self.task.done():
            raise ConnectionError(self.error)
        fut = asyncio.get_running_loop().create_future()
        await self.queue.put((fut, fn))
        return await asyncio.wait_for(fut, timeout)

    async def close(self):
        if self.task and not self.task.done():
            fut = asyncio.get_running_loop().create_future()
            await self.queue.put((fut, None))
            try:
                await asyncio.wait_for(fut, 10)
            except Exception:
                self.task.cancel()


def expand(value, env=None):
    value = value.replace("{node}", os.environ.get("DVA_NODE") or "node")
    value = value.replace("{playwright_mcp}", os.environ.get("DVA_PLAYWRIGHT_MCP", ""))
    return value.replace("{python}", sys.executable)


class Connectors:
    def __init__(self, db, secrets, plugins, bus):
        self.db, self.secrets, self.plugins, self.bus = db, secrets, plugins, bus
        self.sessions = {}
        self.tool_cache = {}  # id -> (time, tools)

    # ------------------------------------------------------------------ configuration
    def all(self, reveal=False):
        out = []
        for row in self.db.all("SELECT * FROM connectors ORDER BY name"):
            token = self.secrets.get(f"connector:{row['id']}") if row["auth"] == "bearer" else None
            out.append({**row, "source": "user", "enabled": bool(row["enabled"]),
                        "token": token if reveal else None, "has_token": bool(token),
                        "ready": row["auth"] != "bearer" or bool(token)})
        for conf in self.plugins.connectors():
            item = dict(conf)
            item["has_token"] = bool(item.get("token"))
            if not reveal:
                item["token"] = None
            item.setdefault("approval", "changes")
            out.append(item)
        for item in out:
            cached = self.tool_cache.get(item["id"])
            item["tool_count"] = len(cached[1]) if cached else len(item.get("tools") or [])
        return out

    def get(self, connector_id, reveal=True):
        return next((c for c in self.all(reveal=reveal) if c["id"] == connector_id), None)

    def create(self, data):
        cid = new_id("c_")
        token = data.pop("token", None)
        self.db.insert("connectors", {
            "id": cid, "name": data["name"], "transport": data["transport"], "url": data.get("url", ""),
            "command": data.get("command", ""), "args": data.get("args") or [], "env": data.get("env") or {},
            "auth": data.get("auth", "none"), "approval": data.get("approval", "changes"),
            "description": data.get("description", ""), "enabled": 1, "created": now(), "updated": now()})
        if token:
            self.secrets.set(f"connector:{cid}", token)
        self.bus.publish("connectors_changed")
        return self.get(cid, reveal=False)

    async def update(self, connector_id, data):
        if ":" in connector_id:  # plugin connector: only enable/disable here
            if "enabled" in data:
                self.db.set_item_enabled("connector", connector_id, data["enabled"])
            if "approval" in data:
                self.db.set_setting(f"connector_approval:{connector_id}", data["approval"])
        else:
            token = data.pop("token", None)
            allowed = {k: v for k, v in data.items() if k in ("name", "url", "command", "args", "env", "auth",
                                                             "approval", "description", "enabled")}
            if "enabled" in allowed:
                allowed["enabled"] = int(bool(allowed["enabled"]))
            allowed["updated"] = now()
            self.db.update("connectors", connector_id, allowed)
            if token:
                self.secrets.set(f"connector:{connector_id}", token)
        await self.reset(connector_id)
        self.bus.publish("connectors_changed")
        return self.get(connector_id, reveal=False)

    async def delete(self, connector_id):
        await self.reset(connector_id)
        self.db.delete("connectors", connector_id)
        self.secrets.delete(f"connector:{connector_id}")
        self.bus.publish("connectors_changed")

    async def reset(self, connector_id):
        session = self.sessions.pop(connector_id, None)
        self.tool_cache.pop(connector_id, None)
        if session:
            await session.close()

    def approval_mode(self, conf):
        return self.db.setting(f"connector_approval:{conf['id']}") or conf.get("approval") or "changes"

    # ------------------------------------------------------------------ use
    def _session(self, conf):
        s = self.sessions.get(conf["id"])
        if not s:
            s = self.sessions[conf["id"]] = Session(conf)
        return s

    async def list_tools(self, connector_id, refresh=False):
        cached = self.tool_cache.get(connector_id)
        if cached and not refresh and time.time() - cached[0] < 600:
            return cached[1]
        conf = self.get(connector_id)
        if not conf:
            raise KeyError(connector_id)
        if not conf.get("ready", True):
            raise ConnectionError(f"{conf['name']} needs its key or token first")
        tools = await self._session(conf).call(lambda client: client.list_tools(), timeout=90)
        rows = []
        for t in tools:
            ann = getattr(t, "annotations", None)
            hint = None
            if ann is not None:  # MCP SDK v2 renamed the fields; support both
                hint = getattr(ann, "read_only_hint", None) if hasattr(type(ann), "read_only_hint") or \
                    "read_only_hint" in getattr(ann, "model_fields", {}) else getattr(ann, "readOnlyHint", None)
            readonly = bool(hint)
            schema = getattr(t, "input_schema", None) if "input_schema" in getattr(t, "model_fields", {}) else \
                getattr(t, "inputSchema", None)
            rows.append({"name": t.name, "description": (t.description or "")[:600],
                         "parameters": schema or {"type": "object", "properties": {}},
                         "readonly": readonly or bool(READONLY_NAME.match(t.name))})
        self.tool_cache[connector_id] = (time.time(), rows)
        if ":" not in connector_id:
            self.db.update("connectors", connector_id, {"tools": [{"name": r["name"], "description": r["description"][:200]} for r in rows]})
        return rows

    async def call(self, connector_id, tool_name, arguments, timeout=300):
        conf = self.get(connector_id)
        result = await self._session(conf).call(
            lambda client: client.call_tool(tool_name, arguments or {}, raise_on_error=False), timeout=timeout)
        text = result_text(result)
        if getattr(result, "is_error", False):
            return False, text or "The connector reported an error"
        return True, text

    async def test(self, connector_id):
        await self.reset(connector_id)
        tools = await self.list_tools(connector_id, refresh=True)
        return {"ok": True, "tools": tools}

    async def shutdown(self):
        for cid in list(self.sessions):
            await self.reset(cid)
