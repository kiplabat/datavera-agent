"""Models: a short recommended list, search across Hugging Face GGUF repositories, downloads (resumable, checked),
importing a GGUF already on disk, per-model settings (CPU/GPU, context, threads) and "will it fit" estimates."""
import asyncio
import re
import shutil
import time
from pathlib import Path
from urllib.parse import quote

from . import gguf
from .db import now
from .download import DownloadCancelled, download, fetch_json

HF = "https://huggingface.co"

# Recommended models. Files are resolved from the repository listing at download time (publishers rename
# files), by quantisation label. `min_ram_gb` is total system memory for comfortable CPU use.
CATALOG = [
    {"key": "qwen3.5-4b", "name": "Qwen3.5 4B", "repo": "unsloth/Qwen3.5-4B-GGUF", "quant": "Q4_K_M",
     "approx_gb": 2.8, "min_ram_gb": 8, "tools": True, "default": True,
     "about": "Best all-rounder for laptops. Good at tools and following instructions. The proven default."},
    {"key": "qwen3.5-2b", "name": "Qwen3.5 2B", "repo": "unsloth/Qwen3.5-2B-GGUF", "quant": "Q4_K_M",
     "approx_gb": 1.4, "min_ram_gb": 6, "tools": True,
     "about": "Faster on older computers; simpler answers."},
    {"key": "qwen3.5-0.8b", "name": "Qwen3.5 0.8B", "repo": "unsloth/Qwen3.5-0.8B-GGUF", "quant": "Q8_0",
     "approx_gb": 0.9, "min_ram_gb": 4, "tools": False,
     "about": "Tiny and very fast. Good for quick wording tasks and memory learning; weak at tools."},
    {"key": "qwen3.5-9b", "name": "Qwen3.5 9B", "repo": "unsloth/Qwen3.5-9B-GGUF", "quant": "Q4_K_M",
     "approx_gb": 5.7, "min_ram_gb": 16, "tools": True,
     "about": "Noticeably smarter; slower on CPU. Great with a graphics card that has 8 GB or more."},
    {"key": "qwen3.5-35b-a3b", "name": "Qwen3.5 35B-A3B (MoE)", "repo": "unsloth/Qwen3.5-35B-A3B-GGUF",
     "quant": "Q4_K_M", "approx_gb": 21, "min_ram_gb": 32, "tools": True,
     "about": "Large 'mixture of experts' model: only ~3B parameters work per word, so it is usable on a "
              "32 GB computer even without a graphics card, at much higher quality."},
    {"key": "qwen3.5-27b", "name": "Qwen3.5 27B", "repo": "unsloth/Qwen3.5-27B-GGUF", "quant": "Q4_K_M",
     "approx_gb": 16.5, "min_ram_gb": 32, "tools": True,
     "about": "High quality; needs a strong graphics card (16 GB+) to be pleasant."},
    {"key": "gemma-3-4b", "name": "Gemma 3 4B", "repo": "ggml-org/gemma-3-4b-it-GGUF", "quant": "Q4_K_M",
     "approx_gb": 2.5, "min_ram_gb": 8, "tools": False,
     "about": "Google's small model; fluent writing, weaker at tool use."},
    {"key": "llama-3.2-3b", "name": "Llama 3.2 3B", "repo": "bartowski/Llama-3.2-3B-Instruct-GGUF",
     "quant": "Q4_K_M", "approx_gb": 2.0, "min_ram_gb": 8, "tools": True,
     "about": "Meta's small model; quick, decent tools."},
    {"key": "gpt-oss-20b", "name": "gpt-oss 20B", "repo": "ggml-org/gpt-oss-20b-GGUF", "quant": "mxfp4",
     "approx_gb": 12.1, "min_ram_gb": 24, "tools": True,
     "about": "OpenAI's open-weight reasoning model (MoE). Strong reasoning; needs 16 GB+ free memory."},
]

QUANT_RE = re.compile(r"(?i)(?:^|[-_.])((?:UD-)?(?:IQ\d_[A-Z0-9_]+|Q\d(?:_[A-Z0-9]+)*|BF16|F16|F32|MXFP4(?:_MOE)?))"
                      r"(?:[-_.]|$)")
SPLIT_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$")


def slug(text):
    return re.sub(r"[^a-z0-9.]+", "-", text.lower()).strip("-")[:80]


def quant_of(filename):
    m = QUANT_RE.search(Path(filename).stem)
    return m.group(1).upper() if m else "?"


class Models:
    def __init__(self, paths, db, bus, runtime):
        self.paths, self.db, self.bus, self.runtime = paths, db, bus, runtime
        self.downloads = {}  # id -> state
        self._cancel = {}

    def _hf_headers(self):
        from .app_state import STATE
        token = STATE.secrets.get("hf_token") if STATE else None
        return {"Authorization": f"Bearer {token}"} if token else {}

    # ------------------------------------------------------------------ registry
    def installed(self):
        rows = self.db.all("SELECT * FROM models ORDER BY COALESCE(last_used, added) DESC")
        for r in rows:
            r["exists"] = bool(r["path"]) and Path(r["path"]).exists()
        return rows

    def default_model(self):
        chosen = self.db.setting("default_model")
        if chosen and self.db.get("models", chosen):
            return chosen
        rows = self.installed()
        return rows[0]["id"] if rows else None

    def update_settings(self, model_id, values):
        row = self.db.get("models", model_id)
        if not row:
            raise KeyError(model_id)
        settings = {**(row["settings"] or {}), **values}
        settings = {k: v for k, v in settings.items() if v not in (None, "")}
        self.db.update("models", model_id, {"settings": settings})
        return self.db.get("models", model_id)

    def delete(self, model_id):
        row = self.db.get("models", model_id)
        if not row:
            return
        if self.runtime.current and self.runtime.current.get("model_id") == model_id:
            raise RuntimeError("Unload the model before deleting it")
        managed = self.paths.models_dir(self._settings())
        for f in row.get("files") or [row["path"]]:
            p = Path(f)
            if p.exists() and managed in p.parents:  # never delete files the user imported from elsewhere
                p.unlink()
        folder = Path(row["path"]).parent if row["path"] else None
        if folder and managed in folder.parents and folder.exists() and not any(folder.iterdir()):
            folder.rmdir()
        self.db.delete("models", model_id)

    def _settings(self):
        return {"models_dir": self.db.setting("models_dir")}

    def register(self, path, repo=None, name=None, files=None, sha256=None, catalog_key=None):
        path = Path(path)
        try:
            meta = gguf.read_metadata(path)
        except Exception as exc:
            raise ValueError(f"That file is not a usable GGUF model ({exc})") from exc
        files = files or [str(path)]
        size = sum(Path(f).stat().st_size for f in files if Path(f).exists())
        model_id = slug(catalog_key or (Path(path).name.replace(".gguf", "")))
        base, n = model_id, 2
        while self.db.get("models", model_id):
            existing = self.db.get("models", model_id)
            if existing["path"] == str(path):
                return existing
            model_id, n = f"{base}-{n}", n + 1
        thinking = meta.get("template_supports_thinking")
        self.db.insert("models", {
            "id": model_id, "name": name or meta.get("name") or path.stem, "repo": repo or "", "files": files,
            "path": str(path), "size": size, "sha256": sha256 or "", "quant": quant_of(path.name), "meta": meta,
            "settings": {"compute": "auto", "thinking_supported": bool(thinking)}, "added": now()})
        self.bus.publish("models_changed")
        if not self.db.setting("default_model"):
            self.db.set_setting("default_model", model_id)
        return self.db.get("models", model_id)

    # ------------------------------------------------------------------ fit
    def fit(self, size_bytes, meta=None, ctx=None, hw=None):
        """'fits' | 'tight' | 'too_big' for CPU, and for the best GPU if any."""
        from .hardware import snapshot
        hw = hw or snapshot()
        ctx = ctx or int(self.db.setting("default_ctx", 8192))
        kv = gguf.kv_cache_bytes(meta, ctx) if meta else None
        if kv is None:
            kv = max(0.35, size_bytes / 2**30 * 0.12) * (ctx / 8192) * 2**30
        need = size_bytes + kv + 0.5 * 2**30
        ram = hw["ram_mb"] * 2**20
        cpu = "fits" if need <= ram * 0.6 else ("tight" if need <= ram * 0.85 else "too_big")
        vram = max([g.get("vram_mb", 0) for g in hw["gpus"] if not g.get("integrated")] or [0]) * 2**20
        gpu = None
        if vram:
            gpu = "fits" if need <= vram * 0.92 else ("partial" if need <= vram + ram * 0.6 else "too_big")
        return {"need_mb": int(need / 2**20), "cpu": cpu, "gpu": gpu}

    # ------------------------------------------------------------------ Hugging Face
    async def catalog(self):
        from .hardware import snapshot
        hw = await asyncio.to_thread(snapshot)
        have = {r["repo"]: r["id"] for r in self.installed() if r["repo"]}
        out = []
        for item in CATALOG:
            entry = dict(item)
            entry["fit"] = self.fit(int(item["approx_gb"] * 2**30), None, None, hw)
            entry["installed"] = have.get(item["repo"])
            out.append(entry)
        return {"hardware": hw, "models": out}

    async def search(self, query, limit=30):
        url = f"{HF}/api/models?search={quote(query)}&filter=gguf&sort=downloads&direction=-1&limit={limit}"
        rows = await fetch_json(url, self._hf_headers())
        return [{"repo": r.get("id") or r.get("modelId"), "downloads": r.get("downloads"), "likes": r.get("likes"),
                 "updated": r.get("lastModified"), "gated": r.get("gated", False)} for r in rows]

    async def repo_files(self, repo):
        """GGUF files in a repository, grouped so split models (-00001-of-00003) appear once."""
        tree = await fetch_json(f"{HF}/api/models/{repo}/tree/main?recursive=true", self._hf_headers())
        groups = {}
        for f in tree:
            path = f.get("path", "")
            if f.get("type") != "file" or not path.endswith(".gguf"):
                continue
            name = Path(path).name
            if "mmproj" in name.lower():
                continue
            size = (f.get("lfs") or {}).get("size") or f.get("size") or 0
            sha = (f.get("lfs") or {}).get("oid")
            m = SPLIT_RE.search(name)
            key = SPLIT_RE.sub(".gguf", path) if m else path
            g = groups.setdefault(key, {"file": key, "quant": quant_of(name), "size": 0, "parts": []})
            g["parts"].append({"path": path, "size": size, "sha256": sha})
            g["size"] += size
        mmproj = [f.get("path") for f in tree if f.get("path", "").endswith(".gguf") and "mmproj" in f.get("path", "").lower()]
        from .hardware import snapshot
        hw = await asyncio.to_thread(snapshot)
        files = sorted(groups.values(), key=lambda g: g["size"])
        for g in files:
            g["parts"].sort(key=lambda p: p["path"])
            g["fit"] = self.fit(g["size"], None, None, hw)
        return {"repo": repo, "files": files, "mmproj": mmproj}

    async def resolve_catalog(self, key):
        item = next((c for c in CATALOG if c["key"] == key), None)
        if not item:
            raise KeyError(key)
        listing = await self.repo_files(item["repo"])
        want = item["quant"].upper()
        match = next((f for f in listing["files"] if f["quant"] == want), None) or \
            next((f for f in listing["files"] if want in f["file"].upper()), None)
        if not match:
            raise ValueError(f"{item['repo']} no longer has a {item['quant']} file")
        return item, match

    # ------------------------------------------------------------------ downloads
    def start_download(self, repo, file_group, name=None, catalog_key=None):
        dl_id = slug(f"{repo}-{Path(file_group['file']).stem}")
        if dl_id in self.downloads and self.downloads[dl_id]["state"] in ("queued", "downloading", "verifying"):
            return dl_id
        state = {"id": dl_id, "repo": repo, "file": file_group["file"], "name": name or Path(file_group["file"]).stem,
                 "total": file_group["size"], "done": 0, "rate": 0, "state": "queued", "error": None,
                 "started": time.time()}
        self.downloads[dl_id] = state
        cancel = asyncio.Event()
        self._cancel[dl_id] = cancel
        asyncio.create_task(self._download(dl_id, repo, file_group, cancel, catalog_key))
        self.bus.publish("download", **state)
        return dl_id

    async def _download(self, dl_id, repo, group, cancel, catalog_key):
        state = self.downloads[dl_id]
        folder = self.paths.models_dir(self._settings()) / repo.replace("/", "__")
        disk = shutil.disk_usage(folder)
        if disk.free < group["size"] * 1.05:
            state.update(state="failed", error=f"Not enough free disk space ({disk.free // 2**30} GB free, "
                                               f"{group['size'] // 2**30 + 1} GB needed). Move the models folder in Settings.")
            self.bus.publish("download", **state)
            return
        state["state"] = "downloading"
        done_before = 0
        paths = []
        try:
            for part in group["parts"]:
                dest = folder / Path(part["path"]).name

                def progress(done, total, rate, base=done_before):
                    state.update(done=base + done, rate=rate)
                    self.bus.publish("download", **state)
                if not (dest.exists() and dest.stat().st_size == part["size"]):
                    url = f"{HF}/{repo}/resolve/main/{quote(part['path'])}?download=true"
                    await download(url, dest, headers=self._hf_headers(), expected_size=part["size"] or None,
                                   sha256=part.get("sha256"), progress=progress, cancel=cancel)
                done_before += part["size"]
                paths.append(str(dest))
            state.update(state="verifying", done=group["size"])
            self.bus.publish("download", **state)
            row = await asyncio.to_thread(self.register, paths[0], repo, state["name"], paths,
                                          group["parts"][0].get("sha256"), catalog_key)
            state.update(state="done", model_id=row["id"])
        except DownloadCancelled:
            state.update(state="cancelled")
        except Exception as exc:
            state.update(state="failed", error=f"{type(exc).__name__}: {str(exc)[:300]}")
        finally:
            self._cancel.pop(dl_id, None)
            self.bus.publish("download", **state)

    def cancel_download(self, dl_id):
        if dl_id in self._cancel:
            self._cancel[dl_id].set()
        elif dl_id in self.downloads:
            self.downloads.pop(dl_id)

    def import_file(self, path):
        path = Path(path)
        if not path.exists() or path.suffix.lower() != ".gguf":
            raise ValueError("Choose a .gguf model file")
        return self.register(path)

    # ------------------------------------------------------------------ benchmark
    async def benchmark(self, model_id, llm):
        """Two short calls: a plain reply and a JSON-schema answer. Records prompt/generation speed."""
        results = {}
        async with self.runtime.lease(model_id):
            t0 = time.monotonic()
            r1 = await llm.complete([{"role": "user", "content": "In one sentence, what is a dataset?"}],
                                    max_tokens=60)
            results["reply_seconds"] = round(time.monotonic() - t0, 2)
            results.update({k: r1["timings"].get(k) for k in ("prompt_per_second", "predicted_per_second")})
            schema = {"type": "object", "properties": {"choice": {"type": "string", "enum": ["sum", "mean"]}},
                      "required": ["choice"]}
            t0 = time.monotonic()
            r2 = await llm.complete([{"role": "user", "content": "Airport passengers per month: should yearly "
                                      "totals use sum or mean? Answer as JSON."}], schema=schema, max_tokens=30)
            results["json_seconds"] = round(time.monotonic() - t0, 2)
            results["json_valid"] = isinstance(r2.get("json"), dict)
        results["at"] = now()
        results["backend"] = self.runtime.current.get("backend") if self.runtime.current else None
        results["compute"] = self.runtime.current.get("compute") if self.runtime.current else None
        self.db.update("models", model_id, {"bench": results})
        return results
