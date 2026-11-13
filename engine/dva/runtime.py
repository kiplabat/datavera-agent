"""llama.cpp runtime: which builds are installed (CPU / Vulkan / CUDA / Metal), installing them from the pinned
llama.cpp release, and running one `llama-server` for the model in use.

Only one model is loaded at a time (memory). A chat or task "leases" the model; a different model is loaded only
when nobody is using the current one. Models never leave this computer: the server listens on 127.0.0.1 only and
needs a random API key the engine generates."""
import asyncio
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


class Runtime:
    def __init__(self, paths, db, bus):
        self.paths, self.db, self.bus = paths, db, bus
        self.proc = None
        self.port = None
        self.api_key = secrets.token_urlsafe(24)
        self.current = None          # {"model_id", "backend", "compute", "ctx", ...}
        self.users = 0
        self.cond = asyncio.Condition()
        self.last_used = time.monotonic()
        self.status = {"state": "idle"}
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
            rel = await fetch_json(f"https://api.github.com/repos/{VERSIONS['llama_cpp_repo']}/releases/tags/{self.tag}")
            assets = rel.get("assets", [])

            def pick(pats, exclude=None):
                for a in assets:
                    name = a["name"]
                    if all(p in name for p in pats) and (name.endswith(".zip") or name.endswith(".tar.gz")) \
                            and not (exclude and name.startswith(exclude)):
                        return a
                return None
            wanted = [pick(patterns[backend], exclude="cudart")]
            if backend == "cuda" and "cuda_runtime" in patterns:
                wanted.append(pick(patterns["cuda_runtime"]))
            if not all(wanted):
                raise RuntimeError_(f"llama.cpp {self.tag} has no {backend} download for this computer")
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
        if self.current and self.current.get("backend") == backend:
            raise RuntimeError_("That build is running the current model; unload the model first")
        shutil.rmtree(self.backend_dir(backend), ignore_errors=True)
        self._devices_cache.pop(backend, None)

    def info(self):
        installed = self.backends()
        return {
            "tag": self.tag, "installed": {k: str(v) for k, v in installed.items()},
            "available": self.available_backends(), "labels": BACKEND_LABELS,
            "devices": {b: self.list_devices(b) for b in installed if b not in ("cpu",)},
            "installing": self.installing, "status": self.status, "current": self.current,
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
        return {"backend": backend, "exe": str(installed[backend]), "compute": compute, "ctx": ctx,
                "threads": int(threads), "gpu_layers": settings.get("gpu_layers", "auto"),
                "parallel": int(settings.get("parallel") or 1)}

    # ------------------------------------------------------------------ process
    def _command(self, model, plan):
        cmd = [plan["exe"], "-m", model["path"], "--host", "127.0.0.1", "--port", str(self.port),
               "--api-key", self.api_key, "-c", str(plan["ctx"]), "-t", str(plan["threads"]),
               "-np", str(plan["parallel"]), "--jinja", "--reasoning-format", "deepseek", "--no-webui",
               "--alias", model["id"], "--cache-reuse", "256"]
        if plan["compute"] == "cpu":
            cmd += ["--device", "none", "-ngl", "0"]
        else:
            cmd += ["-ngl", str(plan["gpu_layers"] or "auto")]
        mmproj = (model.get("settings") or {}).get("mmproj")
        if mmproj and Path(mmproj).exists():
            cmd += ["--mmproj", mmproj]
        extra = (model.get("settings") or {}).get("extra_args")
        if extra:
            cmd += [a for a in extra.split() if a]
        return cmd

    async def _start(self, model, plan):
        await self._stop()
        self.port = free_port()
        cmd = self._command(model, plan)
        if not Path(model["path"]).exists():
            raise RuntimeError_(f"The model file is missing: {model['path']}")
        self.status = {"state": "loading", "model_id": model["id"], "backend": plan["backend"], "since": time.time()}
        self.bus.publish("model_status", **self.status)
        log = self.log_path.open("ab")
        log.write(f"\n==== {time.ctime()} {' '.join(c if c != self.api_key else '***' for c in cmd)}\n".encode())
        log.flush()
        try:
            self.proc = await asyncio.to_thread(
                subprocess.Popen, cmd, stdout=log, stderr=subprocess.STDOUT, cwd=str(Path(plan["exe"]).parent),
                creationflags=NO_WINDOW)
        except OSError as exc:
            raise RuntimeError_(f"Could not start llama-server: {exc}") from exc
        finally:
            log.close()
        deadline = time.monotonic() + 600
        async with httpx.AsyncClient(timeout=5, trust_env=False) as http:
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    tail = self.log_tail(20)
                    self.status = {"state": "failed", "model_id": model["id"], "error": self.friendly_error(tail)}
                    self.bus.publish("model_status", **self.status)
                    raise RuntimeError_(self.status["error"])
                try:
                    r = await http.get(f"http://127.0.0.1:{self.port}/health")
                    if r.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
            else:
                await self._stop()
                raise RuntimeError_("The model took more than 10 minutes to load")
        self.current = {"model_id": model["id"], **{k: v for k, v in plan.items() if k != "exe"},
                        "port": self.port, "loaded_at": time.time()}
        self.status = {"state": "ready", "model_id": model["id"], "backend": plan["backend"],
                       "compute": plan["compute"], "ctx": plan["ctx"]}
        self.bus.publish("model_status", **self.status)
        self.db.update("models", model["id"], {"last_used": time.strftime("%Y-%m-%dT%H:%M:%S")})

    def friendly_error(self, tail):
        low = tail.lower()
        if "out of memory" in low or "failed to allocate" in low or "unable to allocate" in low:
            return ("Not enough memory to load this model with these settings. Try CPU, a smaller context, "
                    "or a smaller model.")
        if "unknown model architecture" in low or "unknown architecture" in low:
            return "This llama.cpp build does not support this model's architecture yet."
        if "vulkan" in low and "error" in low:
            return "The Vulkan GPU build failed to start on this graphics card. Switch this model to CPU."
        return "The model server stopped while loading. Details are in logs/llama-server.log. " + tail[-300:]

    def log_tail(self, lines=40):
        try:
            data = self.log_path.read_bytes()[-20000:].decode("utf-8", errors="replace")
            return "\n".join(data.splitlines()[-lines:])
        except OSError:
            return ""

    async def _stop(self):
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                await asyncio.to_thread(proc.wait, 15)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self.current:
            self.bus.publish("model_status", state="idle", model_id=None)
        self.current = None
        self.status = {"state": "idle"}

    # ------------------------------------------------------------------ leasing
    @asynccontextmanager
    async def lease(self, model_id, compute=None, on_wait=None):
        """Use a model for the duration of a block. Loads/switches it if needed; waits while another model is busy."""
        model = self.db.get("models", model_id)
        if not model:
            raise RuntimeError_(f"Model {model_id!r} is not installed. Download it in Models.")
        async with self.cond:
            plan = self.plan(model, compute)
            while True:
                same = self.current and self.current["model_id"] == model_id and \
                    self.current["compute"] == plan["compute"] and self.current["ctx"] == plan["ctx"] and \
                    self.proc and self.proc.poll() is None
                if same:
                    break
                if self.users == 0:
                    await self._start(model, plan)
                    break
                if on_wait:
                    on_wait(f"Waiting for {self.current['model_id']} to finish another task…")
                await self.cond.wait()
            self.users += 1
        try:
            self.last_used = time.monotonic()
            yield self
        finally:
            async with self.cond:
                self.users -= 1
                self.last_used = time.monotonic()
                self.cond.notify_all()

    async def unload(self):
        async with self.cond:
            if self.users:
                raise RuntimeError_("The model is in use by a chat or task right now")
            await self._stop()
            self.cond.notify_all()

    async def idle_watch(self):
        """Unload the model after the configured idle time to give memory back."""
        while True:
            await asyncio.sleep(30)
            minutes = float(self.db.setting("unload_after_minutes", 20) or 0)
            if minutes and self.current and self.users == 0 and time.monotonic() - self.last_used > minutes * 60:
                async with self.cond:
                    if self.users == 0:
                        await self._stop()

    async def shutdown(self):
        await self._stop()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.api_key}"}
