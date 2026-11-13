"""Local HTTP API for the desktop window. Listens on 127.0.0.1 only and requires the per-launch token the app
generates (header `Authorization: Bearer …`, or `?token=` for event streams and file links)."""
import asyncio
import hmac
import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from . import VERSION
from .app_state import App
from .db import new_id, now
from .events import sse
from .hardware import snapshot
from .runtime import RuntimeError_

TOKEN = os.environ.get("DVA_TOKEN", "")


def create_app(data_dir=None, resources_dir=None, token=None):
    token = token or TOKEN
    state = {}

    @asynccontextmanager
    async def lifespan(_):
        app_ = App(data_dir, resources_dir)
        state["app"] = app_
        await app_.start()
        yield
        await app_.stop()

    api = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    A = lambda: state["app"]  # noqa: E731

    @api.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "testserver"):
            return JSONResponse({"error": "forbidden host"}, 403)
        path = request.url.path
        if path.startswith("/api/") and path != "/api/health":
            supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or \
                request.query_params.get("token", "")
            if not token or not hmac.compare_digest(supplied, token):
                return JSONResponse({"error": "unauthorised"}, 401)
        return await call_next(request)

    @api.exception_handler(ValueError)
    async def bad(_, exc):
        return JSONResponse({"error": str(exc)}, 400)

    @api.exception_handler(KeyError)
    async def missing(_, exc):
        return JSONResponse({"error": f"Not found: {exc}"}, 404)

    @api.exception_handler(RuntimeError_)
    async def conflict(_, exc):
        return JSONResponse({"error": str(exc)}, 409)

    @api.exception_handler(RuntimeError)
    async def runtime_error(_, exc):
        return JSONResponse({"error": str(exc)}, 409)

    async def body(request):
        try:
            return await request.json()
        except Exception:
            return {}

    # ------------------------------------------------------------------ general
    @api.get("/api/health")
    async def health():
        return {"ok": True, "version": VERSION}

    @api.get("/api/bootstrap")
    async def bootstrap():
        a = A()
        settings = {r["key"]: r["value"] for r in a.db.all("SELECT * FROM settings")
                    if not r["key"].startswith(("chat_allow:", "chat_auto_approve:", "connector_approval:"))}
        return {"version": VERSION, "settings": settings, "data_dir": str(a.paths.data),
                "models_dir": str(a.paths.models_dir({"models_dir": a.db.setting("models_dir")})),
                "secrets_backend": a.secrets.backend, "secrets_secure": a.secrets.secure,
                "models": a.models.installed(), "default_model": a.models.default_model(),
                "runtime": {"installed": list(a.runtime.backends()), "status": a.runtime.status, "tag": a.runtime.tag},
                "harnesses": [{k: h.get(k) for k in ("id", "name", "description", "type", "mode", "enabled", "source", "error")}
                              for h in a.harnesses.all()],
                "has_hf_token": a.secrets.has("hf_token")}

    @api.get("/api/settings")
    async def get_settings():
        a = A()
        return {r["key"]: r["value"] for r in a.db.all("SELECT * FROM settings")}

    @api.put("/api/settings")
    async def put_settings(request: Request):
        a = A()
        data = await body(request)
        for key, value in data.items():
            if key == "hf_token":
                a.secrets.set("hf_token", value)
                continue
            if key == "models_dir" and value:
                Path(value).mkdir(parents=True, exist_ok=True)
            a.db.set_setting(key, value)
        a.bus.publish("settings_changed")
        return {"ok": True}

    @api.get("/api/hardware")
    async def hardware():
        return await asyncio.to_thread(snapshot)

    @api.get("/api/events")
    async def events(request: Request, since: int = 0):
        last = request.headers.get("last-event-id")
        start = int(last) if last and last.isdigit() else since

        async def gen():
            async for event in A().bus.stream(start):
                if await request.is_disconnected():
                    break
                yield sse(event)
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @api.post("/api/shutdown")
    async def shutdown():
        asyncio.get_running_loop().call_later(0.3, lambda: os._exit(0))
        await A().stop()
        return {"ok": True}

    # ------------------------------------------------------------------ runtime + models
    @api.get("/api/runtime")
    async def runtime_info():
        return await asyncio.to_thread(A().runtime.info)

    @api.post("/api/runtime/install")
    async def runtime_install(request: Request):
        backend = (await body(request))["backend"]
        asyncio.create_task(A().runtime.install_backend(backend))
        return {"ok": True}

    @api.delete("/api/runtime/{backend}")
    async def runtime_remove(backend: str):
        A().runtime.remove_backend(backend)
        return {"ok": True}

    @api.post("/api/runtime/unload")
    async def runtime_unload():
        await A().runtime.unload()
        return {"ok": True}

    @api.get("/api/runtime/log")
    async def runtime_log():
        return {"log": A().runtime.log_tail(120)}

    @api.get("/api/models")
    async def models_list():
        a = A()
        rows = a.models.installed()
        hw = await asyncio.to_thread(snapshot)
        for r in rows:
            r["fit"] = a.models.fit(r["size"] or 0, r.get("meta"), (r.get("settings") or {}).get("ctx"), hw)
        return {"models": rows, "default": a.models.default_model(), "status": a.runtime.status,
                "downloads": list(a.models.downloads.values())}

    @api.get("/api/models/catalog")
    async def models_catalog():
        return await A().models.catalog()

    @api.get("/api/models/search")
    async def models_search(q: str):
        return {"results": await A().models.search(q)}

    @api.get("/api/models/files")
    async def models_files(repo: str):
        return await A().models.repo_files(repo)

    @api.post("/api/models/download")
    async def models_download(request: Request):
        a, data = A(), await body(request)
        if data.get("catalog_key"):
            item, group = await a.models.resolve_catalog(data["catalog_key"])
            return {"id": a.models.start_download(item["repo"], group, item["name"], item["key"])}
        listing = await a.models.repo_files(data["repo"])
        group = next((g for g in listing["files"] if g["file"] == data["file"]), None)
        if not group:
            raise ValueError("That file is not in the repository")
        return {"id": a.models.start_download(data["repo"], group, data.get("name"))}

    @api.get("/api/downloads")
    async def downloads():
        return {"downloads": list(A().models.downloads.values())}

    @api.delete("/api/downloads/{dl_id}")
    async def cancel_download(dl_id: str):
        A().models.cancel_download(dl_id)
        return {"ok": True}

    @api.post("/api/models/import")
    async def models_import(request: Request):
        data = await body(request)
        return await asyncio.to_thread(A().models.import_file, data["path"], data.get("name"))

    @api.get("/api/models/local")
    async def models_local():
        return await asyncio.to_thread(A().models.scan_local)

    @api.put("/api/models/{model_id}")
    async def models_update(model_id: str, request: Request):
        a, data = A(), await body(request)
        if data.pop("make_default", False):
            a.db.set_setting("default_model", model_id)
        if "name" in data:
            a.db.update("models", model_id, {"name": data.pop("name")})
        return a.models.update_settings(model_id, data)

    @api.delete("/api/models/{model_id}")
    async def models_delete(model_id: str):
        A().models.delete(model_id)
        return {"ok": True}

    @api.post("/api/models/{model_id}/load")
    async def models_load(model_id: str, request: Request):
        a, data = A(), await body(request)
        async with a.runtime.lease(model_id, data.get("compute")):
            pass
        return a.runtime.status

    @api.post("/api/models/{model_id}/benchmark")
    async def models_benchmark(model_id: str):
        a = A()
        return await a.models.benchmark(model_id, a.llm)

    # ------------------------------------------------------------------ projects
    @api.get("/api/projects")
    async def projects():
        rows = A().db.all("SELECT p.*, (SELECT COUNT(*) FROM chats c WHERE c.project_id=p.id AND c.archived=0) AS chat_count "
                          "FROM projects p WHERE archived=0 ORDER BY updated DESC")
        return {"projects": rows}

    @api.post("/api/projects")
    async def project_create(request: Request):
        a, data = A(), await body(request)
        pid = new_id("prj_")
        a.db.insert("projects", {"id": pid, "name": data.get("name") or "New project",
                                 "instructions": data.get("instructions", ""), "folders": data.get("folders") or [],
                                 "default_model": data.get("default_model"), "default_harness": data.get("default_harness"),
                                 "connectors": data.get("connectors") or [], "created": now(), "updated": now()})
        a.bus.publish("projects_changed")
        return a.db.get("projects", pid)

    @api.get("/api/projects/{pid}")
    async def project_get(pid: str):
        a = A()
        row = a.db.get("projects", pid)
        if not row:
            raise KeyError(pid)
        row["chats"] = a.db.all("SELECT id, title, updated, origin FROM chats WHERE project_id=? AND archived=0 "
                                "ORDER BY updated DESC", (pid,))
        row["memories"] = a.memory.list("project", pid)
        return row

    @api.put("/api/projects/{pid}")
    async def project_update(pid: str, request: Request):
        a, data = A(), await body(request)
        allowed = {k: v for k, v in data.items() if k in ("name", "instructions", "folders", "default_model",
                                                         "default_harness", "connectors", "memory_enabled", "archived")}
        for k in ("memory_enabled", "archived"):
            if k in allowed:
                allowed[k] = int(bool(allowed[k]))
        allowed["updated"] = now()
        a.db.update("projects", pid, allowed)
        a.bus.publish("projects_changed")
        return a.db.get("projects", pid)

    @api.delete("/api/projects/{pid}")
    async def project_delete(pid: str):
        a = A()
        a.db.run("UPDATE chats SET project_id=NULL WHERE project_id=?", (pid,))
        a.db.run("DELETE FROM memories WHERE scope='project' AND scope_id=?", (pid,))
        a.db.delete("projects", pid)
        a.bus.publish("projects_changed")
        return {"ok": True}

    # ------------------------------------------------------------------ chats
    @api.get("/api/chats")
    async def chats(project_id: str = "", q: str = "", limit: int = 200):
        a = A()
        sql = "SELECT id, project_id, title, model, harness, origin, pinned, updated FROM chats WHERE archived=0"
        params = []
        if project_id == "none":
            sql += " AND project_id IS NULL"
        elif project_id:
            sql += " AND project_id=?"
            params.append(project_id)
        if q:
            sql += " AND (title LIKE ? OR id IN (SELECT chat_id FROM messages WHERE content LIKE ?))"
            params += [f"%{q}%", f"%{q}%"]
        sql += " ORDER BY pinned DESC, updated DESC LIMIT ?"
        params.append(limit)
        rows = a.db.all(sql, params)
        running = {a.db.get("jobs", jid)["chat_id"] for jid in a.jobs.tasks if a.db.get("jobs", jid)}
        for r in rows:
            r["running"] = r["id"] in running
        return {"chats": rows}

    @api.post("/api/chats")
    async def chat_create(request: Request):
        data = await body(request)
        return A().create_chat(project_id=data.get("project_id"), title=data.get("title", ""),
                               model=data.get("model"), harness=data.get("harness"),
                               connectors=data.get("connectors"), folders=data.get("folders"),
                               thinking=data.get("thinking", False))

    @api.get("/api/chats/{chat_id}")
    async def chat_get(chat_id: str):
        a = A()
        chat = a.db.get("chats", chat_id)
        if not chat:
            raise KeyError(chat_id)
        chat["messages"] = a.db.all("SELECT * FROM messages WHERE chat_id=? ORDER BY created, rowid", (chat_id,))
        chat["running_job"] = a.jobs.running_for_chat(chat_id)
        chat["pending_approvals"] = [{"approval_id": k, **{x: v[x] for x in ("tool", "description")}}
                                     for k, v in a.approvals.items() if v["chat_id"] == chat_id]
        chat["auto_approve"] = bool(a.db.setting(f"chat_auto_approve:{chat_id}", False))
        chat["memories"] = a.memory.list("chat", chat_id)
        chat["workspace"] = str(a.paths.workspace(chat_id))
        return chat

    @api.put("/api/chats/{chat_id}")
    async def chat_update(chat_id: str, request: Request):
        a, data = A(), await body(request)
        if "auto_approve" in data:
            a.db.set_setting(f"chat_auto_approve:{chat_id}", bool(data.pop("auto_approve")))
        allowed = {k: v for k, v in data.items() if k in ("title", "model", "harness", "connectors", "folders",
                                                         "thinking", "project_id", "pinned", "archived")}
        for k in ("thinking", "pinned", "archived"):
            if k in allowed:
                allowed[k] = int(bool(allowed[k]))
        if allowed:
            a.db.update("chats", chat_id, allowed)
        a.bus.publish("chats_changed")
        return await chat_get(chat_id)

    @api.delete("/api/chats/{chat_id}")
    async def chat_delete(chat_id: str):
        a = A()
        jid = a.jobs.running_for_chat(chat_id)
        if jid:
            a.jobs.cancel(jid)
        a.db.run("DELETE FROM messages WHERE chat_id=?", (chat_id,))
        a.db.run("DELETE FROM memories WHERE scope='chat' AND scope_id=?", (chat_id,))
        a.db.delete("chats", chat_id)
        a.bus.publish("chats_changed")
        return {"ok": True}

    @api.post("/api/chats/{chat_id}/messages")
    async def chat_send(chat_id: str, request: Request):
        data = await body(request)
        text = (data.get("text") or "").strip()
        if not text and not data.get("attachments"):
            raise ValueError("Type a message")
        job = A().start_turn(chat_id, text, data.get("attachments") or [])
        return {"job_id": job["id"]}

    @api.post("/api/chats/{chat_id}/retry")
    async def chat_retry(chat_id: str):
        a = A()
        rows = a.db.all("SELECT * FROM messages WHERE chat_id=? ORDER BY created, rowid", (chat_id,))
        last_user = next((r for r in reversed(rows) if r["role"] == "user"), None)
        if not last_user:
            raise ValueError("Nothing to retry")
        for r in rows[rows.index(last_user):]:
            a.db.delete("messages", r["id"])
        job = a.start_turn(chat_id, last_user["content"], (last_user.get("meta") or {}).get("attachments") or [])
        return {"job_id": job["id"]}

    @api.post("/api/chats/{chat_id}/stop")
    async def chat_stop(chat_id: str):
        a = A()
        jid = a.jobs.running_for_chat(chat_id)
        return {"stopped": a.jobs.cancel(jid) if jid else False}

    @api.post("/api/chats/{chat_id}/upload")
    async def chat_upload(chat_id: str, file: UploadFile = File(...)):
        a = A()
        folder = a.paths.workspace(chat_id) / "uploads"
        folder.mkdir(exist_ok=True)
        name = Path(file.filename or "file").name
        dest = folder / name
        n = 1
        while dest.exists():
            dest = folder / f"{Path(name).stem}-{n}{Path(name).suffix}"
            n += 1
        size = 0
        with dest.open("wb") as f:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > 500 * 2**20:
                    raise ValueError("Files up to 500 MB")
                f.write(chunk)
        textlike = (mimetypes.guess_type(name)[0] or "").startswith("text") or Path(name).suffix.lower() in (
            ".csv", ".md", ".json", ".txt", ".py", ".r", ".tsv", ".yaml", ".yml", ".sql", ".js", ".ts")
        return {"name": dest.name, "path": str(dest), "size": size, "inline": textlike and size <= 12000}

    @api.get("/api/chats/{chat_id}/files")
    async def chat_files(chat_id: str):
        root = A().paths.workspace(chat_id)
        files = [{"path": str(p), "name": str(p.relative_to(root)), "size": p.stat().st_size}
                 for p in sorted(root.rglob("*")) if p.is_file() and not any(x.startswith(".") for x in p.relative_to(root).parts)]
        return {"root": str(root), "files": files[:500]}

    @api.get("/api/file")
    async def get_file(path: str):
        a = A()
        p = Path(path).resolve()
        roots = [a.paths.data.resolve()]
        for prj in a.db.all("SELECT folders_json FROM projects"):
            roots += [Path(f).resolve() for f in (prj["folders"] or [])]
        for c in a.db.all("SELECT folders_json FROM chats"):
            roots += [Path(f).resolve() for f in (c["folders"] or [])]
        if not any(p == r or r in p.parents for r in roots) or not p.is_file():
            raise KeyError(path)
        if p.name in ("secrets.json",) or p.suffix == ".sqlite3":
            raise KeyError(path)
        return FileResponse(p)

    @api.post("/api/approvals/{approval_id}")
    async def approve(approval_id: str, request: Request):
        data = await body(request)
        ap = A().approvals.get(approval_id)
        if not ap:
            raise KeyError(approval_id)
        if not ap["future"].done():
            ap["future"].set_result({"allow": bool(data.get("allow")), "always": bool(data.get("always"))})
        return {"ok": True}

    # ------------------------------------------------------------------ memory
    @api.get("/api/memory")
    async def memory_list(scope: str = "", scope_id: str = None):
        a = A()
        rows = a.memory.list(scope or None, scope_id)
        names = {p["id"]: p["name"] for p in a.db.all("SELECT id, name FROM projects")}
        chats_ = {c["id"]: c["title"] for c in a.db.all("SELECT id, title FROM chats")}
        hnames = {h["id"]: h["name"] for h in a.harnesses.all()}
        for r in rows:
            r["scope_name"] = names.get(r["scope_id"]) or chats_.get(r["scope_id"]) or hnames.get(r["scope_id"]) or ""
        return {"memories": rows, "enabled": a.db.setting("memory_enabled", True),
                "learn": a.db.setting("learn_enabled", True)}

    @api.post("/api/memory")
    async def memory_add(request: Request):
        data = await body(request)
        row, _ = A().memory.add(data.get("scope", "global"), data["text"], data.get("scope_id", ""),
                                data.get("kind", "fact"), "user")
        return row

    @api.put("/api/memory/{mid}")
    async def memory_update(mid: str, request: Request):
        return A().memory.update(mid, await body(request))

    @api.delete("/api/memory/{mid}")
    async def memory_delete(mid: str):
        A().memory.delete(mid)
        return {"ok": True}

    @api.post("/api/memory/learn/{chat_id}")
    async def memory_learn(chat_id: str):
        saved = await A().memory.learn(chat_id)
        return {"saved": saved}

    # ------------------------------------------------------------------ skills
    @api.get("/api/skills")
    async def skills():
        a = A()
        return {"skills": a.skills.all(),
                "suggestions": a.db.all("SELECT * FROM skill_suggestions WHERE status='open' ORDER BY created DESC")}

    @api.get("/api/skills/{skill_id:path}/detail")
    async def skill_get(skill_id: str):
        s = A().skills.get(skill_id)
        if not s:
            raise KeyError(skill_id)
        return s

    @api.post("/api/skills")
    async def skill_create(request: Request):
        a, data = A(), await body(request)
        s = a.skills.save(data["name"], data.get("description", ""), data.get("body", ""))
        a.bus.publish("skills_changed")
        return s

    @api.put("/api/skills/{skill_id:path}/raw")
    async def skill_save(skill_id: str, request: Request):
        a = A()
        s = a.skills.save_raw(skill_id, (await body(request))["raw"])
        a.bus.publish("skills_changed")
        return s

    @api.put("/api/skills/{skill_id:path}/enabled")
    async def skill_enable(skill_id: str, request: Request):
        A().db.set_item_enabled("skill", skill_id, (await body(request)).get("enabled", True))
        return {"ok": True}

    @api.post("/api/skills/{skill_id:path}/copy")
    async def skill_copy(skill_id: str):
        return A().skills.copy_to_user(skill_id)

    @api.delete("/api/skills/{skill_id:path}/delete")
    async def skill_delete(skill_id: str):
        A().skills.delete(skill_id)
        return {"ok": True}

    @api.post("/api/skills-upload")
    async def skill_upload(file: UploadFile = File(...)):
        return A().skills.install_upload(file.filename or "skill.md", await file.read())

    @api.post("/api/skill-suggestions/{sid}/{action}")
    async def skill_suggestion(sid: str, action: str):
        a = A()
        sug = a.db.get("skill_suggestions", sid)
        if not sug:
            raise KeyError(sid)
        if action == "apply":
            skill = a.skills.get(sug["skill"])
            if not skill:
                raise ValueError("That skill no longer exists")
            if not skill["editable"]:
                skill = a.skills.copy_to_user(skill["id"])
                skill = a.skills.get(skill["id"])
            body_text = skill["body"].rstrip()
            if "## Learned adjustments" not in body_text:
                body_text += "\n\n## Learned adjustments"
            body_text += f"\n- {sug['suggestion']}"
            a.skills.save(skill["name"], skill["description"], body_text, skill_id=Path(skill["dir"]).name)
        a.db.update("skill_suggestions", sid, {"status": "applied" if action == "apply" else "dismissed"})
        a.bus.publish("skills_changed")
        return {"ok": True}

    # ------------------------------------------------------------------ harnesses
    @api.get("/api/harnesses")
    async def harnesses():
        return {"harnesses": A().harnesses.all(), "default": A().harnesses.default_id()}

    @api.get("/api/harnesses/{hid:path}/detail")
    async def harness_get(hid: str):
        h = A().harnesses.get(hid)
        if not h:
            raise KeyError(hid)
        h["lessons"] = A().memory.list("harness", hid)
        return h

    @api.post("/api/harnesses")
    async def harness_create(request: Request):
        data = await body(request)
        return A().harnesses.save(data["spec"], data.get("prompt"))

    @api.put("/api/harnesses/{hid:path}/spec")
    async def harness_update(hid: str, request: Request):
        data = await body(request)
        return A().harnesses.update(hid, data.get("spec", {}), data.get("prompt"))

    @api.put("/api/harnesses/{hid:path}/enabled")
    async def harness_enable(hid: str, request: Request):
        A().db.set_item_enabled("harness", hid, (await body(request)).get("enabled", True))
        return {"ok": True}

    @api.post("/api/harnesses/{hid:path}/copy")
    async def harness_copy(hid: str):
        return A().harnesses.copy_to_user(hid)

    @api.delete("/api/harnesses/{hid:path}/delete")
    async def harness_delete(hid: str):
        A().harnesses.delete(hid)
        return {"ok": True}

    @api.post("/api/harnesses-upload")
    async def harness_upload(file: UploadFile = File(...)):
        return A().harnesses.install_upload(file.filename or "harness.json", await file.read())

    # ------------------------------------------------------------------ plugins
    @api.get("/api/plugins")
    async def plugins():
        a = A()
        rows = []
        for p in a.plugins.all():
            item = {k: v for k, v in p.items()}
            if not p.get("error"):
                item["values"] = a.plugins.settings(p)
                item["skills"] = [s for s in a.skills.all() if s["plugin"] == p["id"]]
                item["harness_list"] = [h for h in a.harnesses.all() if h["plugin"] == p["id"]]
            rows.append(item)
        return {"plugins": rows}

    @api.put("/api/plugins/{pid}/enabled")
    async def plugin_enable(pid: str, request: Request):
        A().plugins.set_enabled(pid, (await body(request)).get("enabled", True))
        return {"ok": True}

    @api.put("/api/plugins/{pid}/settings")
    async def plugin_settings(pid: str, request: Request):
        a = A()
        values = a.plugins.save_settings(pid, await body(request))
        for c in a.plugins.get(pid).get("connectors", []):
            await a.connectors.reset(f"{pid}:{c['id']}")
        return values

    @api.post("/api/plugins/{pid}/setup")
    async def plugin_setup(pid: str):
        return await A().plugins.run_setup(pid)

    @api.post("/api/plugins/{pid}/actions/{action_id}")
    async def plugin_action(pid: str, action_id: str):
        a = A()
        plugin = a.plugins.get(pid)
        action = next((x for x in plugin.get("actions", []) if x["id"] == action_id), None)
        if not action:
            raise KeyError(action_id)

        async def go(row):
            out = await a.plugins.run_script(pid, action["run"], action.get("args", []), timeout=3600,
                                             on_line=lambda line: (a.jobs.log(row["id"], line),
                                                                   a.jobs.progress(row["id"], line[-160:])))
            if out["code"] != 0:
                raise RuntimeError((out["output"] or "failed")[-400:])
            return {"output": out["output"][-4000:], "result": out["result"]}
        job = a.jobs.create("action", f"{plugin['name']}: {action['label']}", go, notify=True)
        return {"job_id": job["id"]}

    @api.delete("/api/plugins/{pid}")
    async def plugin_delete(pid: str):
        A().plugins.uninstall(pid)
        return {"ok": True}

    @api.post("/api/plugins-upload")
    async def plugin_upload(file: UploadFile = File(...)):
        return A().plugins.install_zip(await file.read())

    @api.post("/api/plugins-create")
    async def plugin_create(request: Request):
        a, data = A(), await body(request)
        conns = [c for c in a.connectors.all() if c["id"] in (data.get("connectors") or []) and c["source"] == "user"]
        return a.plugins.create(data["name"], data.get("description", ""), data.get("skills") or [],
                                data.get("harnesses") or [], skills_mgr=a.skills, harness_mgr=a.harnesses,
                                db_connectors=conns)

    @api.get("/api/plugins/{pid}/export")
    async def plugin_export(pid: str):
        path = A().plugins.export_zip(pid)
        return FileResponse(path, filename=path.name)

    # ------------------------------------------------------------------ connectors
    @api.get("/api/connectors")
    async def connectors():
        from .mcp import PRESETS
        a = A()
        rows = a.connectors.all()
        for r in rows:
            r["approval"] = a.connectors.approval_mode(r)
        return {"connectors": rows, "presets": PRESETS}

    @api.post("/api/connectors")
    async def connector_create(request: Request):
        return A().connectors.create(await body(request))

    @api.put("/api/connectors/{cid:path}/config")
    async def connector_update(cid: str, request: Request):
        return await A().connectors.update(cid, await body(request))

    @api.delete("/api/connectors/{cid:path}/delete")
    async def connector_delete(cid: str):
        await A().connectors.delete(cid)
        return {"ok": True}

    @api.post("/api/connectors/{cid:path}/test")
    async def connector_test(cid: str):
        try:
            return await A().connectors.test(cid)
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:400]}"}

    # ------------------------------------------------------------------ schedules
    @api.get("/api/schedules")
    async def schedules():
        return {"schedules": A().scheduler.list()}

    @api.post("/api/schedules/parse")
    async def schedule_parse(request: Request):
        from .scheduler import describe, next_after, parse_when
        cron = parse_when((await body(request)).get("when", ""))
        return {"cron": cron, "when": describe(cron), "next": next_after(cron).isoformat(timespec="minutes")}

    @api.post("/api/schedules")
    async def schedule_create(request: Request):
        return A().scheduler.create(await body(request))

    @api.put("/api/schedules/{sid}")
    async def schedule_update(sid: str, request: Request):
        return A().scheduler.update(sid, await body(request))

    @api.delete("/api/schedules/{sid}")
    async def schedule_delete(sid: str):
        A().scheduler.delete(sid)
        return {"ok": True}

    @api.post("/api/schedules/{sid}/run")
    async def schedule_run(sid: str):
        job = A().scheduler.run_now(sid)
        return {"job_id": job["id"], "chat_id": job["chat_id"]}

    # ------------------------------------------------------------------ jobs
    @api.get("/api/jobs")
    async def jobs(status: str = ""):
        return {"jobs": A().jobs.list(status=status or None)}

    @api.get("/api/jobs/{job_id}")
    async def job_get(job_id: str):
        row = A().jobs.get(job_id)
        if not row:
            raise KeyError(job_id)
        return row

    @api.post("/api/jobs/{job_id}/cancel")
    async def job_cancel(job_id: str):
        return {"cancelled": A().jobs.cancel(job_id)}

    # ------------------------------------------------------------------ the window's files
    ui_dir = os.environ.get("DVA_UI_DIR")
    if ui_dir and Path(ui_dir).exists():
        root = Path(ui_dir).resolve()

        @api.get("/{path:path}")
        async def ui(path: str):
            target = (root / path).resolve()
            if path and target.is_file() and root in target.parents:
                return FileResponse(target)
            return FileResponse(root / "index.html")

    return api
