"""Runs one chat turn with the chosen harness and model, streaming everything to the UI.

Loop harnesses (mode):
  chat      one reply, no tools
  agent     tool loop with a step limit
  planner   short plan → each step with tools → final answer
  research  fixed stages run by code (plan searches → search → rank → read → download files → extract facts →
            cited report). The model only answers small questions, so it cannot loop.
  team      a lead splits the task between agents that run in parallel (each its own chat, model and limits),
            then combines their results.
Pipeline harnesses: a Python program run as a task; its output streams into the chat.

Every tool-using turn has a loop guard: repeated or near-identical calls are refused (the model is told why), tools
have per-turn caps (searches especially), several unproductive results in a row trigger a "change approach" note and
then a forced write-up, and a time limit stops the turn with a summary of what was found.

The user can send follow-ups while a reply is being written: queued ones run right after it; "steer" ones are handed
to the agent at its next step.

Each turn is saved as one assistant message whose meta.steps lists text, tool calls and results in order, so a
reloaded chat looks exactly like the live one."""
import asyncio
import hashlib
import json
import os
import platform
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import media
from .db import new_id, now
from .llm import LLMError, estimate_tokens
from .mcp import tool_key
from .tools import GROUPS, Tool, ToolContext, ToolDenied, ToolError, builtin_tools

TOOL_RESULT_LIMIT = 6000
PLAN_SCHEMA_STEP = {"type": "object", "required": ["title"], "properties": {
    "title": {"type": "string", "maxLength": 90}, "detail": {"type": "string", "maxLength": 300}}}
TEXT_TOOLCALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
URL_RE = re.compile(r"https?://[^\s)>\]]+")

BASE_PROMPT = """You are Datavera Agent, an assistant that runs entirely on the user's own computer using a local \
model ({model}). Nothing the user shares leaves this computer unless a tool sends it (web pages, connectors).
Today is {today}. The computer runs {os}.
Be direct and practical. Use Markdown. Never invent file contents, numbers or sources: read, fetch or compute them."""

AGENT_RULES = """You can use tools. Use one when it helps; otherwise just answer. After tool results, continue until \
the task is done, then give a clear final answer (what you did, what you found, where files are).
Never repeat a tool call you already made; if something did not work twice, try a different approach or answer with \
what you have. If a search says NO RELEVANT RESULTS, do not search for the same thing again.
Changes (writing files, running code, scheduling, connector actions that change things) are shown to the user for \
approval by the app; if one is declined, do not retry it — explain or choose another way."""


class TurnStopped(Exception):
    pass


def _norm_args(args):
    return json.dumps(args or {}, sort_keys=True, ensure_ascii=False).lower()


def _terms(text):
    from .tools.web import terms_of
    return set(terms_of(text))


class LoopGuard:
    """Stops agents from going round in circles. Shared by every model call of a turn."""
    UNPRODUCTIVE = ("NO RELEVANT RESULTS", "Error:", "There is no tool called", "The arguments were not valid JSON",
                    "Blocked:")

    def __init__(self, turn):
        lim, h, db = turn.limits, turn.harness, turn.db
        self.max_calls = int(lim.get("max_tool_calls") or h.get("max_tool_calls") or 40)
        self.caps = {"web_search": int(lim.get("max_searches") or h.get("max_searches") or 6),
                     "web_fetch": 14, "download_file": 15, "look_at_video": 6, "delegate": 2}
        self.caps.update({k: int(v) for k, v in (h.get("tool_limits") or {}).items()})
        minutes = float(lim.get("time_limit_minutes") or h.get("time_limit_minutes") or
                        db.setting("turn_time_limit_minutes", 30) or 30)
        self.minutes = minutes
        self.deadline = time.monotonic() + minutes * 60
        self.counts = Counter()
        self.total = 0
        self.done = {}            # call signature -> short result
        self.results = set()      # hashes of results already seen
        self.searches = []        # term sets of earlier searches
        self.streak = 0           # unproductive results in a row
        self.blocked = 0
        self.nudged_at = 0
        self.stop_reason = None

    def check(self, name, args):
        """None if the call may run, else the message returned to the model instead of running it."""
        sig = name + _norm_args(args)
        if sig in self.done:
            return (f"Blocked: you already called {name} with exactly these arguments. Result was: "
                    f"{self.done[sig][:400]}\nUse that result, or do something different.")
        if self.total >= self.max_calls:
            return f"Blocked: this reply has used its {self.max_calls} tool calls. Write your answer now."
        cap = self.caps.get(name)
        if cap is not None and self.counts[name] >= cap:
            return (f"Blocked: {name} was already used {cap} times in this reply (the limit). Work with what you "
                    "have, try another kind of source, or answer now.")
        if name == "web_search":
            terms = _terms((args or {}).get("query", ""))
            for prev in self.searches:
                if terms and prev and len(terms & prev) / len(terms | prev) >= 0.6:
                    return ("Blocked: this search is nearly the same as one you already ran. Searching again with "
                            "similar words gives the same results. Use different words, open a specific source with "
                            "web_fetch, or answer with what you have.")
        return None

    def record(self, name, args, ok, result, blocked=False):
        sig = name + _norm_args(args)
        if blocked:
            self.blocked += 1
            self.streak += 1
            return
        self.total += 1
        self.counts[name] += 1
        self.done[sig] = result
        if name == "web_search":
            self.searches.append(_terms((args or {}).get("query", "")))
        digest = hashlib.sha1(result[:4000].encode("utf-8", "ignore")).hexdigest()
        useless = (not ok) or result.startswith(self.UNPRODUCTIVE) or digest in self.results
        self.results.add(digest)
        self.streak = self.streak + 1 if useless else 0

    def nudge(self):
        if self.streak >= 3 and self.streak != self.nudged_at:
            self.nudged_at = self.streak
            return ("Note from the app: your last few tool calls did not produce anything new. Do not repeat them. "
                    "Either try a clearly different approach (another source, another tool) or stop using tools "
                    "and answer with what you have, saying what you could not find.")
        return None

    def should_stop(self):
        if self.stop_reason:
            return self.stop_reason
        if time.monotonic() > self.deadline:
            self.stop_reason = f"the time limit of {self.minutes:g} minutes was reached"
        elif self.streak >= 6:
            self.stop_reason = "the last six tool calls produced nothing new"
        elif self.blocked >= 5:
            self.stop_reason = "the agent kept repeating calls that were already made"
        return self.stop_reason


RESEARCH_PLAN = {"type": "object", "required": ["action", "queries"], "properties": {
    "action": {"enum": ["research", "answer"]},
    "queries": {"type": "array", "maxItems": 5, "items": {"type": "string", "maxLength": 120}},
    "want_files": {"type": "boolean"},
    "known_sources": {"type": "array", "maxItems": 4, "items": {"type": "string", "maxLength": 200}}}}
EXTRACT_SCHEMA = {"type": "object", "required": ["relevant", "facts"], "properties": {
    "relevant": {"type": "boolean"},
    "facts": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 400}},
    "files": {"type": "array", "maxItems": 4, "items": {"type": "integer"}}}}


class Turn:
    def __init__(self, app, chat_id, job):
        self.app, self.db = app, app.db
        self.job = job
        self.chat = self.db.get("chats", chat_id)
        self.project = self.db.get("projects", self.chat["project_id"]) if self.chat.get("project_id") else None
        hid = self.chat.get("harness") or (self.project or {}).get("default_harness") or app.harnesses.default_id()
        self.harness = app.harnesses.get(hid) or app.harnesses.get("agent")
        if (job.get("payload") or {}).get("team") and self.harness.get("mode") != "team":
            # the user put a team together in the composer: the team lead runs this turn
            self.harness = app.harnesses.get("team-lead") or {**self.harness, "mode": "team", "name": "Team"}
        self.model_id = self.chat.get("model") or (self.project or {}).get("default_model") or app.models.default_model()
        self.limits = dict(self.chat.get("settings") or {})
        self.message_id = new_id("msg_")
        self.steps = []
        self.reasoning = []
        self.text = []
        self.stats = {"model_calls": 0, "prompt_tokens": 0, "output_tokens": 0, "seconds": 0}
        self.tool_map = {}
        self.workspace = app.paths.workspace(chat_id)
        self.auto_approve = bool(job.get("payload", {}).get("auto_approve")) or \
            self.db.setting(f"chat_auto_approve:{chat_id}", False)
        self.allowed_always = set(self.db.setting(f"chat_allow:{chat_id}", []) or [])
        self.ctx = None
        self.server = None
        self._lease = None
        self.guard = LoopGuard(self)

    # ------------------------------------------------------------------ settings
    @property
    def max_reply(self):
        return int(self.limits.get("max_reply_tokens") or self.harness.get("max_reply_tokens") or
                   self.db.setting("max_reply_tokens", 2048))

    @property
    def temperature(self):
        t = self.limits.get("temperature")
        return float(t if t not in (None, "") else self.harness.get("temperature", 0.4))

    @property
    def vision(self):
        return bool(self.server and self.server.plan.get("mmproj"))

    # ------------------------------------------------------------------ model lease (released while waiting on others)
    async def hold_model(self):
        if self._lease is None:
            self._lease = self.app.runtime.lease(self.model_id, on_wait=self.status)
            self.server = await self._lease.__aenter__()

    async def drop_model(self):
        lease, self._lease = self._lease, None
        self.server = None
        if lease is not None:
            await lease.__aexit__(None, None, None)

    # ------------------------------------------------------------------ events
    def emit(self, type_, **data):
        self.app.bus.publish("chat", chat_id=self.chat["id"], job_id=self.job["id"], type=type_,
                             message_id=self.message_id, **data)

    def status(self, text):
        self.emit("status", text=text)
        self.app.jobs.progress(self.job["id"], text)

    def note(self, text):
        self.steps.append({"type": "note", "text": text})
        self.emit("note", text=text)

    # ------------------------------------------------------------------ approvals
    async def approve(self, tool, args, description):
        if not tool.changes:
            return True
        if self.auto_approve or tool.name in self.allowed_always:
            return True
        approval_id = new_id("ap_")
        fut = asyncio.get_running_loop().create_future()
        self.app.approvals[approval_id] = {"future": fut, "chat_id": self.chat["id"], "job_id": self.job["id"],
                                           "tool": tool.name, "description": description,
                                           "parent_chat_id": self.chat.get("parent_chat_id"),
                                           "agent": self.chat.get("agent_name")}
        self.emit("approval", approval_id=approval_id, tool=tool.name, label=tool.label or tool.name,
                  description=description, arguments=_preview(args))
        if self.chat.get("parent_chat_id"):  # agents in a team ask in the lead's chat too
            self.app.bus.publish("chat", chat_id=self.chat["parent_chat_id"], type="agent_approval",
                                 approval_id=approval_id, agent=self.chat.get("agent_name") or "Agent",
                                 agent_chat_id=self.chat["id"], tool=tool.name, label=tool.label or tool.name,
                                 description=description, arguments=_preview(args))
        self.app.jobs.set_status(self.job["id"], "waiting")
        self.app.bus.publish("notify", title="Approval needed",
                             body=f"{self.chat.get('title') or 'A chat'}: {tool.label or tool.name}",
                             chat_id=self.chat.get("parent_chat_id") or self.chat["id"])
        timeout = float(self.db.setting("approval_timeout_minutes", 360)) * 60
        try:
            decision = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            decision = {"allow": False, "reason": "timed out"}
        finally:
            self.app.approvals.pop(approval_id, None)
            self.app.jobs.set_status(self.job["id"], "running")
        self.emit("approval_done", approval_id=approval_id, allow=bool(decision.get("allow")))
        if self.chat.get("parent_chat_id"):
            self.app.bus.publish("chat", chat_id=self.chat["parent_chat_id"], type="agent_approval_done",
                                 approval_id=approval_id)
        if decision.get("allow") and decision.get("always"):
            self.allowed_always.add(tool.name)
            self.db.set_setting(f"chat_allow:{self.chat['id']}", sorted(self.allowed_always))
        return bool(decision.get("allow"))

    # ------------------------------------------------------------------ prompt building
    def folders(self):
        folders = list((self.project or {}).get("folders") or []) + list(self.chat.get("folders") or [])
        return [f for f in dict.fromkeys(folders) if Path(f).exists()]

    def tool_groups(self):
        groups = self.harness.get("tools")
        if groups is None:
            groups = list(GROUPS) if self.harness.get("mode", "agent") != "chat" else []
        if "*" in groups:
            groups = list(GROUPS)
        groups = set(groups)
        if self.limits.get("tools"):
            groups &= set(self.limits["tools"])
        if not self.db.setting("allow_python", True):
            groups.discard("python")
        if not self.vision:
            groups.discard("vision")
        if self.chat.get("parent_chat_id"):   # agents in a team do not start their own teams
            groups.discard("agents")
        return groups

    async def build_tools(self):
        if self.harness.get("mode") == "chat":
            return []
        groups = self.tool_groups()
        tools = [t for t in builtin_tools() if t.group in groups]
        if "agents" in groups:
            tools.append(self._delegate_tool())
        connector_ids = list(self.chat.get("connectors") or []) or list((self.project or {}).get("connectors") or [])
        connector_tools = []
        if self.harness.get("connectors", True) and connector_ids:
            for cid in connector_ids:
                conf = self.app.connectors.get(cid, reveal=False)
                if not conf or not conf.get("enabled", True):
                    continue
                try:
                    listed = await self.app.connectors.list_tools(cid)
                except Exception as exc:
                    self.status(f"Connector {conf['name']} is unavailable: {str(exc)[:120]}")
                    continue
                mode = self.app.connectors.approval_mode(conf)
                for t in listed:
                    connector_tools.append(self._connector_tool(cid, conf, t, mode))
        limit = int(self.harness.get("max_native_tools") or self.db.setting("max_native_tools", 28))
        if len(tools) + len(connector_tools) > limit and connector_tools:
            tools += self._connector_meta_tools(connector_ids, connector_tools)
            self.connector_index = connector_tools
        else:
            tools += connector_tools
        self.tool_map = {t.name: t for t in tools}
        return tools

    def _connector_tool(self, cid, conf, t, mode):
        async def call(ctx, **args):
            ok, text = await self.app.connectors.call(cid, t["name"], args)
            if not ok:
                raise ToolError(text)
            return text
        changes = mode == "always" or (mode == "changes" and not t["readonly"])
        return Tool(tool_key(cid, t["name"]), f"[{conf['name']}] {t['description']}", t["parameters"], call,
                    "connector", changes=changes, label=f"{conf['name']}: {t['name']}", source=f"connector:{cid}",
                    summary=lambda a, n=t["name"], c=conf["name"]: f"{c} → {n} {json.dumps(a, ensure_ascii=False)[:300]}")

    def _connector_meta_tools(self, connector_ids, connector_tools):
        """Too many connector tools for a small model's prompt: offer a lookup + call pair instead."""
        by_name = {t.name: t for t in connector_tools}

        async def find(ctx, query=""):
            words = query.lower().split()
            rows = [t for t in connector_tools if not words or any(w in (t.name + t.description).lower() for w in words)]
            return "\n\n".join(f"{t.name}: {t.description[:300]}\nparameters: {json.dumps(t.parameters)[:600]}"
                               for t in rows[:12]) or "No matching tools"

        async def use(ctx, tool, arguments=None):
            t = by_name.get(tool)
            if not t:
                raise ToolError(f"Unknown connector tool {tool!r}; use find_connector_tools first")
            if not await self.approve(t, arguments or {}, t.describe_call(arguments or {})):
                raise ToolDenied("The user declined this action")
            return await t.func(ctx, **(arguments or {}))
        return [Tool("find_connector_tools", "Find connector tools by keyword (shows their parameters).",
                     {"type": "object", "properties": {"query": {"type": "string"}}}, find, "connector",
                     label="Find connector tools"),
                Tool("use_connector_tool", "Call a connector tool found with find_connector_tools.",
                     {"type": "object", "properties": {"tool": {"type": "string"}, "arguments": {"type": "object"}},
                      "required": ["tool"]}, use, "connector", label="Use connector tool",
                     summary=lambda a: f"{a.get('tool')} {json.dumps(a.get('arguments') or {})[:300]}")]

    def _delegate_tool(self):
        harness_ids = [h["id"] for h in self.app.harnesses.all() if h.get("type") == "loop"
                       and h.get("mode") not in ("team",) and h.get("enabled", True)]

        async def delegate(ctx, tasks):
            if not isinstance(tasks, list) or not tasks:
                raise ToolError("Give a list of tasks")
            agents = [{"name": str(t.get("name") or f"Agent {i}")[:40], "task": str(t.get("task") or "")[:1500],
                       "harness": t.get("harness") if t.get("harness") in harness_ids else "agent"}
                      for i, t in enumerate(tasks[:int(self.db.setting("max_parallel_agents", 4))], 1)]
            await self.drop_model()        # free this model's memory slot while the agents work
            try:
                results = await self.run_agents(agents, overall=self.last_user_text())
            finally:
                await self.hold_model()
            return "\n\n".join(f"## {r['name']} ({r['status']})\n{r['content'][:2500]}" for r in results)
        return Tool("delegate", "Hand independent parts of the task to other agents that work at the same time, "
                    "each in its own chat. Use only for parts that do not depend on each other. Returns their results.",
                    {"type": "object", "required": ["tasks"], "properties": {"tasks": {"type": "array", "maxItems": 6,
                     "items": {"type": "object", "required": ["name", "task"], "properties": {
                         "name": {"type": "string"}, "task": {"type": "string"},
                         "harness": {"enum": harness_ids}}}}}},
                    delegate, "agents", label="Start agents",
                    summary=lambda a: "Start agents: " + ", ".join(t.get("name", "?") for t in a.get("tasks") or []))

    def system_prompt(self, tools):
        model = self.db.get("models", self.model_id) if self.model_id else None
        parts = [BASE_PROMPT.format(model=(model or {}).get("name") or self.model_id or "local model",
                                    today=datetime.now().strftime("%A %d %B %Y"),  # date only: a changing prefix defeats prompt caching
                                    os=f"{platform.system()} {platform.release()}")]
        if self.chat.get("parent_chat_id"):
            parts.append(f"You are “{self.chat.get('agent_name') or 'an agent'}”, one member of a team working in "
                         "parallel. Do only your part, then reply with a clear, self-contained result (facts, numbers, "
                         "file paths) that the team lead can combine with the others' results.")
        if tools:
            parts.append(AGENT_RULES)
            folders = self.folders()
            parts.append("Folders you may use: " + ", ".join(folders + [str(self.workspace)]) +
                         f"\nRelative paths start in {folders[0] if folders else self.workspace}. "
                         f"Files you create without a folder go to {self.workspace}.")
        if self.harness.get("system_prompt"):
            parts.append(self.harness["system_prompt"].strip())
        if self.project and (self.project.get("instructions") or "").strip():
            parts.append(f"Project “{self.project['name']}” instructions:\n{self.project['instructions'].strip()}")
        if self.harness.get("memory", True):
            mem = self.app.memory.context(self.chat, self.project, self.harness["id"])
            if mem:
                parts.append(mem + "\nUse these when relevant; the user can edit them on the Memory page.")
        if "load_skill" in self.tool_map:
            index = self.app.skills.index()
            if index:
                parts.append("Skills (reusable instructions). If one fits the task, call load_skill first:\n" + index)
        if getattr(self, "connector_index", None):
            names = sorted({t.label.split(':')[0] for t in self.connector_index})
            parts.append("Connected services: " + ", ".join(names) +
                         ". Use find_connector_tools then use_connector_tool to work with them.")
        if self.chat.get("summary"):
            parts.append("Summary of the earlier conversation:\n" + self.chat["summary"])
        return "\n\n".join(parts)

    def history(self):
        rows = self.db.all("SELECT * FROM messages WHERE chat_id=? ORDER BY created, rowid", (self.chat["id"],))
        if self.chat.get("summary_upto"):
            ids = [r["id"] for r in rows]
            if self.chat["summary_upto"] in ids:
                rows = rows[ids.index(self.chat["summary_upto"]) + 1:]
        out = []
        for r in rows:
            meta = r.get("meta") or {}
            if meta.get("pending") or meta.get("not_sent"):
                continue
            if r["role"] == "user":
                item = {"role": "user", "content": self._user_content(r), "_id": r["id"],
                        "_media": [a["path"] for a in meta.get("attachments", []) if media.kind_of(a.get("path", ""))]}
                if out and out[-1]["role"] == "user":     # follow-ups sent while the agent worked: one message
                    out[-1]["content"] += "\n\n" + item["content"]
                    out[-1]["_media"] += item["_media"]
                    out[-1]["_id"] = r["id"]
                else:
                    out.append(item)
            elif r["role"] == "assistant":
                used = [s for s in meta.get("steps", []) if s.get("type") == "tool"]
                note = ""
                if used:
                    note = "\n\n(Tools used: " + "; ".join(
                        f"{s.get('label') or s['name']}{'' if s.get('ok', True) else ' (failed)'}" for s in used[:8]) + ")"
                content = (r["content"] or "").strip() or "(no answer)"
                out.append({"role": "assistant", "content": content + note, "_id": r["id"]})
        return out

    def _user_content(self, row):
        meta = row.get("meta") or {}
        text = row["content"] or ""
        for att in meta.get("attachments", []):
            p = Path(att["path"])
            kind = media.kind_of(p)
            text += f"\n\n[Attached {kind or 'file'}: {att['name']} ({att.get('size', 0):,} bytes) at {p}]"
            if att.get("inline") and p.exists() and p.stat().st_size <= 12000:
                try:
                    text += f"\n```\n{p.read_text(encoding='utf-8', errors='replace')[:12000]}\n```"
                except OSError:
                    pass
        return text

    def last_user_text(self):
        row = self.db.one("SELECT content FROM messages WHERE chat_id=? AND role='user' ORDER BY created DESC, "
                          "rowid DESC LIMIT 1", (self.chat["id"],))
        return (row or {}).get("content") or ""

    async def attach_media(self, messages, history):
        """Put images / video frames of the latest message in front of a vision model."""
        last = history[-1] if history else None
        if not last or last["role"] != "user" or not last.get("_media"):
            return messages
        files = [p for p in last["_media"] if Path(p).exists()]
        if not files:
            return messages
        if not self.vision:
            self.note("This model cannot see images or videos, so it only knows the file names. Pick a model with "
                      "the eye icon (vision) to have them analysed.")
            return messages
        ctx = self.server.plan["ctx"]
        frames = max(3, min(8, (ctx - 3000) // 700))
        parts = [{"type": "text", "text": messages[-1]["content"]}]
        self.status("Looking at the attached media…")
        for p in files[:6]:
            try:
                parts += await asyncio.to_thread(media.parts_for, p, frames)
            except Exception as exc:
                parts.append({"type": "text", "text": f"({Path(p).name} could not be opened: {type(exc).__name__})"})
        messages[-1] = {"role": "user", "content": parts}
        return messages

    async def fit_history(self, system, tools, messages):
        ctx_tokens = self.server.plan["ctx"] if self.server else 8192
        reserve = self.max_reply
        fixed = estimate_tokens(system) + estimate_tokens(json.dumps([t.schema() for t in tools]))
        media_tokens = 900 * len((messages[-1] if messages else {}).get("_media") or []) if self.vision else 0
        budget = ctx_tokens - reserve - fixed - media_tokens - 200
        total = sum(estimate_tokens(m["content"]) for m in messages)
        if total <= budget * 0.85 or len(messages) <= 3:
            if total > budget:  # one huge message: trim it
                last = messages[-1]
                last["content"] = last["content"][: max(2000, int(budget * 3.2))] + "\n…(trimmed to fit the model's memory)"
            return messages
        keep = messages[-3:]
        older = messages[:-3]
        self.status("Summarising the earlier conversation to fit the model's context…")
        try:
            summary = await self.app.memory.summarise(self.chat, older)
            self.chat["summary"] = summary
            self.db.update("chats", self.chat["id"], {"summary": summary, "summary_upto": older[-1]["_id"]})
            self.stats["model_calls"] += 1
        except Exception:
            pass
        return keep

    # ------------------------------------------------------------------ follow-ups ("steer")
    def take_steer(self, messages):
        rows = self.app.pending_messages(self.chat["id"], "steer")
        for r in rows:
            self.app.mark_message(r, pending=None, steered=True)
            messages.append({"role": "user", "content": "(Message from me while you were working — take it into "
                                                        f"account from now on:)\n{r['content']}"})
            self.steps.append({"type": "steer", "text": r["content"], "message_id": r["id"]})
            self.emit("steer", text=r["content"], steer_id=r["id"])
        return bool(rows)

    # ------------------------------------------------------------------ running
    async def run(self):
        started = time.monotonic()
        self.emit("assistant_start", harness=self.harness["name"], model=self.model_id,
                  agent=self.chat.get("agent_name"))
        try:
            if self.harness.get("type") == "pipeline":
                await self.run_pipeline()
            else:
                if not self.model_id:
                    raise TurnStopped("No model is installed yet. Open Models and download one (the recommended "
                                      "one is marked).")
                if self.harness.get("mode") == "team":
                    await self.run_team()
                else:
                    await self.hold_model()
                    await self.run_loop()
        except asyncio.CancelledError:
            self.steps.append({"type": "note", "text": "Stopped by the user."})
            await self._safe_drop()
            self.finish(time.monotonic() - started, stopped=True)
            raise
        except TurnStopped as exc:
            await self._safe_drop()
            self.steps.append({"type": "error", "text": str(exc)})
            self.emit("error", text=str(exc))
            self.finish(time.monotonic() - started, error=str(exc))
            return
        except Exception as exc:
            await self._safe_drop()
            msg = f"{type(exc).__name__}: {str(exc)[:500]}"
            self.steps.append({"type": "error", "text": msg})
            self.emit("error", text=msg)
            self.finish(time.monotonic() - started, error=msg)
            raise
        await self._safe_drop()
        self.finish(time.monotonic() - started)

    async def _safe_drop(self):
        try:
            await self.drop_model()
        except Exception as exc:
            self.app.log(f"releasing the model after a turn: {type(exc).__name__}: {exc}")

    def finish(self, seconds, stopped=False, error=None):
        self.stats["seconds"] = round(seconds, 1)
        content = "".join(self.text).strip()
        meta = {"steps": self.steps, "model": self.model_id, "harness": self.harness["id"],
                "harness_name": self.harness["name"], "stats": self.stats,
                "reasoning": "".join(self.reasoning)[-20000:], "skills_loaded": sorted(self.ctx.loaded_skills) if self.ctx else [],
                "stopped": stopped, "error": error, "job_id": self.job["id"]}
        if getattr(self, "plan", None):
            meta["plan"] = self.plan
        if self.guard.stop_reason:
            meta["guard"] = self.guard.stop_reason
        self.db.insert("messages", {"id": self.message_id, "chat_id": self.chat["id"], "role": "assistant",
                                    "content": content, "meta": meta, "created": now()})
        self.db.update("chats", self.chat["id"], {"updated": now()})
        self.emit("assistant_end", content=content, meta=meta)
        if not error and not stopped and self.harness.get("learn", True) and self.harness.get("type") != "pipeline" \
                and not self.chat.get("parent_chat_id"):
            self.app.memory.schedule_learning(self.chat["id"])

    def new_ctx(self):
        self.ctx = ToolContext(app=self.app, chat=self.chat, project=self.project, workspace=self.workspace,
                               folders=self.folders(), readonly=self.app.skills.roots(), emit=self.emit,
                               approve=self.approve, job_id=self.job["id"], model_id=self.model_id,
                               harness=self.harness)
        return self.ctx

    async def base_messages(self, tools):
        system = self.system_prompt(tools)
        history = self.history()
        history = await self.fit_history(system, tools, history)
        messages = [{"role": "system", "content": system}] + [
            {k: v for k, v in m.items() if not k.startswith("_")} for m in history]
        return await self.attach_media(messages, history)

    async def run_loop(self):
        self.new_ctx()
        mode = self.harness.get("mode", "agent")
        tools = await self.build_tools() if mode != "research" else []
        messages = await self.base_messages(tools)
        if mode == "research":
            await self.run_research(messages)
        elif mode == "chat" or not tools:
            await self.stream_text(messages, None)
        elif mode == "planner":
            await self.run_planner(messages, tools)
        else:
            steps = int(self.limits.get("max_steps") or self.harness.get("max_steps") or 10)
            await self.agent_loop(messages, tools, steps)

    # ---- one model call, streaming text, collecting tool calls
    async def stream_text(self, messages, tools, final=True, max_tokens=None):
        thinking = bool(self.chat.get("thinking")) or bool(self.harness.get("thinking"))
        buffer = []
        self.stats["model_calls"] += 1
        self.status("Thinking…" if self.stats["model_calls"] == 1 else "Working…")
        end = None
        async for kind, value in self.app.llm.stream(messages, tools=[t.schema() for t in tools] if tools else None,
                                                     max_tokens=int(max_tokens or self.max_reply),
                                                     temperature=self.temperature, thinking=thinking,
                                                     server=self.server):
            if kind == "token":
                buffer.append(value)
                self.emit("token", text=value)
            elif kind == "reasoning":
                self.reasoning.append(value)
                self.emit("reasoning", text=value)
            elif kind == "end":
                end = value
        self._count(end)
        text = "".join(buffer)
        calls = end["tool_calls"]
        if not calls and tools and "<tool_call>" in text:  # model wrote the call as text
            for m in TEXT_TOOLCALL.finditer(text):
                try:
                    obj = json.loads(m.group(1))
                    calls.append({"id": new_id("call_"), "name": obj.get("name"), "arguments": obj.get("arguments") or {}})
                except json.JSONDecodeError:
                    pass
            if calls:
                text = TEXT_TOOLCALL.sub("", text).strip()
                self.emit("text_replace", text=text)
        if not tools and "<tool_call>" in text:   # asked for a final answer but it still tried to call tools
            text = TEXT_TOOLCALL.sub("", text).strip()
            self.emit("text_replace", text=text)
        if text.strip():
            self.steps.append({"type": "text", "text": text})
            self.text = [text] if final or not calls else self.text
        if end.get("finish_reason") == "length" and not calls:
            self.note(f"The reply reached its length limit ({int(max_tokens or self.max_reply):,} tokens). Raise "
                      "'Reply length' in this chat's limits for longer answers.")
        return text, calls

    def _count(self, end):
        timings = (end or {}).get("timings") or {}
        self.stats["prompt_tokens"] += int(timings.get("prompt_n") or 0)
        self.stats["output_tokens"] += int(timings.get("predicted_n") or 0)
        if timings.get("predicted_per_second"):
            self.stats["tokens_per_second"] = round(timings["predicted_per_second"], 1)

    async def complete(self, messages, **kw):
        self.stats["model_calls"] += 1
        out = await self.app.llm.complete(messages, server=self.server, **kw)
        self._count(out)
        return out

    async def run_tool(self, call):
        tool = self.tool_map.get(call["name"])
        step = {"type": "tool", "id": call["id"], "name": call["name"], "arguments": call["arguments"],
                "label": tool.label if tool else call["name"]}
        self.steps.append(step)
        self.emit("tool_call", call={**step, "description": tool.describe_call(call["arguments"]) if tool else ""})
        blocked = self.guard.check(call["name"], call["arguments"]) if tool else None
        if blocked:
            result, ok = blocked, False
            step["blocked"] = True
        elif not tool:
            result, ok = f"There is no tool called {call['name']!r}. Available: {', '.join(self.tool_map)}", False
        elif "_unparsed" in (call["arguments"] or {}):
            result, ok = "The arguments were not valid JSON; try again with valid JSON arguments.", False
        else:
            try:
                if call["name"] != "use_connector_tool" and not await self.approve(
                        tool, call["arguments"], tool.describe_call(call["arguments"])):
                    raise ToolDenied("The user declined this action.")
                self.app.jobs.progress(self.job["id"], f"{tool.label or tool.name}")
                result = await tool.func(self.ctx, **(call["arguments"] or {}))
                ok = True
            except ToolDenied as exc:
                result, ok = str(exc), False
                step["declined"] = True
            except (ToolError, ValueError, FileNotFoundError, PermissionError, ConnectionError, KeyError) as exc:
                result, ok = f"Error: {exc}", False
            except TypeError as exc:
                result, ok = f"Error: wrong arguments ({exc})", False
            except Exception as exc:
                result, ok = f"Error: {type(exc).__name__}: {str(exc)[:400]}", False
        result = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        self.guard.record(call["name"], call["arguments"], ok, result, blocked=bool(blocked))
        step.update(ok=ok, result=result[:4000])
        self.emit("tool_result", call_id=call["id"], ok=ok, result=result[:4000], declined=step.get("declined", False),
                  blocked=bool(blocked))
        if len(result) > TOOL_RESULT_LIMIT:
            result = result[:TOOL_RESULT_LIMIT] + f"\n…(trimmed; {len(result) - TOOL_RESULT_LIMIT:,} more characters)"
        return result

    async def agent_loop(self, messages, tools, max_steps):
        use_json = self.harness.get("tool_mode") == "json"
        for step in range(max_steps):
            self.take_steer(messages)
            reason = self.guard.should_stop()
            if reason:
                return await self.write_up(messages, reason)
            if use_json:
                calls, done = await self.json_decide(messages, tools)
                if done:
                    await self.stream_text(messages, None)
                    return
                text = ""
            else:
                try:
                    text, calls = await self.stream_text(messages, tools, final=True)
                except LLMError as exc:
                    if "tool" in str(exc).lower() or "500" in str(exc):
                        self.status("This model's template has no native tool calling; switching to JSON mode")
                        use_json = True
                        continue
                    raise
            if not calls:
                if self.take_steer(messages):     # a follow-up arrived while it was answering: carry on
                    messages.insert(len(messages) - 1, {"role": "assistant", "content": text or "(working)"})
                    continue
                return
            messages.append({"role": "assistant", "content": text or "",
                             "tool_calls": [{"id": c["id"], "type": "function",
                                             "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                                            for c in calls]})
            for call in calls:
                result = await self.run_tool(call)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
            nudge = self.guard.nudge()
            if nudge:
                self.note("The agent was not making progress, so the app asked it to change approach.")
                messages.append({"role": "user", "content": nudge})
        await self.write_up(messages, f"it used all {max_steps} steps this harness allows")

    async def write_up(self, messages, reason):
        self.status("Writing up what was found…")
        self.note(f"Stopped using tools because {reason}.")
        messages.append({"role": "user", "content": f"Stop using tools now ({reason}). Using only what you have "
                                                     "already found, tell me: what you did, what you found (with "
                                                     "sources/files), and what is still missing or could not be done."})
        self.text = []
        await self.stream_text(messages, None)

    async def json_decide(self, messages, tools):
        """Grammar-constrained decision for models without native tool calling."""
        options = [{"type": "object", "required": ["action"], "properties": {"action": {"const": "answer"}}}]
        for t in tools:
            params = t.parameters if len(json.dumps(t.parameters)) < 2500 else {"type": "object"}
            options.append({"type": "object", "required": ["action", "tool", "arguments"],
                            "properties": {"action": {"const": "use_tool"}, "tool": {"const": t.name},
                                           "arguments": params}})
        guide = ("Decide the next step. Tools:\n" + "\n".join(f"- {t.name}: {t.description[:200]}" for t in tools) +
                 "\nReply with JSON: {\"action\":\"use_tool\",\"tool\":…,\"arguments\":{…}} or {\"action\":\"answer\"}.")
        self.status("Deciding the next step…")
        out = await self.complete(messages + [{"role": "user", "content": guide}], schema={"oneOf": options},
                                  max_tokens=700)
        decision = out["json"]
        if decision.get("action") != "use_tool":
            return [], True
        call = {"id": new_id("call_"), "name": decision["tool"], "arguments": decision.get("arguments") or {}}
        messages.append({"role": "assistant", "content": json.dumps(decision)})
        result = await self.run_tool(call)
        messages.append({"role": "user", "content": f"Tool result for {call['name']}:\n{result}"})
        return [], False

    async def run_planner(self, messages, tools):
        n = int(self.harness.get("plan_max_steps") or 5)
        schema = {"type": "object", "required": ["steps"], "properties": {
            "steps": {"type": "array", "minItems": 1, "maxItems": n, "items": PLAN_SCHEMA_STEP}}}
        self.status("Making a plan…")
        try:
            plan = (await self.complete(
                messages + [{"role": "user", "content": f"Before doing anything, break this request into at most {n} "
                                                         "concrete steps (fewer is better). JSON only."}],
                schema=schema, max_tokens=600))["json"]["steps"]
        except LLMError:
            plan = [{"title": "Do the task", "detail": ""}]
        self.plan = [{"title": s["title"], "detail": s.get("detail", ""), "state": "todo"} for s in plan]
        self.emit("plan", steps=self.plan)
        total = int(self.limits.get("max_steps") or self.harness.get("max_steps") or 12)
        per_step = max(2, total // max(1, len(plan)))
        messages.append({"role": "assistant", "content": "Plan:\n" + "\n".join(
            f"{i}. {s['title']}" for i, s in enumerate(self.plan, 1))})
        for i, s in enumerate(self.plan, 1):
            if self.guard.should_stop():
                s["state"] = "skipped"
                continue
            s["state"] = "doing"
            self.emit("plan", steps=self.plan, current=i - 1)
            self.steps.append({"type": "plan_step", "index": i, "title": s["title"]})
            messages.append({"role": "user", "content": f"Now do step {i}: {s['title']}. {s['detail']}\n"
                                                         "When this step is done, reply with a short result."})
            await self.agent_loop(messages, tools, per_step)
            messages.append({"role": "assistant", "content": "".join(self.text)[-1500:] or "(done)"})
            s["state"] = "done"
        self.emit("plan", steps=self.plan, current=len(self.plan))
        stopped = self.guard.stop_reason
        messages.append({"role": "user", "content": ("Some steps were skipped because " + stopped + ". " if stopped else "")
                         + "Give me the final answer now: results, files created, and anything left open."})
        self.text = []
        self.steps.append({"type": "plan_step", "index": len(self.plan) + 1, "title": "Final answer"})
        await self.stream_text(messages, None)

    # ------------------------------------------------------------------ research (fixed stages, cannot loop)
    def pseudo_tool(self, name, label, args, description):
        step = {"type": "tool", "id": new_id("call_"), "name": name, "arguments": args, "label": label}
        self.steps.append(step)
        self.emit("tool_call", call={**step, "description": description})
        return step

    def pseudo_done(self, step, ok, result):
        step.update(ok=ok, result=result[:4000])
        self.emit("tool_result", call_id=step["id"], ok=ok, result=result[:4000], declined=False)

    async def run_research(self, messages):
        from .tools import web
        question = self.last_user_text()
        self.take_steer(messages)
        self.status("Planning the searches…")
        n_queries = int(self.harness.get("queries") or 4)
        try:
            plan = (await self.complete(messages + [{"role": "user", "content": (
                "Plan web research for my last message. If it can be answered from this conversation alone (for "
                "example it asks to reformat or explain earlier results) set action to 'answer'. Otherwise set action "
                f"'research' and write up to {n_queries} different search queries of 3-8 plain words each (different "
                "angles; include the organisation that publishes such information when you know it). Set want_files "
                "true if I want data, tables or documents downloaded. known_sources: full URLs of official pages you "
                "are confident exist for this topic (or none). JSON only.")}], schema=RESEARCH_PLAN, max_tokens=500,
                temperature=0.2))["json"]
        except LLMError:
            plan = {"action": "research", "queries": [question[:120]], "want_files": False, "known_sources": []}
        if plan["action"] == "answer" and self.history()[:-1]:
            self.text = []
            return await self.stream_text(messages, None)
        queries = [q for q in dict.fromkeys(q.strip() for q in plan["queries"] if q.strip())][:n_queries] or [question[:120]]
        want_files = bool(plan.get("want_files")) or bool(re.search(r"(?i)\b(download|dataset|data|csv|excel|xlsx|pdf|report|table)s?\b", question))

        # 1. search -------------------------------------------------------------
        found = {}
        for q in queries:
            step = self.pseudo_tool("web_search", "Search the web", {"query": q}, f"Search: {q}")
            self.status(f"Searching: {q}")
            try:
                rows, report = await web.search(q, 8, self.app)
            except Exception as exc:
                rows, report = [], {"engines": [f"error {type(exc).__name__}"], "dropped": 0}
            for r in rows:
                key = r["url"].split("#")[0].rstrip("/")
                if key in found:
                    found[key]["score"] += r["score"] * 0.6
                    found[key]["queries"].append(q)
                else:
                    found[key] = {**r, "queries": [q]}
            self.pseudo_done(step, bool(rows), ("\n".join(f"- {r['title']} — {r['url']}" for r in rows) or
                                                "No relevant results") + f"\n({'; '.join(report['engines'])})")
        for url in plan.get("known_sources") or []:
            if re.match(r"https?://", url) and url.rstrip("/") not in found:
                found[url.rstrip("/")] = {"title": "Known source", "url": url, "snippet": "", "score": 0.9,
                                          "queries": ["known source"]}
        if self.take_steer(messages):
            question += "\n" + messages[-1]["content"]
        # 2. rank and pick pages -------------------------------------------------
        per_host, picked = Counter(), []
        for r in sorted(found.values(), key=lambda r: -r["score"]):
            host = web.host_of(r["url"])
            if per_host[host] >= 2:
                continue
            per_host[host] += 1
            picked.append(r)
            if len(picked) >= int(self.harness.get("pages") or 6):
                break
        if not picked:
            self.note("No relevant sources were found online for this question.")
            messages.append({"role": "user", "content": (
                "Web research found no relevant sources (searches tried: " + "; ".join(queries) + "). Tell me plainly "
                "that nothing reliable was found, give what you know with a clear warning that it is unverified, and "
                "suggest where I could look (organisations, websites) or better search words.")})
            self.text = []
            return await self.stream_text(messages, None)
        # 3. read pages (in parallel) -------------------------------------------
        sem = asyncio.Semaphore(4)

        async def read(r):
            async with sem:
                step = self.pseudo_tool("web_fetch", "Read page", {"url": r["url"]}, f"Read {r['url']}")
                try:
                    page = await asyncio.wait_for(web.fetch_page(r["url"]), 60)
                except asyncio.TimeoutError:
                    page = {"kind": "error", "url": r["url"], "error": "took too long"}
                ok = page["kind"] not in ("error",)
                self.pseudo_done(step, ok, page.get("error") or f"{page.get('title') or r['title']} "
                                 f"({len(page.get('text') or ''):,} characters, {len(page.get('files') or [])} file links)")
                return r, page
        self.status(f"Reading {len(picked)} sources…")
        pages = await asyncio.gather(*(read(r) for r in picked))
        folder = self.workspace / "research" / datetime.now().strftime("%Y%m%d-%H%M%S")
        folder.mkdir(parents=True, exist_ok=True)
        terms = set(web.terms_of(question + " " + " ".join(queries)))
        downloads = []
        for r, page in pages:            # a link that was itself a data file or PDF: keep it
            if page["kind"] == "binary" or (page["kind"] == "pdf" and want_files):
                downloads.append(r["url"])
        # 4. extract facts, page by page ----------------------------------------
        ctx_chars = max(4000, min(14000, (self.server.plan["ctx"] - 2200) * 3))
        sources = []
        for r, page in pages:
            if page["kind"] in ("error", "binary") or not (page.get("text") or "").strip():
                continue
            self.take_steer(messages)
            idx = len(sources) + 1
            text = focus(page["text"], terms, ctx_chars)
            files = page.get("files") or []
            file_list = ""
            if want_files and files:
                ranked = sorted(files, key=lambda f: -sum(t in (f["label"] + " " + f["url"]).lower() for t in terms))[:15]
                file_list = "\n\nFile links on the page:\n" + "\n".join(f"{i}. {f['label'] or '(no label)'} — {f['url']}"
                                                                       for i, f in enumerate(ranked, 1))
                files = ranked
            step = self.pseudo_tool("extract", "Take notes", {"source": idx}, f"Notes from {page.get('title') or r['url']}")
            self.status(f"Taking notes from source {idx}…")
            try:
                got = (await self.complete([
                    {"role": "system", "content": "You extract facts from one web page for a research question. Copy "
                     "numbers, dates and names exactly as written. Only facts stated on the page."},
                    {"role": "user", "content": f"Question: {question}\n\nPage: {page.get('title')}\nURL: {page['url']}\n\n"
                     f"{text}{file_list}\n\nReturn relevant=false if the page does not help. facts: up to 8 short "
                     "facts that answer the question." + (" files: numbers of the file links that contain the "
                                                          "requested data (at most 4)." if file_list else "")}],
                    schema=EXTRACT_SCHEMA, max_tokens=900, temperature=0.1))["json"]
            except LLMError as exc:
                self.pseudo_done(step, False, f"Could not take notes ({str(exc)[:120]})")
                continue
            if not got["relevant"] or not got["facts"]:
                self.pseudo_done(step, False, "Not relevant")
                continue
            for i in got.get("files") or []:
                if 1 <= i <= len(files):
                    downloads.append(files[i - 1]["url"])
            sources.append({"n": idx, "title": page.get("title") or r["title"], "url": page["url"],
                            "facts": got["facts"]})
            self.pseudo_done(step, True, "\n".join(f"- {f}" for f in got["facts"]))
        # 5. download files -----------------------------------------------------
        saved = []
        for url in list(dict.fromkeys(downloads))[:int(self.harness.get("max_downloads") or 6)]:
            step = self.pseudo_tool("download_file", "Download", {"url": url}, f"Download {url}")
            try:
                dest, size = await web.save_url(url, folder / "files", max_bytes=100 * 2**20)
                saved.append({"path": str(dest), "url": url, "size": size})
                self.emit("file_changed", path=str(dest), action="downloaded")
                self.pseudo_done(step, True, f"Saved {size:,} bytes to {dest}")
            except Exception as exc:
                self.pseudo_done(step, False, f"Error: {exc}")
        # 6. write the report ---------------------------------------------------
        notes = "\n\n".join(f"[{s['n']}] {s['title']} — {s['url']}\n" + "\n".join(f"- {f}" for f in s["facts"])
                            for s in sources) or "(no page had relevant facts)"
        files_txt = "\n".join(f"- {f['path']} (from {f['url']})" for f in saved)
        (folder / "notes.md").write_text(f"# Research notes\n\nQuestion: {question}\n\nSearches: {'; '.join(queries)}\n\n"
                                         f"{notes}\n\n## Downloaded files\n{files_txt or '(none)'}\n", encoding="utf-8")
        (folder / "sources.json").write_text(json.dumps({"question": question, "queries": queries, "sources": sources,
                                                         "files": saved}, indent=2, ensure_ascii=False), encoding="utf-8")
        self.steps.append({"type": "note", "text": f"Notes and sources saved in {folder}"})
        messages.append({"role": "user", "content": (
            f"Research notes from {len(sources)} sources (numbered):\n\n{notes}\n\n"
            + (f"Files downloaded:\n{files_txt}\n\n" if saved else "") +
            "Now answer my question using only these notes. Cite sources like [1] after each claim, say clearly what "
            "the sources did not cover, and end with a 'Sources' list (number, title, URL)"
            + (" and the downloaded files with their paths" if saved else "") + ".")})
        self.text = []
        self.status("Writing the answer…")
        await self.stream_text(messages, None)

    # ------------------------------------------------------------------ teams (agents in parallel)
    def team_harnesses(self):
        return [h for h in self.app.harnesses.all() if h.get("type") == "loop" and h.get("mode") != "team"
                and h.get("enabled", True)]

    async def run_team(self):
        spec = list(self.job.get("payload", {}).get("team") or [])
        max_agents = int(self.db.setting("max_parallel_agents", 4))
        harness_ids = [h["id"] for h in self.team_harnesses()]
        question = self.last_user_text()
        await self.hold_model()
        self.new_ctx()
        messages = await self.base_messages([])
        missing = [a for a in spec if not (a.get("task") or "").strip()]
        if not spec or missing:
            self.status("Splitting the work between agents…")
            names = [a.get("name") or f"Agent {i}" for i, a in enumerate(spec, 1)]
            item = {"type": "object", "required": ["name", "task"], "properties": {
                "name": {"enum": names} if spec else {"type": "string", "maxLength": 40},
                "task": {"type": "string", "maxLength": 900},
                "harness": {"enum": harness_ids}}}
            count = len(spec) if spec else None
            schema = {"type": "object", "required": ["agents"], "properties": {"agents": {
                "type": "array", "items": item, "minItems": count or 1, "maxItems": count or max_agents}}}
            hint = ("Agents: " + ", ".join(f"{a.get('name')} (harness {a.get('harness') or 'agent'})" for a in spec)
                    if spec else f"Use 2 to {max_agents} agents.")
            options = "; ".join(f"{h['id']}: {h.get('description', '')[:80]}" for h in self.team_harnesses())
            try:
                planned = (await self.complete(messages + [{"role": "user", "content": (
                    "You lead a team of agents that work at the same time on separate parts of my request. Split it "
                    f"into independent tasks, one per agent. {hint} Each task must be self-contained (include the "
                    f"details the agent needs) and should not depend on another agent's result. Harnesses: {options}. "
                    "JSON only.")}], schema=schema, max_tokens=1200, temperature=0.2))["json"]["agents"]
            except LLMError:
                planned = [{"name": n, "task": question, "harness": "agent"} for n in (names or ["Agent 1"])]
            by_name = {p["name"]: p for p in planned}
            if spec:
                for a in spec:
                    p = by_name.get(a.get("name")) or {}
                    a["task"] = (a.get("task") or "").strip() or p.get("task") or question
                    a["harness"] = a.get("harness") or p.get("harness") or "agent"
            else:
                spec = planned
        await self.drop_model()     # do not hold memory while the agents work
        results = await self.run_agents(spec, overall=question)
        await self.hold_model()
        self.status("Combining the agents' results…")
        budget = max(1500, int((self.server.plan["ctx"] - self.max_reply - 2500) * 3 / max(1, len(results))))
        joined = "\n\n".join(f"### {r['name']} — {r['status']}\nTask: {r['task']}\n\n{r['content'][:budget]}"
                             for r in results)
        messages.append({"role": "user", "content": f"Your team has finished. Their results:\n\n{joined}\n\nCombine "
                         "them into one answer to my request. Keep facts, numbers, sources and file paths; point out "
                         "disagreements between agents and anything that failed or is missing."})
        self.text = []
        await self.stream_text(messages, None)

    async def run_agents(self, agents, overall=""):
        """Start agents as child chats (in parallel, up to the configured number at a time) and wait for them."""
        parent = self.chat
        max_parallel = int(self.db.setting("max_parallel_agents", 4))
        team_step = {"type": "team", "agents": []}
        self.steps.append(team_step)
        sem = asyncio.Semaphore(max_parallel)
        rows = []
        for i, a in enumerate(agents):
            model = a.get("model") or self.model_id
            if model and not self.db.get("models", model):
                model = self.model_id
            row = {"name": a.get("name") or f"Agent {i + 1}", "task": a.get("task") or overall,
                   "harness": a.get("harness") or "agent", "model": model, "limits": a.get("limits") or {},
                   "status": "waiting", "chat_id": None, "job_id": None, "progress": "", "content": ""}
            rows.append(row)
        team_step["agents"] = rows
        shared = {}
        for r in rows:
            shared[r["model"]] = shared.get(r["model"], 0) + 1
        for model, n in shared.items():
            m = self.db.get("models", model) or {}
            if n > 1 and int((m.get("settings") or {}).get("parallel") or 1) < n:
                self.note(f"{n} agents use {m.get('name') or model}; with its current 'Simultaneous replies' setting "
                          f"({int((m.get('settings') or {}).get('parallel') or 1)}) they take turns. Raise it in Models → "
                          "settings to run them truly at the same time (needs more memory).")

        def publish():
            for r in rows:
                if r["job_id"] and r["status"] in ("running", "waiting_approval"):
                    r["progress"] = self.app.jobs.live.get(r["job_id"], r["progress"])
            self.emit("team", agents=[{k: r[k] for k in ("name", "task", "harness", "model", "status", "chat_id",
                                                           "progress")} for r in rows])

        async def one(r):
            async with sem:
                from .app_state import clean_limits
                chat = self.app.create_chat(project_id=parent.get("project_id"), title=f"{r['name']}: {r['task'][:60]}",
                                            model=r["model"], harness=r["harness"], connectors=parent.get("connectors"),
                                            folders=self.folders(), origin="agent", settings=clean_limits(r["limits"]),
                                            parent_chat_id=parent["id"], agent_name=r["name"])
                r["chat_id"] = chat["id"]
                text = (f"Overall request (for context): {overall[:1500]}\n\nYour task: {r['task']}" if overall and
                        overall.strip() != r["task"].strip() else r["task"])
                job = self.app.start_turn(chat["id"], text, kind="agent", payload={"auto_approve": self.auto_approve})
                r["job_id"], r["status"] = job["id"], "running"
                publish()
                task = self.app.jobs.tasks.get(job["id"])
                if task:
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        if not task.done():
                            self.app.jobs.cancel(job["id"])
                        raise
                    except Exception:
                        pass
                status = (self.db.get("jobs", job["id"]) or {}).get("status") or "done"
                msg = self.db.one("SELECT content, meta_json FROM messages WHERE chat_id=? AND role='assistant' "
                                  "ORDER BY created DESC, rowid DESC LIMIT 1", (chat["id"],))
                r["content"] = (msg or {}).get("content") or ((msg or {}).get("meta") or {}).get("error") or "(no result)"
                r["status"] = "done" if status == "done" and not ((msg or {}).get("meta") or {}).get("error") else \
                    ("stopped" if status == "cancelled" else "failed")
                publish()

        self.status(f"{len(rows)} agents are working…")
        publish()
        tasks = [asyncio.create_task(one(r)) for r in rows]
        try:
            while not all(t.done() for t in tasks):
                await asyncio.wait(tasks, timeout=1.5)
                publish()
                done = sum(r["status"] in ("done", "failed", "stopped") for r in rows)
                self.status(f"Agents: {done}/{len(rows)} finished")
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            for r in rows:
                if r["job_id"]:
                    self.app.jobs.cancel(r["job_id"])
            raise
        for t in tasks:
            if not t.cancelled() and t.exception():
                self.note(f"An agent could not start: {t.exception()}")
        return rows

    # ------------------------------------------------------------------ pipelines
    async def pipeline_inputs(self, user_text):
        schema = self.harness.get("inputs") or {"type": "object", "properties": {}}
        props = schema.get("properties", {})
        values = {}
        url = URL_RE.search(user_text or "")
        for key, spec in props.items():
            if "default" in spec:
                values[key] = spec["default"]
        for key, spec in props.items():
            if url and ("url" in key or spec.get("format") == "uri"):
                values[key] = url.group(0)
        if self.harness.get("input_prompt") and self.model_id and props:
            try:
                async with self.app.runtime.lease(self.model_id, on_wait=self.status) as server:
                    self.status("Reading your request…")
                    got = await self.app.llm.json([
                        {"role": "system", "content": self.harness["input_prompt"]},
                        {"role": "user", "content": user_text}], {**schema, "additionalProperties": False},
                        max_tokens=300, server=server)
                values.update({k: v for k, v in got.items() if v not in (None, "")})
            except Exception:
                pass
        text_key = next((k for k in ("topic", "query", "prompt", "subject", "text") if k in props), None)
        if text_key and not values.get(text_key) and not url:
            cleaned = (user_text or "").strip()
            if cleaned and cleaned.lower() not in ("run", "go", "start", "run it", "run now"):
                values[text_key] = cleaned[:200]
        if self.job.get("payload", {}).get("inputs"):
            values.update(self.job["payload"]["inputs"])
        return values

    async def run_pipeline(self):
        h = self.harness
        last_user = self.db.one("SELECT * FROM messages WHERE chat_id=? AND role='user' ORDER BY created DESC, rowid DESC LIMIT 1",
                                (self.chat["id"],))
        inputs = await self.pipeline_inputs((last_user or {}).get("content", ""))
        self.steps.append({"type": "pipeline", "harness": h["name"], "inputs": inputs})
        self.emit("pipeline_start", harness=h["name"], inputs=inputs)
        args = list(h.get("args") or [])
        flags = h.get("args_map") or {}
        for key, value in inputs.items():
            flag = flags.get(key)
            if not flag or value in (None, "", False):
                continue
            args += [flag] if value is True else [flag, str(value)]
        env = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "DVA_WORKSPACE": str(self.workspace),
               "DVA_INPUTS": json.dumps(inputs)}
        if h.get("plugin"):
            plugin = self.app.plugins.get(h["plugin"])
            if not self.app.plugins.configured(plugin):
                raise TurnStopped(f"Set up the {plugin['name']} plugin first: Plugins & connectors → {plugin['name']} "
                                  "→ fill in the settings and press 'Save, check and connect'.")
            base_env = self.app.plugins.script_env(plugin)
        else:
            base_env = {**os.environ}
            base_env.pop("DVA_TOKEN", None)
        base_env.update(env)
        entry = (Path(h["dir"]) / h["entry"]).resolve()
        root = Path(h["dir"]).resolve() if not h.get("plugin") else Path(self.app.plugins.get(h["plugin"])["dir"]).resolve()
        if root not in entry.parents:
            raise TurnStopped("The pipeline's entry file must be inside its folder")
        timeout = int(h.get("timeout_minutes") or 60) * 60
        log_tail = []
        result = {}

        async def execute():
            nonlocal result
            if "browser" in (h.get("needs") or []):
                self.status("Starting the browser helper…")
                base_env["DVA_BROWSER_MCP_URL"] = await self.app.services.browser()
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-u", str(entry), *args, cwd=str(entry.parent), env=base_env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                creationflags=0x08000000 if os.name == "nt" else 0)
            try:
                async for raw in proc.stdout:
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if line.startswith("::result "):
                        try:
                            result = json.loads(line[9:])
                        except json.JSONDecodeError:
                            pass
                        continue
                    if line.startswith("::progress "):
                        try:
                            self.status(json.loads(line[11:]).get("text", ""))
                        except json.JSONDecodeError:
                            pass
                        continue
                    log_tail.append(line)
                    del log_tail[:-400]
                    self.emit("log", text=line)
                    self.app.jobs.log(self.job["id"], line)
                await proc.wait()
            except asyncio.CancelledError:
                proc.kill()
                raise
            return proc.returncode

        async def with_model():
            if "model" in (h.get("needs") or []):
                if not self.model_id:
                    raise TurnStopped("This pipeline needs a model; download one in Models first.")
                async with self.app.runtime.lease(self.model_id, on_wait=self.status) as server:
                    base_env.update({"DV_LLM_API": "openai", "LLM_BASE_URL": server.base_url,
                                     "LLM_API_KEY": server.api_key, "LLM_MODEL": self.model_id})
                    return await execute()
            return await execute()

        try:
            code = await asyncio.wait_for(with_model(), timeout)
        except asyncio.TimeoutError:
            raise TurnStopped(f"The pipeline ran longer than {timeout // 60} minutes and was stopped")
        summary = result.get("summary_markdown") or result.get("summary") or "\n".join(log_tail[-25:])
        self.text = [summary]
        self.steps.append({"type": "pipeline_end", "code": code, "result": result, "log_tail": log_tail[-60:]})
        self.emit("pipeline_end", code=code, result=result)
        if self.model_id and h.get("learn", True):
            outcome = f"exit code {code}\n{summary}\n" + "\n".join(log_tail[-40:])
            self.app.spawn(self.app.memory.reflect(h["id"], h["name"], outcome, self.model_id))


def focus(text, terms, limit):
    """The beginning of a long page plus the lines that mention the question's words (with neighbours)."""
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    keep, size = set(), 0
    for i, line in enumerate(lines):
        if size > min(1500, limit // 4):
            break
        keep.add(i)
        size += len(line) + 1
    scored = sorted(((sum(t in line.lower() for t in terms), i) for i, line in enumerate(lines)),
                    key=lambda x: (-x[0], x[1]))
    for sc, i in scored:
        if sc == 0 or size >= limit:
            break
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(lines) and j not in keep and size + len(lines[j]) < limit:
                keep.add(j)
                size += len(lines[j]) + 1
    out, prev = [], -2
    for i in sorted(keep):
        if i != prev + 1:
            out.append("…")
        out.append(lines[i])
        prev = i
    return "\n".join(out)[:limit]


def _preview(args):
    text = json.dumps(args, ensure_ascii=False, default=str)
    return text if len(text) < 3000 else text[:3000] + "…"
