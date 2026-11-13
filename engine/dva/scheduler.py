"""Scheduled tasks: run a prompt (or a pipeline harness) on a schedule, in its own chat, while the app is open
(it can start with Windows and sit in the tray). Missed runs can catch up once when the app starts again."""
import asyncio
import re
from datetime import datetime, timedelta, timezone

from croniter import croniter

from .db import new_id, now

DAYS = {"monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4, "friday": 5, "saturday": 6, "sunday": 0,
        "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 0}


def _time(text):
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if not m:
        return 9, 0
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3) == "pm" and hour < 12:
        hour += 12
    if m.group(3) == "am" and hour == 12:
        hour = 0
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError("That time is not valid")
    return hour, minute


def parse_when(text):
    """Plain-English schedule (or a 5-field cron expression) → cron expression."""
    t = (text or "").strip().lower()
    if re.fullmatch(r"[\d*/,\-]+(\s+[\d*/,\-a-z]+){4}", t):
        if not croniter.is_valid(t):
            raise ValueError("That cron expression is not valid")
        return t
    m = re.search(r"every\s+(\d+)\s*min", t)
    if m:
        n = int(m.group(1))
        if n < 15:
            raise ValueError("The shortest interval is 15 minutes")
        return f"*/{n} * * * *"
    m = re.search(r"every\s+(\d+)\s*h", t)
    if m:
        return f"0 */{int(m.group(1))} * * *"
    if re.search(r"every\s+hour|hourly", t):
        return "0 * * * *"
    time_part = re.sub(r"every\s+\d+\s*\w+|day\s+\d{1,2}\b|\d{1,2}(st|nd|rd|th)\b", "", t)
    hour, minute = _time(time_part) if re.search(r"\d", time_part) else (9, 0)
    if "month" in t:
        m = re.search(r"day\s+(\d{1,2})|(\d{1,2})(?:st|nd|rd|th)", t)
        day = int(m.group(1) or m.group(2)) if m else 1
        if not 1 <= day <= 28:
            raise ValueError("For monthly tasks choose a day from 1 to 28")
        return f"{minute} {hour} {day} * *"
    if "weekday" in t or "work day" in t or "workday" in t:
        return f"{minute} {hour} * * 1-5"
    if "weekend" in t:
        return f"{minute} {hour} * * 0,6"
    days = [DAYS[d] for d in DAYS if re.search(rf"\b{d}s?\b", t)]
    if days:
        return f"{minute} {hour} * * {','.join(str(d) for d in sorted(set(days)))}"
    if re.search(r"day|daily|night|morning|evening|every", t) or re.search(r"\d", t):
        return f"{minute} {hour} * * *"
    raise ValueError("I could not understand that schedule. Try 'every day at 08:30', 'weekdays at 9', "
                     "'every 3 hours' or 'every monday at 10:00'.")


def describe(cron):
    try:
        minute, hour, dom, month, dow = cron.split()
    except ValueError:
        return cron
    if hour.startswith("*/") and minute == "0":
        return f"Every {hour[2:]} hours"
    if minute.startswith("*/"):
        return f"Every {minute[2:]} minutes"
    if hour == "*" and minute == "0":
        return "Every hour"
    at = f"{int(hour):02d}:{int(minute):02d}" if hour.isdigit() and minute.isdigit() else f"{hour}:{minute}"
    names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    if dow == "1-5":
        return f"Weekdays at {at}"
    if dow == "0,6":
        return f"Weekends at {at}"
    if dow != "*":
        return ", ".join(names[int(d) % 7] for d in dow.split(",") if d.isdigit()) + f" at {at}"
    if dom != "*":
        return f"Monthly on day {dom} at {at}"
    return f"Every day at {at}"


def next_after(cron, after=None):
    base = after or datetime.now().astimezone()
    return croniter(cron, base).get_next(datetime)


class Scheduler:
    def __init__(self, app):
        self.app = app
        self.db = app.db

    def list(self):
        rows = self.db.all("SELECT * FROM schedules ORDER BY name")
        for r in rows:
            r["when"] = describe(r["cron"])
            r["running"] = any((self.db.get("jobs", jid) or {}).get("schedule_id") == r["id"]
                               for jid in self.app.jobs.tasks)
        return rows

    def create(self, data):
        cron = data.get("cron") or parse_when(data.get("when", ""))
        if not croniter.is_valid(cron):
            raise ValueError("Invalid schedule")
        sid = new_id("sch_")
        row = {"id": sid, "name": data.get("name") or "Scheduled task", "enabled": int(data.get("enabled", 1)),
               "cron": cron, "prompt": data.get("prompt", ""), "harness": data.get("harness") or "",
               "model": data.get("model") or "", "project_id": data.get("project_id"),
               "inputs": data.get("inputs") or {}, "chat_mode": data.get("chat_mode", "new"),
               "connectors": data.get("connectors") or [], "auto_approve": int(bool(data.get("auto_approve"))),
               "catch_up": int(data.get("catch_up", 1)), "only_on_ac": int(bool(data.get("only_on_ac"))),
               "max_runs_per_day": int(data.get("max_runs_per_day") or 0),
               "next_run": next_after(cron).isoformat(timespec="seconds"), "created": now(), "updated": now()}
        self.db.insert("schedules", row)
        self.app.bus.publish("schedules_changed")
        return self.db.get("schedules", sid)

    def update(self, sid, data):
        row = self.db.get("schedules", sid)
        if not row:
            raise KeyError(sid)
        values = {}
        for key in ("name", "prompt", "harness", "model", "project_id", "inputs", "chat_mode", "connectors"):
            if key in data:
                values[key] = data[key]
        for key in ("enabled", "auto_approve", "catch_up", "only_on_ac"):
            if key in data:
                values[key] = int(bool(data[key]))
        if "max_runs_per_day" in data:
            values["max_runs_per_day"] = int(data["max_runs_per_day"] or 0)
        if data.get("when") or data.get("cron"):
            values["cron"] = data.get("cron") or parse_when(data["when"])
            if not croniter.is_valid(values["cron"]):
                raise ValueError("Invalid schedule")
        cron = values.get("cron", row["cron"])
        values["next_run"] = next_after(cron).isoformat(timespec="seconds")
        values["updated"] = now()
        self.db.update("schedules", sid, values)
        self.app.bus.publish("schedules_changed")
        return self.db.get("schedules", sid)

    def delete(self, sid):
        self.db.delete("schedules", sid)
        self.app.bus.publish("schedules_changed")

    def runs_today(self, sid):
        start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        return self.db.one("SELECT COUNT(*) AS n FROM jobs WHERE schedule_id=? AND created>=?",
                           (sid, start.astimezone(timezone.utc).isoformat(timespec="seconds")))["n"]

    def run_now(self, sid, reason="manual"):
        s = self.db.get("schedules", sid)
        if not s:
            raise KeyError(sid)
        if any((self.db.get("jobs", jid) or {}).get("schedule_id") == sid for jid in self.app.jobs.tasks):
            raise ValueError("This task is already running")
        chat_id = s.get("chat_id") if s["chat_mode"] == "same" and s.get("chat_id") and \
            self.db.get("chats", s["chat_id"]) else None
        if not chat_id:
            title = f"{s['name']} · {datetime.now().strftime('%d %b %H:%M')}"
            chat = self.app.create_chat(project_id=s.get("project_id"), title=title, model=s.get("model") or None,
                                        harness=s.get("harness") or None, connectors=s.get("connectors") or [],
                                        origin="schedule", schedule_id=sid)
            chat_id = chat["id"]
            if s["chat_mode"] == "same":
                self.db.update("schedules", sid, {"chat_id": chat_id})
        prompt = s.get("prompt") or "Run."
        job = self.app.start_turn(chat_id, prompt, kind="schedule", schedule_id=sid, notify=True,
                                  payload={"auto_approve": bool(s["auto_approve"]), "inputs": s.get("inputs") or {},
                                           "reason": reason})
        self.db.update("schedules", sid, {"last_run": now()})
        return job

    async def loop(self):
        await asyncio.sleep(5)
        first = True
        while True:
            try:
                self.tick(first)
            except Exception as exc:
                self.app.log(f"scheduler: {type(exc).__name__}: {exc}")
            first = False
            await asyncio.sleep(20)

    def tick(self, startup=False):
        from .hardware import power
        current = datetime.now().astimezone()
        for s in self.db.all("SELECT * FROM schedules WHERE enabled=1"):
            due = datetime.fromisoformat(s["next_run"]) if s.get("next_run") else current
            if due > current:
                continue
            missed = current - due > timedelta(minutes=10)
            values = {"next_run": next_after(s["cron"], current).isoformat(timespec="seconds")}
            self.db.update("schedules", s["id"], values)
            if missed and not s["catch_up"]:
                continue
            if s["only_on_ac"] and not power()["on_ac"]:
                self.app.log(f"schedule {s['name']}: skipped (on battery)")
                continue
            if s["max_runs_per_day"] and self.runs_today(s["id"]) >= s["max_runs_per_day"]:
                continue
            try:
                self.run_now(s["id"], reason="catch-up" if missed else "schedule")
            except ValueError:
                continue
        self.app.bus.publish("schedules_tick")
