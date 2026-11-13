"""Plugins bundle connectors (MCP servers), skills, harnesses, setup steps, actions and schedule templates.

A plugin is a folder with plugin.json. Settings marked "secret" are kept in the OS credential store. A plugin may
ship a setup script (run with the app's Python) that checks the settings and returns choices or updates, and
actions (buttons) that run scripts, e.g. "Sync my catalogue"."""
import asyncio
import io
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from jsonschema import Draft202012Validator

from .db import now
from .paths import BUILTIN

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
SCHEMA = {
    "type": "object", "required": ["id", "name", "version"],
    "properties": {
        "id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{1,40}$"},
        "name": {"type": "string"}, "version": {"type": "string"}, "description": {"type": "string"},
        "author": {"type": "string"}, "homepage": {"type": "string"},
        "settings": {"type": "array", "items": {"type": "object", "required": ["key", "label"], "properties": {
            "key": {"type": "string"}, "label": {"type": "string"}, "help": {"type": "string"},
            "type": {"enum": ["text", "secret", "select", "boolean", "textarea", "folder"]},
            "required": {"type": "boolean"}, "default": {}, "options": {"type": "array"}}}},
        "connectors": {"type": "array", "items": {"type": "object", "required": ["id", "name", "transport"]}},
        "setup": {"type": "string"},
        "actions": {"type": "array", "items": {"type": "object", "required": ["id", "label", "run"]}},
        "schedule_templates": {"type": "array"},
        "default_enabled": {"type": "boolean"},
    },
}
VALIDATOR = Draft202012Validator(SCHEMA)


class Plugins:
    def __init__(self, paths, db, secrets, bus):
        self.paths, self.db, self.secrets, self.bus = paths, db, secrets, bus

    def _dirs(self):
        for root, source in ((BUILTIN / "plugins", "builtin"), (self.paths.data / "plugins", "user")):
            if root.exists():
                for pj in sorted(root.glob("*/plugin.json")):
                    yield source, pj

    def all(self):
        out = {}
        for source, pj in self._dirs():
            try:
                manifest = json.loads(pj.read_text(encoding="utf-8"))
                errors = list(VALIDATOR.iter_errors(manifest))
                if errors:
                    raise ValueError(errors[0].message)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                out[pj.parent.name] = {"id": pj.parent.name, "name": pj.parent.name, "error": str(exc)[:200],
                                       "dir": str(pj.parent), "source": source, "enabled": False}
                continue
            state = self.db.get("plugin_state", manifest["id"]) or {}
            enabled = bool(state["enabled"]) if state else manifest.get("default_enabled", True)
            out[manifest["id"]] = {**manifest, "dir": str(pj.parent), "source": source, "enabled": enabled,
                                   "setup_state": (state.get("setup") or {}), "configured": self.configured(manifest)}
        return list(out.values())

    def get(self, plugin_id):
        return next((p for p in self.all() if p["id"] == plugin_id), None)

    def enabled(self):
        return [p for p in self.all() if p.get("enabled") and not p.get("error")]

    def set_enabled(self, plugin_id, enabled):
        self._state(plugin_id, enabled=int(bool(enabled)))
        self.bus.publish("plugins_changed")

    def _state(self, plugin_id, **values):
        if not self.db.get("plugin_state", plugin_id):
            self.db.insert("plugin_state", {"id": plugin_id, "enabled": 1, "settings": {}, "setup": {}, "updated": now()})
        self.db.update("plugin_state", plugin_id, {**values, "updated": now()})

    # ------------------------------------------------------------------ settings
    def _secret_name(self, plugin_id, key):
        return f"plugin:{plugin_id}:{key}"

    def settings(self, plugin, reveal=False):
        state = self.db.get("plugin_state", plugin["id"]) or {}
        stored = state.get("settings") or {}
        values = {}
        for field in plugin.get("settings", []):
            key = field["key"]
            if field.get("type") == "secret":
                secret = self.secrets.get(self._secret_name(plugin["id"], key))
                values[key] = secret if reveal else bool(secret)
            else:
                values[key] = stored.get(key, field.get("default"))
        for key, value in stored.items():  # values set by the setup script (e.g. account name)
            values.setdefault(key, value)
        return values

    def configured(self, plugin):
        values = self.settings(plugin)
        return all(values.get(f["key"]) not in (None, "", False) for f in plugin.get("settings", [])
                   if f.get("required"))

    def save_settings(self, plugin_id, values):
        plugin = self.get(plugin_id)
        if not plugin:
            raise KeyError(plugin_id)
        state = self.db.get("plugin_state", plugin_id) or {}
        stored = dict(state.get("settings") or {})
        fields = {f["key"]: f for f in plugin.get("settings", [])}
        for key, value in values.items():
            field = fields.get(key)
            if field and field.get("type") == "secret":
                if value is None:                     # explicit "remove the key"
                    self.secrets.delete(self._secret_name(plugin_id, key))
                elif value not in ("", True):         # "" / True = unchanged
                    self.secrets.set(self._secret_name(plugin_id, key), str(value).strip())
            else:
                stored[key] = value
        self._state(plugin_id, settings=stored)
        self.bus.publish("plugins_changed")
        return self.settings(self.get(plugin_id))

    def clear_secret(self, plugin_id, key):
        self.secrets.delete(self._secret_name(plugin_id, key))
        self.bus.publish("plugins_changed")

    # ------------------------------------------------------------------ scripts (setup + actions)
    def script_env(self, plugin, extra=None):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
               "DVA_PLUGIN_DIR": plugin["dir"], "DVA_PLUGIN_DATA": str(self.paths.plugin_data(plugin["id"])),
               "DVA_PLUGIN_SETTINGS": json.dumps(self.settings(plugin, reveal=True))}
        env.pop("DVA_TOKEN", None)
        env.update(extra or {})
        return env

    async def run_script(self, plugin_id, script, args=(), timeout=600, on_line=None, extra_env=None):
        """Run a plugin script; lines starting with '::result ' carry a JSON result."""
        plugin = self.get(plugin_id)
        path = Path(plugin["dir"]) / script
        if not path.exists() or Path(plugin["dir"]).resolve() not in path.resolve().parents:
            raise ValueError(f"Plugin script not found: {script}")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", str(path), *args, cwd=str(path.parent), env=self.script_env(plugin, extra_env),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, creationflags=NO_WINDOW)
        result, lines = None, []
        try:
            async def pump():
                nonlocal result
                async for raw in proc.stdout:
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if line.startswith("::result "):
                        try:
                            result = json.loads(line[9:])
                        except json.JSONDecodeError:
                            pass
                        continue
                    lines.append(line)
                    if on_line:
                        on_line(line)
            await asyncio.wait_for(pump(), timeout=timeout)
            await proc.wait()
        except (asyncio.TimeoutError, asyncio.CancelledError):
            proc.kill()
            raise
        return {"code": proc.returncode, "result": result, "output": "\n".join(lines[-200:])}

    async def run_setup(self, plugin_id):
        plugin = self.get(plugin_id)
        if not plugin.get("setup"):
            return {"ok": True, "message": "Nothing to check"}
        out = await self.run_script(plugin_id, plugin["setup"], ["check"], timeout=900)
        result = out["result"] or {"ok": False, "message": (out["output"] or "Setup failed")[-400:]}
        if result.get("settings"):
            state = self.db.get("plugin_state", plugin_id) or {}
            self._state(plugin_id, settings={**(state.get("settings") or {}), **result["settings"]})
        self._state(plugin_id, setup={k: v for k, v in result.items() if k != "settings"} | {"at": now()})
        self.bus.publish("plugins_changed")
        return result

    # ------------------------------------------------------------------ connectors provided by plugins
    def connectors(self):
        out = []
        for plugin in self.enabled():
            values = None
            for c in plugin.get("connectors", []):
                if values is None:
                    values = self.settings(plugin, reveal=True)
                conf = {**c, "id": f"{plugin['id']}:{c['id']}", "source": f"plugin:{plugin['id']}",
                        "plugin": plugin["id"], "enabled": self.db.item_enabled("connector", f"{plugin['id']}:{c['id']}")}
                auth = c.get("auth") or {}
                if auth.get("type") == "bearer":
                    conf["token"] = values.get(auth.get("setting"))
                    conf["auth"] = "bearer"
                    conf["ready"] = bool(conf["token"])
                else:
                    conf["ready"] = True
                out.append(conf)
        return out

    # ------------------------------------------------------------------ install / create / export
    def install_zip(self, data):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            pj = sorted((n for n in names if n.endswith("plugin.json")), key=lambda n: n.count("/"))
            if not pj:
                raise ValueError("The zip has no plugin.json")
            prefix = pj[0][: -len("plugin.json")]
            manifest = json.loads(z.read(pj[0]).decode("utf-8"))
            errors = list(VALIDATOR.iter_errors(manifest))
            if errors:
                raise ValueError(f"plugin.json: {errors[0].message}")
            if any(p["id"] == manifest["id"] and p["source"] == "builtin" for p in self.all()):
                raise ValueError("A built-in plugin already uses that id")
            dest = self.paths.data / "plugins" / manifest["id"]
            tmp = dest.with_name(dest.name + ".installing")
            shutil.rmtree(tmp, ignore_errors=True)
            for n in names:
                if not n.startswith(prefix):
                    continue
                rel = Path(n[len(prefix):])
                if rel.is_absolute() or ".." in rel.parts:
                    raise ValueError("Unsafe path inside the zip")
                (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
                (tmp / rel).write_bytes(z.read(n))
            if dest.exists():
                backup = self.paths.data / "plugins" / f".backup-{manifest['id']}-{now().replace(':', '')}"
                shutil.move(str(dest), str(backup))
            shutil.move(str(tmp), str(dest))
        self.bus.publish("plugins_changed")
        return self.get(manifest["id"])

    def uninstall(self, plugin_id):
        plugin = self.get(plugin_id)
        if not plugin or plugin["source"] != "user":
            raise ValueError("Built-in plugins can be switched off, not removed")
        shutil.rmtree(plugin["dir"])
        self.db.delete("plugin_state", plugin_id)
        self.bus.publish("plugins_changed")

    def create(self, name, description, skills=(), harnesses=(), connectors=(), skills_mgr=None, harness_mgr=None,
               db_connectors=()):
        """Bundle existing items into a new plugin (so a setup can be shared as one zip)."""
        pid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
        if not pid or len(pid) < 2:
            raise ValueError("Give the plugin a name")
        dest = self.paths.data / "plugins" / pid
        if dest.exists():
            raise ValueError("A plugin with that name exists")
        (dest / "skills").mkdir(parents=True)
        (dest / "harnesses").mkdir()
        for sid in skills:
            s = skills_mgr.get(sid, with_body=False)
            if s:
                shutil.copytree(s["dir"], dest / "skills" / Path(s["dir"]).name)
        for hid in harnesses:
            h = harness_mgr.get(hid)
            if h:
                shutil.copytree(h["dir"], dest / "harnesses" / Path(h["dir"]).name)
        manifest = {"id": pid, "name": name, "version": "1.0.0", "description": description, "author": "",
                    "settings": [], "connectors": []}
        for c in db_connectors:
            entry = {"id": re.sub(r"[^a-z0-9]+", "-", c["name"].lower()).strip("-") or c["id"], "name": c["name"],
                     "transport": c["transport"], "description": c.get("description", "")}
            if c["transport"] == "http":
                entry["url"] = c["url"]
            else:
                entry["command"], entry["args"] = c["command"], c.get("args") or []
            if c.get("auth") == "bearer":
                key = f"{entry['id']}_token"
                entry["auth"] = {"type": "bearer", "setting": key}
                manifest["settings"].append({"key": key, "label": f"{c['name']} token", "type": "secret",
                                             "required": True})
            manifest["connectors"].append(entry)
        (dest / "plugin.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        self.bus.publish("plugins_changed")
        return self.get(pid)

    def export_zip(self, plugin_id):
        plugin = self.get(plugin_id)
        out = self.paths.data / "exports" / f"{plugin_id}-{plugin.get('version', '1')}.zip"
        root = Path(plugin["dir"])
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for p in root.rglob("*"):
                if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(".pyc"):
                    z.write(p, Path(plugin_id) / p.relative_to(root))
        return out
