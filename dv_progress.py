"""Console progress, heartbeat and redacted append-only run logs (no LangChain dependency)."""
import asyncio
import json
import re
import threading
import time
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SECRET_KEYS = {"token", "access_token", "refresh_token", "api_key", "apikey", "key", "password",
               "secret", "authorization", "code", "sig", "signature"}


def safe_text(value):
    """Remove API keys, bearer tokens and secret-looking URL parameters from text."""
    value = str(value)
    value = re.sub(r"dv_(?:sk|co)_[A-Za-z0-9_-]+", "[REDACTED_KEY]", value)
    value = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)

    def clean_url(match):
        raw = match.group(0)
        trimmed = raw.rstrip(".;,)")
        suffix = raw[len(trimmed):]
        try:
            p = urlsplit(trimmed)
            host = p.netloc.rsplit("@", 1)[-1]
            query = [(k, "[REDACTED]" if k.casefold() in SECRET_KEYS else v)
                     for k, v in parse_qsl(p.query, keep_blank_values=True)]
            return urlunsplit((p.scheme, host, p.path, urlencode(query), "")) + suffix
        except ValueError:
            return "[INVALID_URL]"
    return re.sub(r"https?://[^\s\"<>]+", clean_url, value)


def safe_value(value):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if str(k).casefold() in SECRET_KEYS else safe_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_value(v) for v in value]
    return safe_text(value) if isinstance(value, str) else value


class RunProgress:
    """Every event goes to the console, progress.log and events.jsonl in the run folder."""

    def __init__(self, folder, quiet=False):
        self.folder = folder
        self.quiet = quiet
        self.started = time.monotonic()
        self.lock = threading.RLock()
        self.state = {"stage": "starting", "action": "Initialising", "since": self.started}

    def event(self, kind, message, details=None):
        with self.lock:
            elapsed = time.monotonic() - self.started
            stamp = datetime.now().astimezone().isoformat(timespec="seconds")
            message = safe_text(message)
            line = f"[{stamp[11:19]} +{int(elapsed)//60:02d}:{int(elapsed)%60:02d}] [{kind}] {message}"
            if not self.quiet:
                print(line, flush=True)
            if self.folder:
                with (self.folder / "progress.log").open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
                entry = {"time": stamp, "elapsed_seconds": round(elapsed, 2), "kind": kind,
                         "message": message, "details": safe_value(details or {})}
                with (self.folder / "events.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def busy(self, stage, action):
        """Record what is happening now so the heartbeat can report it."""
        with self.lock:
            self.state.update(stage=stage, action=safe_text(action), since=time.monotonic())

    def idle(self):
        with self.lock:
            self.state.update(action="between steps", since=time.monotonic())

    async def heartbeat(self, interval=15):
        while True:
            await asyncio.sleep(interval)
            with self.lock:
                waited = time.monotonic() - self.state["since"]
                action, stage = self.state["action"], self.state["stage"]
            if waited >= interval - 1 and action != "between steps":
                self.event("waiting", f"{stage}: {action}; {waited:.0f}s so far")
