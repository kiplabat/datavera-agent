"""Helper programs the engine starts on demand: the browser helper (Playwright MCP) used by pipelines.

Inside the installed app, Node programs run on the app's own Electron (no Node.js install needed). In
development, it falls back to `npx @playwright/mcp`."""
import asyncio
import json
import os
import platform
import shutil
import socket
import subprocess
from pathlib import Path

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
VERSIONS = json.loads((Path(__file__).with_name("versions.json")).read_text(encoding="utf-8"))


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def free_port(preferred=8931):
    if not port_open(preferred):
        return preferred
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Services:
    def __init__(self, app):
        self.app = app
        self.browser_proc = None
        self.browser_port = None
        self.lock = asyncio.Lock()

    def _browser_cmd(self, port):
        channel = self.app.db.setting("browser_channel") or {"Windows": "msedge", "Darwin": "chrome"}.get(
            platform.system(), "chromium")
        tail = ["--browser", channel, "--isolated", "--port", str(port), "--host", "127.0.0.1"]
        if self.app.db.setting("browser_headless", False):
            tail.append("--headless")
        node, cli = os.environ.get("DVA_NODE"), os.environ.get("DVA_PLAYWRIGHT_MCP")
        env = {**os.environ}
        env.pop("DVA_TOKEN", None)
        if node and cli and Path(cli).exists():
            if os.environ.get("DVA_NODE_IS_ELECTRON"):
                env["ELECTRON_RUN_AS_NODE"] = "1"
            return [node, cli, *tail], env
        npx = shutil.which("npx.cmd" if os.name == "nt" else "npx")
        if not npx:
            raise RuntimeError("The browser helper needs the app's bundled Node or Node.js (npx) in development")
        return [npx, "-y", f"@playwright/mcp@{VERSIONS['playwright_mcp']}", *tail], env

    async def browser(self):
        async with self.lock:
            if self.browser_proc and self.browser_proc.poll() is None and port_open(self.browser_port):
                return f"http://127.0.0.1:{self.browser_port}/mcp"
            port = free_port()
            if port_open(port):  # someone else's helper (e.g. the command-line agent) is already there
                return f"http://127.0.0.1:{port}/mcp"
            cmd, env = self._browser_cmd(port)
            log = (self.app.paths.data / "logs" / "browser-helper.log").open("ab")
            self.browser_proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env,
                                                 creationflags=NO_WINDOW)
            log.close()
            self.browser_port = port
            for _ in range(240):
                if port_open(port):
                    return f"http://127.0.0.1:{port}/mcp"
                if self.browser_proc.poll() is not None:
                    raise RuntimeError("The browser helper stopped while starting; see logs/browser-helper.log")
                await asyncio.sleep(0.5)
            raise RuntimeError("The browser helper did not start within 2 minutes")

    async def shutdown(self):
        proc, self.browser_proc = self.browser_proc, None
        if proc and proc.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                               creationflags=NO_WINDOW)
            else:
                proc.terminate()
