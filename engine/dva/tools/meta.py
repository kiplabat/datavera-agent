"""Tools about the agent itself: memory, skills, scheduled tasks and harnesses."""
from pathlib import Path

from . import Tool, ToolError


async def remember(ctx, text, scope="project"):
    if scope == "project" and not ctx.project:
        scope = "global"
    scope_id = ctx.project["id"] if scope == "project" else (ctx.chat["id"] if scope == "chat" else "")
    try:
        row, created = ctx.app.memory.add(scope, text, scope_id, "fact", "agent", ctx.chat["id"])
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    ctx.emit("memory_saved", item={"id": row["id"], "text": row["text"], "scope": scope})
    return ("Saved to memory" if created else "Updated an existing memory") + f" ({scope}): {row['text']}"


async def search_memory(ctx, query):
    words = [w for w in query.lower().split() if len(w) > 2]
    rows = []
    for row in ctx.app.memory.list():
        if row["scope"] == "project" and (not ctx.project or row["scope_id"] != ctx.project["id"]):
            continue
        if row["scope"] == "chat" and row["scope_id"] != ctx.chat["id"]:
            continue
        score = sum(w in row["text"].lower() for w in words)
        if score:
            rows.append((score, row))
    rows.sort(key=lambda r: -r[0])
    return "\n".join(f"- ({r['scope']}) {r['text']}" for _, r in rows[:15]) or "Nothing in memory matches."


async def load_skill(ctx, name):
    skill = ctx.app.skills.get(name)
    if not skill or not skill["enabled"]:
        names = ", ".join(s["name"] for s in ctx.app.skills.enabled())
        raise ToolError(f"No enabled skill named {name!r}. Available: {names}")
    ctx.loaded_skills.add(skill["id"])
    ctx.emit("skill_loaded", skill=skill["name"])
    files = ("\n\nFiles in this skill (read with read_skill_file): " + ", ".join(skill["files"])) if skill["files"] else ""
    return f"# Skill: {skill['name']}\n{skill['body']}{files}"


async def read_skill_file(ctx, name, path):
    skill = ctx.app.skills.get(name)
    if not skill:
        raise ToolError(f"No skill named {name!r}")
    p = (Path(skill["dir"]) / path).resolve()
    if Path(skill["dir"]).resolve() not in p.parents or not p.is_file():
        raise ToolError("No such file in that skill")
    return p.read_text(encoding="utf-8", errors="replace")[:30000]


async def save_skill(ctx, name, description, instructions):
    existing = ctx.app.skills.get(name, with_body=False)
    try:
        if existing and existing["editable"]:
            skill = ctx.app.skills.save(name, description, instructions, skill_id=Path(existing["dir"]).name)
        else:
            skill = ctx.app.skills.save(name, description, instructions)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    ctx.app.bus.publish("skills_changed")
    return f"Skill '{skill['name']}' saved. It is now available to every chat (Skills page to edit or switch off)."


async def create_schedule(ctx, name, prompt, when, harness="", model=""):
    from ..scheduler import parse_when
    try:
        cron = parse_when(when)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    row = ctx.app.scheduler.create({
        "name": name, "prompt": prompt, "cron": cron, "harness": harness or ctx.chat.get("harness") or "",
        "model": model or ctx.model_id or "", "project_id": ctx.project["id"] if ctx.project else None,
        "connectors": ctx.chat.get("connectors") or []})
    return f"Scheduled task '{name}' created ({cron}); next run {row['next_run']}. Manage it on the Schedules page."


async def list_schedules(ctx):
    rows = ctx.app.scheduler.list()
    return "\n".join(f"- {r['name']}: {r['cron']} ({'on' if r['enabled'] else 'off'}), next {r['next_run']}"
                     for r in rows) or "No scheduled tasks yet."


async def save_harness(ctx, name, description, mode, instructions, tools=None, max_steps=8):
    spec = {"name": name, "description": description, "type": "loop", "mode": mode,
            "tools": tools or ["files", "web", "memory", "skills"], "max_steps": int(max_steps or 8),
            "connectors": True}
    try:
        h = ctx.app.harnesses.save(spec, prompt=instructions)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    ctx.app.bus.publish("harnesses_changed")
    return f"Harness '{h['name']}' saved (id {h['id']}). Pick it in the chat's harness menu."


S = lambda props, req: {"type": "object", "properties": props, "required": req}  # noqa: E731
TOOLS = [
    Tool("remember", "Save one durable fact or preference to memory so future chats know it.",
         S({"text": {"type": "string"}, "scope": {"enum": ["global", "project", "chat"]}}, ["text"]),
         remember, "memory", label="Remember", summary=lambda a: f"Remember: {a.get('text')}"),
    Tool("search_memory", "Search what has been remembered.", S({"query": {"type": "string"}}, ["query"]),
         search_memory, "memory", label="Search memory", summary=lambda a: f"Search memory: {a.get('query')}"),
    Tool("load_skill", "Load a skill's full instructions before doing a task it covers.",
         S({"name": {"type": "string"}}, ["name"]), load_skill, "skills", label="Load skill",
         summary=lambda a: f"Load skill {a.get('name')}"),
    Tool("read_skill_file", "Read a file that belongs to a loaded skill.",
         S({"name": {"type": "string"}, "path": {"type": "string"}}, ["name", "path"]), read_skill_file, "skills",
         label="Read skill file", summary=lambda a: f"Read {a.get('path')} from skill {a.get('name')}"),
    Tool("save_skill", "Create or update a skill (reusable instructions) for the user.",
         S({"name": {"type": "string", "description": "lowercase-with-dashes"},
            "description": {"type": "string", "description": "When to use it, one sentence"},
            "instructions": {"type": "string", "description": "Markdown instructions"}},
           ["name", "description", "instructions"]),
         save_skill, "skills", changes=True, label="Save skill", summary=lambda a: f"Save skill '{a.get('name')}'"),
    Tool("create_schedule", "Create a scheduled task that runs a prompt automatically. `when` examples: "
         "'every day at 08:30', 'weekdays at 9:00', 'every 3 hours', 'every monday at 10:00', or a cron expression.",
         S({"name": {"type": "string"}, "prompt": {"type": "string"}, "when": {"type": "string"},
            "harness": {"type": "string"}, "model": {"type": "string"}}, ["name", "prompt", "when"]),
         create_schedule, "automation", changes=True, label="Create scheduled task",
         summary=lambda a: f"Schedule '{a.get('name')}' {a.get('when')}: {a.get('prompt', '')[:160]}"),
    Tool("list_schedules", "List scheduled tasks.", S({}, []), list_schedules, "automation",
         label="List scheduled tasks", summary=lambda a: "List scheduled tasks"),
    Tool("save_harness", "Create a new harness (a reusable way of working: instructions, tools and step limit).",
         S({"name": {"type": "string"}, "description": {"type": "string"},
            "mode": {"enum": ["chat", "agent", "planner"]}, "instructions": {"type": "string"},
            "tools": {"type": "array", "items": {"enum": ["files", "web", "python", "memory", "skills", "automation"]}},
            "max_steps": {"type": "integer"}}, ["name", "description", "mode", "instructions"]),
         save_harness, "automation", changes=True, label="Save harness",
         summary=lambda a: f"Save harness '{a.get('name')}' ({a.get('mode')})"),
]
