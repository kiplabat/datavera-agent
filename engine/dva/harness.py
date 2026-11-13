"""Harnesses: how the agent works on a request.

type "loop"      the model works in a loop with tools. mode = chat (no tools) | agent (tools, step limit) |
                 planner (writes a short plan, then does each step, then reports).
type "pipeline"  a fixed sequence of steps written in Python (like the Datavera dataset publisher). The model is
                 asked only bounded questions inside it; the engine runs it as a task and streams its progress.

A harness is a folder with harness.json and, optionally, prompt.md (instructions) or pipeline code."""
import io
import json
import re
import shutil
import zipfile
from pathlib import Path

from jsonschema import Draft202012Validator

from .paths import BUILTIN

SCHEMA = {
    "type": "object",
    "required": ["name", "type"],
    "properties": {
        "name": {"type": "string", "minLength": 2, "maxLength": 60},
        "description": {"type": "string", "maxLength": 400},
        "type": {"enum": ["loop", "pipeline"]},
        "mode": {"enum": ["chat", "agent", "planner"]},
        "tools": {"type": "array", "items": {"type": "string"}},
        "connectors": {"type": "boolean"},
        "max_steps": {"type": "integer", "minimum": 1, "maximum": 60},
        "plan_max_steps": {"type": "integer", "minimum": 1, "maximum": 12},
        "temperature": {"type": "number", "minimum": 0, "maximum": 2},
        "thinking": {"type": "boolean"},
        "tool_mode": {"enum": ["native", "json"]},
        "prompt": {"type": "string"},
        "prompt_file": {"type": "string"},
        "memory": {"type": "boolean"},
        "learn": {"type": "boolean"},
        "max_native_tools": {"type": "integer", "minimum": 1, "maximum": 128},
        "entry": {"type": "string"},
        "args": {"type": "array", "items": {"type": "string"}},
        "args_map": {"type": "object", "additionalProperties": {"type": "string"}},
        "inputs": {"type": "object"},
        "input_prompt": {"type": "string"},
        "needs": {"type": "array", "items": {"enum": ["browser", "model"]}},
        "timeout_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
        "icon": {"type": "string"},
    },
    "allOf": [
        {"if": {"properties": {"type": {"const": "pipeline"}}}, "then": {"required": ["entry"]}},
    ],
}
VALIDATOR = Draft202012Validator(SCHEMA)


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:48] or "harness"


def validate(spec):
    errors = sorted(VALIDATOR.iter_errors(spec), key=lambda e: e.path)
    if errors:
        e = errors[0]
        where = "/".join(str(p) for p in e.path) or "harness"
        raise ValueError(f"{where}: {e.message}")


class Harnesses:
    def __init__(self, paths, db, plugins=None):
        self.paths, self.db, self.plugins = paths, db, plugins

    def _sources(self):
        yield "builtin", None, BUILTIN / "harnesses"
        if self.plugins:
            for plugin in self.plugins.enabled():
                yield "plugin", plugin["id"], Path(plugin["dir"]) / "harnesses"
        yield "user", None, self.paths.data / "harnesses"

    def all(self):
        out = {}
        for source, plugin_id, root in self._sources():
            if not root.exists():
                continue
            for hj in sorted(root.glob("*/harness.json")):
                try:
                    spec = json.loads(hj.read_text(encoding="utf-8"))
                    validate(spec)
                    error = None
                except (OSError, json.JSONDecodeError, ValueError) as exc:
                    spec, error = {"name": hj.parent.name, "type": "loop"}, str(exc)[:200]
                hid = f"{plugin_id}:{hj.parent.name}" if plugin_id else hj.parent.name
                out[hid] = {**spec, "id": hid, "source": source, "plugin": plugin_id, "dir": str(hj.parent),
                            "editable": source == "user", "error": error,
                            "enabled": self.db.item_enabled("harness", hid)}
        return list(out.values())

    def get(self, harness_id):
        h = next((h for h in self.all() if h["id"] == harness_id), None)
        if not h:
            return None
        prompt = h.get("prompt") or ""
        pf = h.get("prompt_file") or ("prompt.md" if (Path(h["dir"]) / "prompt.md").exists() else None)
        if pf and (Path(h["dir"]) / pf).exists():
            prompt = (Path(h["dir"]) / pf).read_text(encoding="utf-8")
        h["system_prompt"] = prompt
        return h

    def default_id(self):
        return self.db.setting("default_harness", "agent")

    # ------------------------------------------------------------------ editing
    def save(self, spec, prompt=None, harness_id=None):
        spec = {k: v for k, v in spec.items() if k not in ("id", "source", "plugin", "dir", "editable", "error",
                                                         "enabled", "system_prompt")}
        if spec.get("type") == "pipeline" and not harness_id:
            raise ValueError("Pipeline harnesses contain code: install them as a zip or plugin, not from the form")
        validate(spec)
        folder = self.paths.data / "harnesses" / (harness_id or slugify(spec["name"]))
        if harness_id is None and folder.exists():
            raise ValueError("A harness with that name exists already")
        folder.mkdir(parents=True, exist_ok=True)
        if prompt is not None:
            (folder / "prompt.md").write_text(prompt, encoding="utf-8")
            spec.pop("prompt", None)
            spec["prompt_file"] = "prompt.md"
        (folder / "harness.json").write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return self.get(folder.name)

    def update(self, harness_id, spec, prompt=None):
        h = self.get(harness_id)
        if not h or not h["editable"]:
            raise ValueError("Only your own harnesses can be edited; copy this one first")
        return self.save({**{k: v for k, v in h.items()}, **spec}, prompt, harness_id=Path(h["dir"]).name)

    def copy_to_user(self, harness_id):
        h = self.get(harness_id)
        if not h:
            raise KeyError(harness_id)
        dest = self.paths.data / "harnesses" / slugify(h["name"] + "-copy")
        if dest.exists():
            raise ValueError("A copy already exists")
        shutil.copytree(h["dir"], dest)
        spec = json.loads((dest / "harness.json").read_text(encoding="utf-8"))
        spec["name"] = h["name"] + " (copy)"
        (dest / "harness.json").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
        return self.get(dest.name)

    def delete(self, harness_id):
        h = self.get(harness_id)
        if not h or not h["editable"]:
            raise ValueError("Built-in and plugin harnesses can be switched off, not deleted")
        shutil.rmtree(h["dir"])

    def install_upload(self, filename, data):
        if filename.lower().endswith(".json"):
            spec = json.loads(data.decode("utf-8"))
            if spec.get("type") == "pipeline":
                raise ValueError("A pipeline harness needs its code: upload it as a zip")
            return self.save(spec)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            hj = sorted((n for n in names if n.endswith("harness.json")), key=lambda n: n.count("/"))
            if not hj:
                raise ValueError("The zip has no harness.json")
            prefix = hj[0][: -len("harness.json")]
            spec = json.loads(z.read(hj[0]).decode("utf-8"))
            validate(spec)
            dest = self.paths.data / "harnesses" / slugify(spec["name"])
            if dest.exists():
                raise ValueError("A harness with that name exists already")
            for n in names:
                if not n.startswith(prefix):
                    continue
                rel = Path(n[len(prefix):])
                if rel.is_absolute() or ".." in rel.parts:
                    raise ValueError("Unsafe path inside the zip")
                (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                (dest / rel).write_bytes(z.read(n))
            return self.get(dest.name)
