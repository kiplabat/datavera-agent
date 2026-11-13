"""Memory: what the agent knows, and how it gets better with use.

Scopes: global (about you), project (about one project), chat (notes for one conversation), harness (lessons a
way of working has learned from past tasks). After a conversation goes quiet, a short "learning pass" asks the
local model what is worth remembering (bounded JSON, max 3 items), skips duplicates and secrets, and shows what it
learned in the chat. Finished tasks leave lessons for the harness that ran them. Long chats are summarised so
older turns still count. Everything is visible and editable on the Memory page."""
import asyncio
import difflib
import re

from .db import new_id, now
from .llm import LLMError

SECRET_RE = re.compile(r"(dv_(sk|co)_\w+|sk-[A-Za-z0-9]{16,}|ghp_\w{20,}|hf_\w{20,}|AKIA[0-9A-Z]{16}|"
                       r"password\s*[:=]|\b\d{12,19}\b|-----BEGIN)", re.I)
LEARN_SCHEMA = {
    "type": "object", "required": ["memories"],
    "properties": {
        "memories": {"type": "array", "maxItems": 3, "items": {
            "type": "object", "required": ["scope", "kind", "text"],
            "properties": {"scope": {"enum": ["global", "project", "chat"]},
                           "kind": {"enum": ["fact", "preference", "correction"]},
                           "text": {"type": "string", "minLength": 8, "maxLength": 220}}}},
        "skill_feedback": {"type": "array", "maxItems": 1, "items": {
            "type": "object", "required": ["skill", "suggestion"],
            "properties": {"skill": {"type": "string"}, "suggestion": {"type": "string", "maxLength": 400}}}},
    },
}
LESSON_SCHEMA = {"type": "object", "required": ["lessons"], "properties": {"lessons": {
    "type": "array", "maxItems": 2, "items": {"type": "string", "minLength": 10, "maxLength": 220}}}}
SUMMARY_SCHEMA = {"type": "object", "required": ["summary"],
                  "properties": {"summary": {"type": "string", "maxLength": 1600}}}


def similar(a, b):
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


class Memory:
    def __init__(self, app):
        self.app = app
        self.db = app.db
        self.pending = {}   # chat_id -> asyncio.Task (debounced learning)

    # ------------------------------------------------------------------ store
    def list(self, scope=None, scope_id=None):
        sql, params = "SELECT * FROM memories", []
        if scope:
            sql += " WHERE scope=?"
            params.append(scope)
            if scope_id is not None:
                sql += " AND scope_id=?"
                params.append(scope_id)
        return self.db.all(sql + " ORDER BY pinned DESC, updated DESC", params)

    def add(self, scope, text, scope_id="", kind="fact", source="user", chat_id=None):
        text = " ".join(text.split())
        if SECRET_RE.search(text):
            raise ValueError("That looks like a secret (key, password or card number); memory never stores those")
        for row in self.list(scope, scope_id):
            if similar(row["text"], text) >= 0.85:
                self.db.update("memories", row["id"], {"text": text, "updated": now()})
                return self.db.get("memories", row["id"]), False
        mid = new_id("m_")
        self.db.insert("memories", {"id": mid, "scope": scope, "scope_id": scope_id or "", "kind": kind,
                                    "text": text, "source": source, "chat_id": chat_id, "created": now(),
                                    "updated": now()})
        self.app.bus.publish("memory_changed", scope=scope, scope_id=scope_id)
        return self.db.get("memories", mid), True

    def update(self, mid, values):
        values = {k: v for k, v in values.items() if k in ("text", "pinned", "scope", "scope_id", "kind")}
        if "text" in values and SECRET_RE.search(values["text"]):
            raise ValueError("That looks like a secret; memory never stores those")
        values["updated"] = now()
        self.db.update("memories", mid, values)
        self.app.bus.publish("memory_changed")
        return self.db.get("memories", mid)

    def delete(self, mid):
        self.db.delete("memories", mid)
        self.app.bus.publish("memory_changed")

    def enabled(self, project=None):
        if not self.db.setting("memory_enabled", True):
            return False
        return not project or bool(project.get("memory_enabled", 1))

    # ------------------------------------------------------------------ use in prompts
    def context(self, chat, project, harness_id=None, budget_chars=3500):
        if not self.enabled(project):
            return ""
        blocks = []
        sections = [("About the user (remembered)", self.list("global")[:15])]
        if project:
            sections.append((f"About the project “{project['name']}”", self.list("project", project["id"])[:25]))
        sections.append(("Notes for this conversation", self.list("chat", chat["id"])[:10]))
        if harness_id:
            sections.append(("Lessons from earlier tasks", self.list("harness", harness_id)[:8]))
        used = []
        size = 0
        for title, rows in sections:
            lines = []
            for r in rows:
                line = f"- {r['text']}"
                if size + len(line) > budget_chars:
                    break
                lines.append(line)
                used.append(r["id"])
                size += len(line)
            if lines:
                blocks.append(f"{title}:\n" + "\n".join(lines))
        if used:
            self.db.run(f"UPDATE memories SET uses = uses + 1 WHERE id IN ({','.join('?' * len(used))})", used)
        return "\n\n".join(blocks)

    # ------------------------------------------------------------------ learning after chats
    def schedule_learning(self, chat_id, delay=None):
        if not self.db.setting("learn_enabled", True):
            return
        delay = float(delay if delay is not None else self.db.setting("learn_delay_seconds", 45))
        old = self.pending.pop(chat_id, None)
        if old and not old.done():
            old.cancel()
        self.pending[chat_id] = asyncio.create_task(self._learn_later(chat_id, delay))

    async def _learn_later(self, chat_id, delay):
        try:
            await asyncio.sleep(delay)
            # wait for the model to be free (chats and tasks first), up to 20 minutes
            for _ in range(240):
                if not self.app.jobs.busy_with_model():
                    break
                await asyncio.sleep(5)
            await self.learn(chat_id)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # learning is best-effort
            self.app.log(f"learning pass for {chat_id} failed: {type(exc).__name__}: {exc}")
        finally:
            self.pending.pop(chat_id, None)

    def _learning_model(self, chat):
        chosen = self.db.setting("learning_model") or ""
        if chosen and self.db.get("models", chosen):
            return chosen
        current = self.app.runtime.current
        if current:
            return current["model_id"]
        return chat.get("model") or self.app.models.default_model()

    async def learn(self, chat_id):
        chat = self.db.get("chats", chat_id)
        if not chat:
            return []
        project = self.db.get("projects", chat["project_id"]) if chat.get("project_id") else None
        if not self.enabled(project):
            return []
        rows = self.db.all("SELECT * FROM messages WHERE chat_id=? ORDER BY created, rowid", (chat_id,))
        start = 0
        if chat.get("learned_upto"):
            ids = [r["id"] for r in rows]
            if chat["learned_upto"] in ids:
                start = ids.index(chat["learned_upto"]) + 1
        new = [r for r in rows[start:] if r["role"] in ("user", "assistant") and (r["content"] or "").strip()]
        if not any(r["role"] == "user" for r in new):
            return []
        transcript = "\n".join(f"{r['role'].upper()}: {r['content'][:1200]}" for r in new[-8:])
        known = [r["text"] for r in self.list("global")[:20]]
        if project:
            known += [r["text"] for r in self.list("project", project["id"])[:20]]
        loaded = sorted({s for r in rows for s in (r.get("meta") or {}).get("skills_loaded", [])})
        schema = LEARN_SCHEMA
        if loaded:
            schema = {**LEARN_SCHEMA, "properties": {**LEARN_SCHEMA["properties"], "skill_feedback": {
                **LEARN_SCHEMA["properties"]["skill_feedback"], "items": {
                    **LEARN_SCHEMA["properties"]["skill_feedback"]["items"],
                    "properties": {"skill": {"enum": loaded}, "suggestion": {"type": "string", "maxLength": 400}}}}}}
        else:
            schema = {**LEARN_SCHEMA, "properties": {"memories": LEARN_SCHEMA["properties"]["memories"]}}
        system = (
            "You maintain a small memory so a local assistant improves over time. From the conversation, pick at "
            "most 3 durable things worth remembering for future conversations: stable facts about the user or their "
            "work, preferences about how they want answers, or corrections they made to the assistant. Use scope "
            "'project' for things about this project" + (f" ('{project['name']}')" if project else " (no project: "
            "do not use 'project')") + ", 'global' for the user in general, 'chat' only for this conversation. "
            "Write each as one short third-person sentence ('Prefers…', 'Works on…'). Never store secrets, keys, "
            "passwords, health, money or identity details, one-off task details, or anything already known. "
            "Return an empty list when nothing is worth keeping — that is the usual answer."
            + (" If the user corrected how a loaded skill made the assistant behave, add one skill_feedback item "
               "describing the change to the skill's instructions." if loaded else ""))
        user = ("Already known:\n" + ("\n".join(f"- {k}" for k in known) or "(nothing)") +
                f"\n\nConversation:\n{transcript}")
        model_id = self._learning_model(chat)
        if not model_id:
            return []
        async with self.app.runtime.lease(model_id):
            try:
                out = await self.app.llm.json([{"role": "system", "content": system},
                                               {"role": "user", "content": user}], schema, max_tokens=400)
            except LLMError:
                return []
        saved = []
        for item in out.get("memories", []):
            scope = item["scope"]
            if scope == "project" and not project:
                scope = "global"
            if any(similar(item["text"], k) >= 0.8 for k in known):
                continue
            try:
                row, created = self.add(scope, item["text"], project["id"] if scope == "project" else
                                        (chat_id if scope == "chat" else ""), item["kind"], "learned", chat_id)
            except ValueError:
                continue
            if created:
                saved.append(row)
        for fb in out.get("skill_feedback", []) or []:
            self.db.insert("skill_suggestions", {"id": new_id("s_"), "skill": fb["skill"],
                                                 "suggestion": fb["suggestion"], "chat_id": chat_id, "created": now()})
            self.app.bus.publish("skill_suggestion", skill=fb["skill"])
        self.db.update("chats", chat_id, {"learned_upto": rows[-1]["id"]})
        if saved:
            self.app.bus.publish("memory_learned", chat_id=chat_id,
                                 items=[{"id": r["id"], "text": r["text"], "scope": r["scope"]} for r in saved])
        return saved

    # ------------------------------------------------------------------ lessons after tasks
    async def reflect(self, harness_id, title, outcome_text, model_id):
        if not self.db.setting("learn_enabled", True) or not harness_id or not model_id:
            return []
        known = [r["text"] for r in self.list("harness", harness_id)[:12]]
        system = ("You keep a short list of practical lessons for an automated task so the next run goes better. "
                  "From this run's outcome, write at most 2 concrete, reusable lessons (what to do or avoid next "
                  "time). Return an empty list if there is nothing new.")
        user = (f"Task: {title}\nAlready known lessons:\n" + ("\n".join(f"- {k}" for k in known) or "(none)") +
                f"\n\nOutcome:\n{outcome_text[-3500:]}")
        try:
            async with self.app.runtime.lease(model_id):
                out = await self.app.llm.json([{"role": "system", "content": system},
                                               {"role": "user", "content": user}], LESSON_SCHEMA, max_tokens=300)
        except Exception:
            return []
        saved = []
        for text in out.get("lessons", []):
            if any(similar(text, k) >= 0.8 for k in known):
                continue
            try:
                row, created = self.add("harness", text, harness_id, "lesson", "learned")
                if created:
                    saved.append(row)
            except ValueError:
                pass
        if saved:
            self.app.bus.publish("memory_learned", harness_id=harness_id,
                                 items=[{"id": r["id"], "text": r["text"], "scope": "harness"} for r in saved])
        return saved

    # ------------------------------------------------------------------ chat summaries (long conversations)
    async def summarise(self, chat, older_messages):
        text = "\n".join(f"{m['role'].upper()}: {(m['content'] or '')[:1500]}" for m in older_messages)
        prior = chat.get("summary") or ""
        system = ("Summarise the earlier part of this conversation so it can continue without the full text. Keep "
                  "decisions, facts, file names, numbers and open questions. Max 12 short lines.")
        user = (f"Earlier summary:\n{prior}\n\n" if prior else "") + f"Conversation:\n{text[-12000:]}"
        out = await self.app.llm.json([{"role": "system", "content": system}, {"role": "user", "content": user}],
                                      SUMMARY_SCHEMA, max_tokens=600)
        return out["summary"]
