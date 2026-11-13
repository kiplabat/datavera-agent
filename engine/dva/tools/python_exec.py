"""Run Python code with the app's own Python (pandas, matplotlib, openpyxl are included). Code runs in the chat's
main folder. It is not sandboxed, so it always asks first unless the chat/task auto-approves."""
import asyncio
import os
import subprocess
import sys
import time

from . import Tool, ToolError

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
OUTPUT_LIMIT = 9000


def _snapshot(folder):
    out = {}
    try:
        for p in folder.rglob("*"):
            if p.is_file() and ".runs" not in p.parts:
                out[str(p)] = p.stat().st_mtime
                if len(out) > 5000:
                    break
    except OSError:
        pass
    return out


async def run_python(ctx, code, timeout=None):
    timeout = int(timeout or ctx.app.db.setting("python_timeout_seconds", 180))
    runs = ctx.workspace / ".runs"
    runs.mkdir(parents=True, exist_ok=True)
    script = runs / f"run_{int(time.time() * 1000)}.py"
    script.write_text(code, encoding="utf-8")
    cwd = ctx.primary
    before = await asyncio.to_thread(_snapshot, cwd)
    env = {**os.environ, "MPLBACKEND": "Agg", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
           "DVA_WORKSPACE": str(ctx.workspace)}
    env.pop("DVA_TOKEN", None)
    ctx.emit("status", text="Running Python…")

    def go():
        try:
            return subprocess.run([sys.executable, "-I", str(script)], cwd=str(cwd), env=env, capture_output=True,
                                  text=True, timeout=timeout, encoding="utf-8", errors="replace",
                                  creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            return None
    proc = await asyncio.to_thread(go)
    if proc is None:
        raise ToolError(f"The code ran longer than {timeout} seconds and was stopped")
    after = await asyncio.to_thread(_snapshot, cwd)
    changed = [p for p, t in after.items() if before.get(p) != t]
    out = (proc.stdout or "")
    if proc.stderr:
        out += ("\n[stderr]\n" + proc.stderr)
    if len(out) > OUTPUT_LIMIT:
        out = out[:OUTPUT_LIMIT // 2] + "\n…(output trimmed)…\n" + out[-OUTPUT_LIMIT // 2:]
    for p in changed[:20]:
        ctx.emit("file_changed", path=p, action="written")
    files = ("\nFiles created/changed:\n" + "\n".join(changed[:20])) if changed else ""
    return f"exit code {proc.returncode}\n{out.strip() or '(no output)'}{files}"


TOOLS = [
    Tool("run_python", "Run a Python script (pandas, numpy, matplotlib, openpyxl available) in the chat's main "
         "folder and return its printed output. Save charts/files to disk; print results you need.",
         {"type": "object", "properties": {"code": {"type": "string"}, "timeout": {"type": "integer"}},
          "required": ["code"]},
         run_python, "python", changes=True, label="Run Python",
         summary=lambda a: "Run Python:\n" + (a.get("code") or "")[:600]),
]
