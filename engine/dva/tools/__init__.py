"""Tools the agent can use. Built-in groups: files, web, python, memory, skills, automation. Connector (MCP)
tools are added per chat. Every tool says whether it changes anything; changes ask the user first unless the
chat or scheduled task was set to approve automatically."""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    func: Callable[..., Awaitable[Any]]
    group: str
    changes: bool = False           # True = needs approval (unless auto-approve)
    label: str = ""                 # short human verb for the UI, e.g. "Read file"
    summary: Callable[[dict], str] | None = None
    source: str = "builtin"         # builtin | connector:<id>

    def schema(self):
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}

    def describe_call(self, args):
        if self.summary:
            try:
                return self.summary(args)
            except Exception:
                pass
        return f"{self.label or self.name}: {json.dumps(args, ensure_ascii=False)[:200]}"


class ToolDenied(Exception):
    pass


class ToolError(Exception):
    pass


@dataclass
class ToolContext:
    app: Any
    chat: dict
    project: dict | None
    workspace: Path
    folders: list            # granted folders (read/write)
    readonly: list           # e.g. skill folders
    emit: Callable           # emit(kind, **data) to the chat's live stream
    approve: Callable        # async approve(tool, args, description) -> bool
    job_id: str | None = None
    model_id: str | None = None
    harness: dict | None = None
    loaded_skills: set = field(default_factory=set)
    notes: dict = field(default_factory=dict)

    @property
    def roots(self):
        return [Path(f).resolve() for f in self.folders] + [self.workspace.resolve()]

    @property
    def primary(self):
        return Path(self.folders[0]).resolve() if self.folders else self.workspace.resolve()

    def resolve(self, path, write=False):
        """Resolve a path inside the granted folders (or read-only skill folders for reading)."""
        p = Path(str(path or ".").strip().strip('"'))
        if not p.is_absolute():
            p = self.primary / p
        p = p.resolve()
        allowed = self.roots + ([] if write else [Path(r).resolve() for r in self.readonly])
        for root in allowed:
            if p == root or root in p.parents:
                return p
        names = ", ".join(str(r) for r in self.roots)
        raise ToolDenied(f"{p} is outside the folders this chat may use ({names}). "
                         "Add the folder to the chat or project to allow it.")


def builtin_tools():
    from . import files, meta, python_exec, web
    return files.TOOLS + web.TOOLS + python_exec.TOOLS + meta.TOOLS


GROUPS = {
    "files": "Read, search and write files in the chat's folders",
    "web": "Search the web and read pages",
    "python": "Run Python code (data analysis, conversions)",
    "memory": "Remember and recall facts",
    "skills": "Load skills and create new ones",
    "automation": "Create scheduled tasks and harnesses",
}
