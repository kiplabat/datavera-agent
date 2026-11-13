"""Runs one chat turn with the chosen harness and model, streaming everything to the UI.

Loop harnesses: chat (no tools), agent (tool loop with a step limit), planner (plan → steps → answer).
Pipeline harnesses: a Python program run as a task; its output streams into the chat.

Each turn is saved as one assistant message whose meta.steps lists text, tool calls and results in order, so a
reloaded chat looks exactly like the live one. Later turns see earlier turns' answers plus a one-line note of the
tools used (keeps prompts small for local models); long chats are summarised automatically."""
import asyncio
import json
import os
import platform
import re
import sys
import time
from datetime import datetime
from pathlib import Path

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
Changes (writing files, running code, scheduling, connector actions that change things) are shown to the user for \
approval by the app; if one is declined, do not retry it — explain or choose another way."""


class TurnStopped(Exception):
    pass


class Turn:
    def __init__(self, app, chat_id, job):
        self.app, self.db = app, app.db
        self.job = job
        self.chat = self.db.get("chats", chat_id)
        self.project = self.db.get("projects", self.chat["project_id"]) if self.chat.get("project_id") else None
        hid = self.chat.get("harness") or (self.project or {}).get("default_harness") or app.harnesses.default_id()
        self.harness = app.harnesses.get(hid) or app.harnesses.get("agent")
        self.model_id = self.chat.get("model") or (self.project or {}).get("default_model") or app.models.default_model()
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

    # ------------------------------------------------------------------ events
    def emit(self, type_, **data):
        self.app.bus.publish("chat", chat_id=self.chat["id"], job_id=self.job["id"], type=type_,
                             message_id=self.message_id, **data)

    def status(self, text):
        self.emit("status", text=text)
        self.app.jobs.progress(self.job["id"], text)

    # ------------------------------------------------------------------ approvals
    async def approve(self, tool, args, description):
        if not tool.changes:
            return True
        if self.auto_approve or tool.name in self.allowed_always:
            return True
        approval_id = new_id("ap_")
        fut = asyncio.get_running_loop().create_future()
        self.app.approvals[approval_id] = {"future": fut, "chat_id": self.chat["id"], "job_id": self.job["id"],
                                           "tool": tool.name, "description": description}
        self.emit("approval", approval_id=approval_id, tool=tool.name, label=tool.label or tool.name,
                  description=description, arguments=_preview(args))
        self.app.jobs.set_status(self.job["id"], "waiting")
        self.app.bus.publish("notify", title="Approval needed",
                             body=f"{self.chat.get('title') or 'A chat'}: {tool.label or tool.name}",
                             chat_id=self.chat["id"])
        timeout = float(self.db.setting("approval_timeout_minutes", 360)) * 60
        try:
            decision = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            decision = {"allow": False, "reason": "timed out"}
        finally:
            self.app.approvals.pop(approval_id, None)
            self.app.jobs.set_status(self.job["id"], "running")
        self.emit("approval_done", approval_id=approval_id, allow=bool(decision.get("allow")))
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
        if not self.db.setting("allow_python", True):
            groups = [g for g in groups if g != "python"]
        return set(groups)

    async def build_tools(self):
        if self.harness.get("mode") == "chat":
            return []
        groups = self.tool_groups()
        tools = [t for t in builtin_tools() if t.group in groups]
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

    def system_prompt(self, tools):
        model = self.db.get("models", self.model_id) if self.model_id else None
        parts = [BASE_PROMPT.format(model=(model or {}).get("name") or self.model_id or "local model",
                                    today=datetime.now().strftime("%A %d %B %Y"),  # date only: a changing prefix defeats prompt caching
                                    os=f"{platform.system()} {platform.release()}")]
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
            if r["role"] == "user":
                out.append({"role": "user", "content": self._user_content(r), "_id": r["id"]})
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
            text += f"\n\n[Attached: {att['name']} ({att.get('size', 0):,} bytes) at {p}]"
            if att.get("inline") and p.exists() and p.stat().st_size <= 12000:
                try:
                    text += f"\n```\n{p.read_text(encoding='utf-8', errors='replace')[:12000]}\n```"
                except OSError:
                    pass
        return text

    async def fit_history(self, system, tools, messages):
        ctx_tokens = (self.app.runtime.current or {}).get("ctx", 8192)
        reserve = int(self.db.setting("max_reply_tokens", 2048))
        fixed = estimate_tokens(system) + estimate_tokens(json.dumps([t.schema() for t in tools]))
        budget = ctx_tokens - reserve - fixed - 200
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

    # ------------------------------------------------------------------ running
    async def run(self):
        started = time.monotonic()
        self.emit("assistant_start", harness=self.harness["name"], model=self.model_id)
        try:
            if self.harness.get("type") == "pipeline":
                await self.run_pipeline()
            else:
                if not self.model_id:
                    raise TurnStopped("No model is installed yet. Open Models and download one (the recommended "
                                      "one is marked).")
                async with self.app.runtime.lease(self.model_id, on_wait=self.status):
                    await self.run_loop()
        except asyncio.CancelledError:
            self.steps.append({"type": "note", "text": "Stopped by the user."})
            self.finish(time.monotonic() - started, stopped=True)
            raise
        except TurnStopped as exc:
            self.steps.append({"type": "error", "text": str(exc)})
            self.emit("error", text=str(exc))
            self.finish(time.monotonic() - started, error=str(exc))
            return
        except Exception as exc:
            msg = f"{type(exc).__name__}: {str(exc)[:500]}"
            self.steps.append({"type": "error", "text": msg})
            self.emit("error", text=msg)
            self.finish(time.monotonic() - started, error=msg)
            raise
        self.finish(time.monotonic() - started)

    def finish(self, seconds, stopped=False, error=None):
        self.stats["seconds"] = round(seconds, 1)
        content = "".join(self.text).strip()
        meta = {"steps": self.steps, "model": self.model_id, "harness": self.harness["id"],
                "harness_name": self.harness["name"], "stats": self.stats,
                "reasoning": "".join(self.reasoning)[-20000:], "skills_loaded": sorted(self.ctx.loaded_skills) if self.ctx else [],
                "stopped": stopped, "error": error, "job_id": self.job["id"]}
        if getattr(self, "plan", None):
            meta["plan"] = self.plan
        self.db.insert("messages", {"id": self.message_id, "chat_id": self.chat["id"], "role": "assistant",
                                    "content": content, "meta": meta, "created": now()})
        self.db.update("chats", self.chat["id"], {"updated": now()})
        self.emit("assistant_end", content=content, meta=meta)
        if not error and not stopped and self.harness.get("learn", True) and self.harness.get("type") != "pipeline":
            self.app.memory.schedule_learning(self.chat["id"])

    def new_ctx(self):
        self.ctx = ToolContext(app=self.app, chat=self.chat, project=self.project, workspace=self.workspace,
                               folders=self.folders(), readonly=self.app.skills.roots(), emit=self.emit,
                               approve=self.approve, job_id=self.job["id"], model_id=self.model_id,
                               harness=self.harness)
        return self.ctx

    async def run_loop(self):
        self.new_ctx()
        tools = await self.build_tools()
        system = self.system_prompt(tools)
        history = self.history()
        history = await self.fit_history(system, tools, history)
        messages = [{"role": "system", "content": system}] + [{k: v for k, v in m.items() if k != "_id"} for m in history]
        mode = self.harness.get("mode", "agent")
        if mode == "chat" or not tools:
            await self.stream_text(messages, None)
        elif mode == "planner":
            await self.run_planner(messages, tools)
        else:
            await self.agent_loop(messages, tools, int(self.harness.get("max_steps") or 10))

    # ---- one model call, streaming text, collecting tool calls
    async def stream_text(self, messages, tools, final=True):
        thinking = bool(self.chat.get("thinking")) or bool(self.harness.get("thinking"))
        temperature = float(self.harness.get("temperature", 0.4))
        buffer = []
        self.stats["model_calls"] += 1
        self.status("Thinking…" if self.stats["model_calls"] == 1 else "Working…")
        end = None
        async for kind, value in self.app.llm.stream(messages, tools=[t.schema() for t in tools] if tools else None,
                                                     max_tokens=int(self.db.setting("max_reply_tokens", 2048)),
                                                     temperature=temperature, thinking=thinking):
            if kind == "token":
                buffer.append(value)
                self.emit("token", text=value)
            elif kind == "reasoning":
                self.reasoning.append(value)
                self.emit("reasoning", text=value)
            elif kind == "end":
                end = value
        timings = end.get("timings") or {}
        self.stats["prompt_tokens"] += int(timings.get("prompt_n") or 0)
        self.stats["output_tokens"] += int(timings.get("predicted_n") or 0)
        if timings.get("predicted_per_second"):
            self.stats["tokens_per_second"] = round(timings["predicted_per_second"], 1)
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
        if text.strip():
            self.steps.append({"type": "text", "text": text})
            self.text = [text] if final or not calls else self.text
        return text, calls

    async def run_tool(self, call):
        tool = self.tool_map.get(call["name"])
        step = {"type": "tool", "id": call["id"], "name": call["name"], "arguments": call["arguments"],
                "label": tool.label if tool else call["name"]}
        self.steps.append(step)
        self.emit("tool_call", call={**step, "description": tool.describe_call(call["arguments"]) if tool else ""})
        if not tool:
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
        step.update(ok=ok, result=result[:4000])
        self.emit("tool_result", call_id=call["id"], ok=ok, result=result[:4000], declined=step.get("declined", False))
        if len(result) > TOOL_RESULT_LIMIT:
            result = result[:TOOL_RESULT_LIMIT] + f"\n…(trimmed; {len(result) - TOOL_RESULT_LIMIT:,} more characters)"
        return result

    async def agent_loop(self, messages, tools, max_steps):
        use_json = self.harness.get("tool_mode") == "json"
        for step in range(max_steps):
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
                return
            messages.append({"role": "assistant", "content": text or "",
                             "tool_calls": [{"id": c["id"], "type": "function",
                                             "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                                            for c in calls]})
            for call in calls:
                result = await self.run_tool(call)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        self.status("Step limit reached; writing up")
        self.steps.append({"type": "note", "text": f"Reached the harness's limit of {max_steps} steps."})
        messages.append({"role": "user", "content": "You have used all your steps. Stop using tools and tell me "
                                                     "what you did, what you found, and what is left to do."})
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
        self.stats["model_calls"] += 1
        self.status("Deciding the next step…")
        out = await self.app.llm.complete(messages + [{"role": "user", "content": guide}],
                                          schema={"oneOf": options}, max_tokens=700)
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
        self.stats["model_calls"] += 1
        try:
            plan = (await self.app.llm.complete(
                messages + [{"role": "user", "content": f"Before doing anything, break this request into at most {n} "
                                                         "concrete steps (fewer is better). JSON only."}],
                schema=schema, max_tokens=600))["json"]["steps"]
        except LLMError:
            plan = [{"title": "Do the task", "detail": ""}]
        self.plan = [{"title": s["title"], "detail": s.get("detail", ""), "state": "todo"} for s in plan]
        self.emit("plan", steps=self.plan)
        per_step = max(2, int(self.harness.get("max_steps") or 12) // max(1, len(plan)))
        messages.append({"role": "assistant", "content": "Plan:\n" + "\n".join(
            f"{i}. {s['title']}" for i, s in enumerate(self.plan, 1))})
        for i, s in enumerate(self.plan, 1):
            s["state"] = "doing"
            self.emit("plan", steps=self.plan, current=i - 1)
            self.steps.append({"type": "plan_step", "index": i, "title": s["title"]})
            messages.append({"role": "user", "content": f"Now do step {i}: {s['title']}. {s['detail']}\n"
                                                         "When this step is done, reply with a short result."})
            await self.agent_loop(messages, tools, per_step)
            messages.append({"role": "assistant", "content": "".join(self.text)[-1500:] or "(done)"})
            s["state"] = "done"
        self.emit("plan", steps=self.plan, current=len(self.plan))
        messages.append({"role": "user", "content": "All steps are done. Give me the final answer now: results, "
                                                     "files created, and anything left open."})
        self.text = []
        self.steps.append({"type": "plan_step", "index": len(self.plan) + 1, "title": "Final answer"})
        await self.stream_text(messages, None)

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
                async with self.app.runtime.lease(self.model_id, on_wait=self.status):
                    self.status("Reading your request…")
                    got = await self.app.llm.json([
                        {"role": "system", "content": self.harness["input_prompt"]},
                        {"role": "user", "content": user_text}], {**schema, "additionalProperties": False},
                        max_tokens=300)
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
                async with self.app.runtime.lease(self.model_id, on_wait=self.status):
                    rt = self.app.runtime
                    base_env.update({"DV_LLM_API": "openai", "LLM_BASE_URL": rt.base_url, "LLM_API_KEY": rt.api_key,
                                     "LLM_MODEL": self.model_id})
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
            asyncio.create_task(self.app.memory.reflect(h["id"], h["name"], outcome, self.model_id))


def _preview(args):
    text = json.dumps(args, ensure_ascii=False, default=str)
    return text if len(text) < 3000 else text[:3000] + "…"
