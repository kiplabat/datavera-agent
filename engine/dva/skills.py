"""Skills: folders with a SKILL.md (frontmatter `name` + `description`, then instructions) and optional files.

Only each skill's one-line description is in the prompt; the agent loads the full instructions when a task needs
them (load_skill). Built-in and plugin skills are read-only; "Copy to my skills" makes an editable copy."""
import io
import re
import shutil
import zipfile
from pathlib import Path

import yaml

from .paths import BUILTIN

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


def parse_skill_md(text):
    meta, body = {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if m:
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        body = m.group(2)
    return meta if isinstance(meta, dict) else {}, body.strip()


def render_skill_md(name, description, body):
    front = yaml.safe_dump({"name": name, "description": description}, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{front}\n---\n\n{body.strip()}\n"


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:64] or "skill"


class Skills:
    def __init__(self, paths, db, plugins=None):
        self.paths, self.db = paths, db
        self.plugins = plugins

    def _sources(self):
        yield "builtin", None, BUILTIN / "skills"
        if self.plugins:
            for plugin in self.plugins.enabled():
                yield "plugin", plugin["id"], Path(plugin["dir"]) / "skills"
        yield "user", None, self.paths.data / "skills"

    def all(self):
        out = {}
        for source, plugin_id, root in self._sources():
            if not root.exists():
                continue
            for md in sorted(root.glob("*/SKILL.md")):
                try:
                    meta, body = parse_skill_md(md.read_text(encoding="utf-8"))
                except OSError:
                    continue
                folder = md.parent.name
                skill_id = f"{plugin_id}:{folder}" if plugin_id else folder
                if source == "user" and skill_id in out:   # a user copy overrides the built-in of the same name
                    skill_id = folder
                out[skill_id] = {
                    "id": skill_id, "name": str(meta.get("name") or folder), "description": str(meta.get("description") or "").strip(),
                    "source": source, "plugin": plugin_id, "dir": str(md.parent), "editable": source == "user",
                    "enabled": self.db.item_enabled("skill", skill_id), "size": len(body),
                }
        return list(out.values())

    def get(self, skill_id, with_body=True):
        skill = next((s for s in self.all() if s["id"] == skill_id or s["name"] == skill_id), None)
        if not skill:
            return None
        if with_body:
            text = (Path(skill["dir"]) / "SKILL.md").read_text(encoding="utf-8")
            skill["raw"] = text
            skill["body"] = parse_skill_md(text)[1]
            skill["files"] = [str(p.relative_to(skill["dir"])) for p in Path(skill["dir"]).rglob("*")
                              if p.is_file() and p.name != "SKILL.md"][:100]
        return skill

    def enabled(self):
        return [s for s in self.all() if s["enabled"]]

    def index(self, limit=40):
        rows = self.enabled()[:limit]
        return "\n".join(f"- {s['name']}: {s['description'][:240]}" for s in rows)

    def roots(self):
        return [s["dir"] for s in self.enabled()]

    # ------------------------------------------------------------------ editing
    def save(self, name, description, body, skill_id=None):
        name = slugify(name)
        if not NAME_RE.match(name):
            raise ValueError("Skill names use lowercase letters, numbers and dashes")
        if not description.strip():
            raise ValueError("Add a one-line description: it is what tells the agent when to use the skill")
        folder = self.paths.data / "skills" / (skill_id or name)
        if skill_id is None and folder.exists():
            raise ValueError(f"A skill named {name!r} already exists; edit it instead")
        folder.mkdir(parents=True, exist_ok=True)
        if (folder / "SKILL.md").exists():
            versions = folder / ".versions"
            versions.mkdir(exist_ok=True)
            n = len(list(versions.glob("SKILL.*.md"))) + 1
            shutil.copy2(folder / "SKILL.md", versions / f"SKILL.{n}.md")
        (folder / "SKILL.md").write_text(render_skill_md(name, description, body), encoding="utf-8")
        return self.get(folder.name)

    def save_raw(self, skill_id, raw):
        skill = self.get(skill_id, with_body=False)
        if not skill or not skill["editable"]:
            raise ValueError("Only your own skills can be edited; copy this one to your skills first")
        meta, body = parse_skill_md(raw)
        return self.save(meta.get("name") or skill["name"], meta.get("description") or skill["description"], body,
                         skill_id=Path(skill["dir"]).name)

    def copy_to_user(self, skill_id):
        skill = self.get(skill_id, with_body=False)
        if not skill:
            raise KeyError(skill_id)
        dest = self.paths.data / "skills" / slugify(skill["name"])
        if dest.exists():
            raise ValueError("You already have a skill with that name")
        shutil.copytree(skill["dir"], dest)
        return self.get(dest.name)

    def delete(self, skill_id):
        skill = self.get(skill_id, with_body=False)
        if not skill or not skill["editable"]:
            raise ValueError("Built-in and plugin skills can be switched off, not deleted")
        shutil.rmtree(skill["dir"])

    def install_upload(self, filename, data):
        """Accept a .zip (with SKILL.md at its root or in one folder) or a single SKILL.md / .md file."""
        if filename.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = [n for n in z.namelist() if not n.endswith("/")]
                md = sorted((n for n in names if n.endswith("SKILL.md")), key=lambda n: n.count("/"))
                if not md:
                    raise ValueError("The zip has no SKILL.md")
                prefix = md[0][: -len("SKILL.md")]
                meta, _ = parse_skill_md(z.read(md[0]).decode("utf-8"))
                name = slugify(meta.get("name") or Path(prefix.rstrip("/")).name or Path(filename).stem)
                dest = self.paths.data / "skills" / name
                if dest.exists():
                    raise ValueError(f"You already have a skill named {name!r}")
                for n in names:
                    if not n.startswith(prefix):
                        continue
                    rel = Path(n[len(prefix):])
                    if rel.is_absolute() or ".." in rel.parts:
                        raise ValueError("Unsafe path inside the zip")
                    target = dest / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(z.read(n))
                return self.get(name)
        text = data.decode("utf-8")
        meta, body = parse_skill_md(text)
        name = meta.get("name") or Path(filename).stem
        return self.save(name, meta.get("description") or "", body)
