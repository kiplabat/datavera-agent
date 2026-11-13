"""llama.cpp runtime: which builds are installed (CPU / Vulkan / CUDA / Metal), installing them from the pinned
llama.cpp release, and running `llama-server` processes for the models in use.

Several models can be loaded at once (one llama-server each) when memory allows, so agents in a team can use
different models in parallel. A chat or task "leases" a model for as long as it needs it; when memory is short,
idle models are unloaded first, and if every loaded model is busy the new request waits. Requests to the same model
share its server (its "simultaneous replies" setting decides how many run at the same time; the rest queue).
Models never leave this computer: servers listen on 127.0.0.1 only and need a random API key."""
import asyncio
import contextvars
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import tarfile
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from .download import download, fetch_json

VERSIONS = json.loads((Path(__file__).with_name("versions.json")).read_text(encoding="utf-8"))
EXE = "llama-server.exe" if os.name == "nt" else "llama-server"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DEVICE_LINE = re.compile(r"^\s*(\S+):\s+(.+?)\s+\((\d+)\s*MiB,\s*(\d+)\s*MiB free\)")
BACKEND_LABELS = {"cpu": "CPU", "vulkan": "GPU (Vulkan: AMD, Intel, NVIDIA)", "cuda": "GPU (NVIDIA CUDA)",
                  "metal": "GPU (Apple Metal)", "custom": "Custom llama-server"}


def os_key():
    return {"Windows": "windows", "Darwin": "darwin"}.get(platform.system(), "linux")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RuntimeError_(RuntimeError):
    pass


ACTIVE = contextvars.ContextVar("dva_active_server", default=None)


class Server:
    """One running llama-server."""
    def __init__(self, model, plan, api_key):
        self.model_id = model["id"]
        self.model = model
        self.plan = plan
        self.api_key = api_key
        self.port = None
        self.proc = None
        self.users = 0
        self.last_used = time.monotonic()
        self.loaded_at = None
        self.need_bytes = 0

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.api_key}"}

    @property
    def alive(self):
        return bool(self.proc and self.proc.poll() is None)

    @property
    def current(self):
        return {"model_id": self.model_id, **{k: v for k, v in self.plan.items() if k != "exe"}, "port": self.port,
                "loaded_at": self.loaded_at, "users": self.users, "vision": bool(self.plan.get("mmproj"))}

    def matches(self, plan):
        return all(self.plan.get(k) == plan.get(k) for k in ("compute", "ctx", "parallel", "mmproj"))


class Runtime:
    def __init__(self, paths, db, bus):
        self.paths, self.db, self.bus = paths, db, bus
        self.api_key = secrets.token_urlsafe(24)
        self.servers = {}            # model_id -> Server
        self.cond = asyncio.Condition()
        self.last_used = time.monotonic()
        self.status = {"state": "idle"}
        self.last_server = None
        self.installing = {}
        self.log_path = paths.data / "logs" / "llama-server.log"
        self._devices_cache = {}
        self._recommended = None

    # ------------------------------------------------------------------ backends
    @property
    def tag(self):
        return VERSIONS["llama_cpp_tag"]

    def backend_dir(self, backend):
        return self.paths.data / "runtimes" / "llama" / self.tag / backend

    def backends(self):
        """Installed llama-server builds, best first."""
        found = {}
        custom = os.environ.get("DVA_LLAMA_SERVER") or self.db.setting("llama_server_path")
        if custom and Path(custom).exists():
            found["custom"] = Path(custom)
        for backend in ("cuda", "vulkan", "metal", "cpu"):
            for folder in (self.backend_dir(backend), self.paths.bundled_runtime(backend)):
                if folder and (folder / EXE).exists():
                    found.setdefault(backend, folder / EXE)
                elif folder and folder.exists():
                    hits = list(folder.rglob(EXE))
                    if hits:
                        found.setdefault(backend, hits[0])
        return found

    def available_backends(self):
        return list(VERSIONS["backend_assets"].get(os_key(), {}).keys() - {"cuda_runtime"})

    def list_devices(self, backend):
        exe = self.backends().get(backend)
        if not exe:
            return []
        if backend in self._devices_cache:
            return self._devices_cache[backend]
        try:
            out = subprocess.run([str(exe), "--list-devices"], capture_output=True, text=True, timeout=30,
                                 cwd=str(exe.parent), creationflags=NO_WINDOW)
            text = out.stdout + "\n" + out.stderr
        except Exception as exc:
            return [{"error": f"{type(exc).__name__}"}]
        devices = []
        for line in text.splitlines():
            m = DEVICE_LINE.match(line)
            if m:
                devices.append({"id": m.group(1), "name": m.group(2), "total_mb": int(m.group(3)),
                                "free_mb": int(m.group(4))})
        self._devices_cache[backend] = devices
        return devices

    async def install_backend(self, backend):
        """Download the matching llama.cpp release build (and the CUDA runtime DLLs for CUDA)."""
        if backend in self.installing:
            return
        patterns = VERSIONS["backend_assets"].get(os_key(), {})
        if backend not in patterns:
            raise RuntimeError_(f"No {backend} build is published for this operating system")
        self.installing[backend] = {"state": "starting"}
        try:
            api = f"https://api.github.com/repos/{VERSIONS['llama_cpp_repo']}/releases"

            def choose(assets):
                def pick(pats, exclude=None):
                    for a in assets:
                        name = a["name"]
                        if all(p in name for p in pats) and (name.endswith(".zip") or name.endswith(".tar.gz")) \
                                and not (exclude and name.startswith(exclude)):
                            return a
                    return None
                found = [pick(patterns[backend], exclude="cudart")]
                if backend == "cuda" and "cuda_runtime" in patterns:
                    found.append(pick(patterns["cuda_runtime"]))
                return found if all(found) else None
            try:
                wanted = choose((await fetch_json(f"{api}/tags/{self.tag}")).get("assets", []))
            except Exception:
                wanted = None
            if not wanted:
                # Some llama.cpp releases are published without binaries; use the newest one that has them.
                for rel in await fetch_json(f"{api}?per_page=30"):
                    wanted = choose(rel.get("assets", []))
                    if wanted:
                        self.bus.publish("runtime", action="install_note", backend=backend,
                                         note=f"llama.cpp {self.tag} has no {backend} build; using {rel.get('tag_name')}")
                        break
            if not wanted:
                raise RuntimeError_(f"No recent llama.cpp release has a {backend} download for this computer")
            target = self.backend_dir(backend)
            tmp = target.with_name(target.name + ".tmp")
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            for asset in wanted:
                archive = self.paths.data / "runtimes" / asset["name"]

                def progress(done, total, rate, name=asset["name"]):
                    self.installing[backend] = {"state": "downloading", "file": name, "done": done, "total": total}
                    self.bus.publish("runtime", action="install_progress", backend=backend, file=name,
                                     done=done, total=total, rate=rate)
                await download(asset["browser_download_url"], archive, expected_size=asset.get("size"),
                               progress=progress)
                self.installing[backend] = {"state": "extracting"}
                await asyncio.to_thread(self._extract, archive, tmp)
                archive.unlink(missing_ok=True)
            exe = next(iter(tmp.rglob(EXE)), None)
            if not exe:
                raise RuntimeError_("The download did not contain llama-server")
            # place all files of the folder that holds llama-server directly in the backend folder
            shutil.rmtree(target, ignore_errors=True)
            if exe.parent != tmp:
                for item in list(tmp.rglob("*")):  # e.g. CUDA runtime DLLs extracted beside the build folder
                    if item.is_file() and item.parent != exe.parent and not (exe.parent / item.name).exists():
                        shutil.move(str(item), str(exe.parent / item.name))
                shutil.move(str(exe.parent), str(target))
                shutil.rmtree(tmp, ignore_errors=True)
            else:
                shutil.move(str(tmp), str(target))
            if os.name != "nt":
                (target / EXE).chmod(0o755)
            self._devices_cache.pop(backend, None)
            self.bus.publish("runtime", action="installed", backend=backend)
        except Exception as exc:
            self.bus.publish("runtime", action="install_failed", backend=backend, error=str(exc)[:300])
            raise
        finally:
            self.installing.pop(backend, None)

    @staticmethod
    def _extract(archive, dest):
        if str(archive).endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                z.extractall(dest)
        else:
            with tarfile.open(archive) as t:
                t.extractall(dest, filter="data")

    def remove_backend(self, backend):
        if any(s.plan["backend"] == backend for s in self.servers.values()):
            raise RuntimeError_("That build is running the current model; unload the model first")
        shutil.rmtree(self.backend_dir(backend), ignore_errors=True)
        self._devices_cache.pop(backend, None)

    def info(self):
        installed = self.backends()
        return {
            "tag": self.tag, "installed": {k: str(v) for k, v in installed.items()},
            "available": self.available_backends(), "labels": BACKEND_LABELS,
            "devices": {b: self.list_devices(b) for b in installed if b not in ("cpu",)},
            "installing": self.installing, "status": self.status, "current": self.current, "loaded": self.loaded(),
        }

    # ------------------------------------------------------------------ choose how to run a model
    def plan(self, model, compute=None):
        """Decide backend and flags for a model. compute: auto | cpu | gpu."""
        settings = model.get("settings") or {}
        compute = compute or settings.get("compute") or self.db.setting("default_compute", "auto")
        installed = self.backends()
        gpu_backends = [b for b in ("custom", "cuda", "metal", "vulkan") if b in installed]
        if compute == "gpu" and not gpu_backends:
            raise RuntimeError_("GPU was chosen but no GPU build is installed. Install one in Models → Hardware, "
                                "or switch this model to CPU.")
        if compute == "auto":
            if self._recommended is None:
                from .hardware import gpus, recommend_backend
                self._recommended = recommend_backend(gpus())
            compute = "gpu" if self._recommended in gpu_backends else "cpu"
        if compute == "gpu":
            backend = gpu_backends[0]
        else:
            backend = "cpu" if "cpu" in installed else (next(iter(installed), None))
        if not backend:
            raise RuntimeError_("No llama.cpp runtime is installed. Reinstall the app, or install one in Models → Hardware.")
        threads = settings.get("threads") or self.db.setting("threads") or None
        if not threads:
            import psutil
            threads = psutil.cpu_count(logical=False) or 4
        ctx = int(settings.get("ctx") or self.db.setting("default_ctx", 8192))
        mmproj = settings.get("mmproj") if settings.get("vision", True) else None
        if mmproj and not Path(mmproj).exists():
            mmproj = None
        return {"backend": backend, "exe": str(installed[backend]), "compute": compute, "ctx": ctx,
                "threads": int(threads), "gpu_layers": settings.get("gpu_layers", "auto"),
                "parallel": max(1, min(8, int(settings.get("parallel") or 1))), "mmproj": mmproj}

    # ------------------------------------------------------------------ process
    def _command(self, model, plan, port):
        # -c is shared by all parallel slots, so each simultaneous reply still gets the full context
        cmd = [plan["exe"], "-m", model["path"], "--host", "127.0.0.1", "--port", str(port),
               "--api-key", self.api_key, "-c", str(plan["ctx"] * plan["parallel"]), "-t", str(plan["threads"]),
               "-np", str(plan["parallel"]), "--jinja", "--reasoning-format", "deepseek", "--no-webui",
               "--alias", model["id"], "--cache-reuse", "256"]
        if plan["compute"] == "cpu":
            cmd += ["--device", "none", "-ngl", "0"]
        else:
            cmd += ["-ngl", str(plan["gpu_layers"] or "auto")]
        if plan.get("mmproj"):
            cmd += ["--mmproj", plan["mmproj"]]
            if plan["compute"] == "cpu":
                cmd += ["--no-mmproj-offload"]
        extra = (model.get("settings") or {}).get("extra_args")
        if extra:
            cmd += [a for a in extra.split() if a]
        return cmd

    def _need_bytes(self, model, plan):
        from . import gguf
        size = int(model.get("size") or 0)
        if plan.get("mmproj"):
            try:
                size += Path(plan["mmproj"]).stat().st_size
            except OSError:
                pass
        kv = gguf.kv_cache_bytes(model.get("meta"), plan["ctx"] * plan["parallel"]) if model.get("meta") else None
        if kv is None:
            kv = max(0.35, size / 2**30 * 0.12) * (plan["ctx"] * plan["parallel"] / 8192) * 2**30
        return int(size + kv + 0.4 * 2**30)

    def _free_bytes(self):
        import psutil
        return int(psutil.virtual_memory().available)

    async def _start(self, model, plan):
        if not Path(model["path"]).exists():
            raise RuntimeError_(f"The model file is missing: {model['path']}")
        server = Server(model, plan, self.api_key)
        server.port = free_port()
        server.need_bytes = self._need_bytes(model, plan)
        cmd = self._command(model, plan, server.port)
        self.status = {"state": "loading", "model_id": model["id"], "backend": plan["backend"], "since": time.time()}
        self._publish()
        log = self.log_path.open("ab")
        log.write(f"\n==== {time.ctime()} {' '.join(c if c != self.api_key else '***' for c in cmd)}\n".encode())
        log.flush()
        try:
            server.proc = await asyncio.to_thread(
                subprocess.Popen, cmd, stdout=log, stderr=subprocess.STDOUT, cwd=str(Path(plan["exe"]).parent),
                creationflags=NO_WINDOW)
        except OSError as exc:
            self.status = {"state": "failed", "model_id": model["id"], "error": f"Could not start llama-server: {exc}"}
            self._publish()
            raise RuntimeError_(self.status["error"]) from exc
        finally:
            log.close()
        deadline = time.monotonic() + 600
        async with httpx.AsyncClient(timeout=5, trust_env=False) as http:
            while time.monotonic() < deadline:
                if server.proc.poll() is not None:
                    tail = self.log_tail(20)
                    self.status = {"state": "failed", "model_id": model["id"], "error": self.friendly_error(tail)}
                    self._publish()
                    raise RuntimeError_(self.status["error"])
                try:
                    r = await http.get(f"{server.base_url}/health")
                    if r.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
            else:
                await self._stop_server(server)
                raise RuntimeError_("The model took more than 10 minutes to load")
        server.loaded_at = time.time()
        self.servers[model["id"]] = server
        self.status = {"state": "ready", "model_id": model["id"], "backend": plan["backend"],
                       "compute": plan["compute"], "ctx": plan["ctx"], "vision": bool(plan.get("mmproj"))}
        self._publish()
        self.db.update("models", model["id"], {"last_used": time.strftime("%Y-%m-%dT%H:%M:%S")})
        return server

    def _publish(self):
        self.bus.publish("model_status", **self.status, loaded=self.loaded())

    def loaded(self):
        return [{"model_id": s.model_id, "backend": s.plan["backend"], "compute": s.plan["compute"],
                 "ctx": s.plan["ctx"], "parallel": s.plan["parallel"], "users": s.users,
                 "vision": bool(s.plan.get("mmproj")), "memory_mb": s.need_bytes // 2**20}
                for s in self.servers.values()]

    def friendly_error(self, tail):
        low = tail.lower()
        if "out of memory" in low or "failed to allocate" in low or "unable to allocate" in low:
            return ("Not enough memory to load this model with these settings. Try CPU, a smaller context, "
                    "fewer simultaneous replies, or a smaller model.")
        if "unknown model architecture" in low or "unknown architecture" in low:
            return "This llama.cpp build does not support this model's architecture yet."
        if "clip_init" in low or ("mmproj" in low and "fail" in low):
            return ("The vision part (mmproj file) of this model could not be loaded. Turn vision off for this model "
                    "in its settings, or download the matching mmproj file.")
        if "vulkan" in low and "error" in low:
            return "The Vulkan GPU build failed to start on this graphics card. Switch this model to CPU."
        return "The model server stopped while loading. Details are in logs/llama-server.log. " + tail[-300:]

    def log_tail(self, lines=40):
        try:
            data = self.log_path.read_bytes()[-20000:].decode("utf-8", errors="replace")
            return "\n".join(data.splitlines()[-lines:])
        except OSError:
            return ""

    async def _stop_server(self, server):
        proc, server.proc = server.proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                await asyncio.to_thread(proc.wait, 15)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self.servers.get(server.model_id) is server:
            del self.servers[server.model_id]
        if self.last_server is server:
            self.last_server = None
        self.status = {"state": "idle"} if not self.servers else {"state": "ready", **self._summary()}
        self.bus.publish("model_status", **self.status, unloaded=server.model_id, loaded=self.loaded())

    def _summary(self):
        s = self.last_server or next(iter(self.servers.values()))
        return {"model_id": s.model_id, "backend": s.plan["backend"], "compute": s.plan["compute"], "ctx": s.plan["ctx"]}

    async def _stop(self):
        for server in list(self.servers.values()):
            await self._stop_server(server)

    # ------------------------------------------------------------------ leasing
    def _max_loaded(self):
        return max(1, int(self.db.setting("max_loaded_models", 3) or 1))

    async def _make_room(self, need):
        """Unload idle models (least recently used first) until the new one fits. True when it fits now."""
        idle = sorted([s for s in self.servers.values() if s.users == 0], key=lambda s: s.last_used)
        while True:
            fits_count = len(self.servers) < self._max_loaded()
            fits_memory = not self.servers or need <= self._free_bytes() * 0.9
            if fits_count and fits_memory:
                return True
            if not idle:
                return False
            await self._stop_server(idle.pop(0))

    @asynccontextmanager
    async def lease(self, model_id, compute=None, on_wait=None):
        """Use a model for the duration of a block; yields its Server. Loads it if needed (alongside the models
        already loaded when memory allows), otherwise waits for a busy model to finish."""
        model = self.db.get("models", model_id)
        if not model:
            raise RuntimeError_(f"Model {model_id!r} is not installed. Download it in Models.")
        async with self.cond:
            plan = self.plan(model, compute)
            waited = False
            while True:
                server = self.servers.get(model_id)
                if server and not server.alive:
                    await self._stop_server(server)
                    server = None
                if server and server.matches(plan):
                    break
                if server and server.users == 0:
                    await self._stop_server(server)      # same model, different settings: restart it
                    server = None
                if not server and await self._make_room(self._need_bytes(model, plan)):
                    try:
                        server = await self._start(model, plan)
                    except RuntimeError_:
                        if not plan.get("mmproj"):
                            raise
                        # the vision part does not load with this build or model: run without it, say so
                        settings = {**(model.get("settings") or {}), "vision": False,
                                    "vision_error": self.status.get("error") or "the vision part could not be loaded"}
                        self.db.update("models", model_id, {"settings": settings})
                        self.bus.publish("models_changed")
                        if on_wait:
                            on_wait("The model's vision part could not be loaded; continuing without vision")
                        plan = {**plan, "mmproj": None}
                        server = await self._start(model, plan)
                    break
                if on_wait and not waited:
                    busy = ", ".join(s.model_id for s in self.servers.values() if s.users) or "another task"
                    on_wait(f"Waiting for memory: {busy} is busy…")
                    waited = True
                await self.cond.wait()
            server.users += 1
            self.last_server = server
        token = ACTIVE.set(server)
        try:
            server.last_used = self.last_used = time.monotonic()
            yield server
        finally:
            server.users -= 1                 # first: nothing below may stop the model being released
            server.last_used = self.last_used = time.monotonic()
            try:
                ACTIVE.reset(token)
            except ValueError:                # released from another task's context
                ACTIVE.set(None)
            async with self.cond:
                self.cond.notify_all()

    def active(self):
        """The server leased by the current task, else the most recently used one."""
        server = ACTIVE.get()
        if server and server.alive:
            return server
        if self.last_server and self.last_server.alive:
            return self.last_server
        return next((s for s in self.servers.values() if s.alive), None)

    @property
    def users(self):
        return sum(s.users for s in self.servers.values())

    @property
    def current(self):
        s = self.active()
        return s.current if s else None

    @property
    def base_url(self):
        s = self.active()
        if not s:
            raise RuntimeError_("No model is loaded")
        return s.base_url

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.api_key}"}

    def is_loaded(self, model_id):
        return model_id in self.servers

    async def unload(self, model_id=None):
        async with self.cond:
            targets = [self.servers[model_id]] if model_id and model_id in self.servers else \
                list(self.servers.values()) if not model_id else []
            if any(s.users for s in targets):
                raise RuntimeError_("The model is in use by a chat or task right now")
            for s in targets:
                await self._stop_server(s)
            self.cond.notify_all()

    async def idle_watch(self):
        """Unload models after the configured idle time to give memory back."""
        while True:
            await asyncio.sleep(30)
            minutes = float(self.db.setting("unload_after_minutes", 20) or 0)
            if not minutes:
                continue
            async with self.cond:
                for s in list(self.servers.values()):
                    if s.users == 0 and time.monotonic() - s.last_used > minutes * 60:
                        await self._stop_server(s)
                self.cond.notify_all()

    async def shutdown(self):
        await self._stop()
