"""Local model calls that return schema-checked JSON, with timing evidence for every call.

The model is only asked bounded questions: pick from enumerated options or write short text
from supplied facts. The JSON schema constrains decoding (Ollama `format`, or llama.cpp `response_format`
when the Datavera Agent desktop app runs the pipeline: DV_LLM_API=openai); jsonschema re-checks it.
"""
import json
import os
import time

import httpx
from jsonschema import ValidationError, validate


class LLMError(RuntimeError):
    pass


class LocalModel:
    def __init__(self, progress=None, log_path=None, base_url=None, model=None, num_ctx=None, timeout=420):
        self.api = os.getenv("DV_LLM_API", "ollama")
        if self.api == "openai":  # llama.cpp server started by the desktop app
            self.base_url = (base_url or os.getenv("LLM_BASE_URL", "http://127.0.0.1:8080")).rstrip("/")
            self.model = model or os.getenv("LLM_MODEL", "local")
            self.api_key = os.getenv("LLM_API_KEY", "")
            self.backend = "llama.cpp"
            self.start_hint = "Open the Datavera Agent app; it starts the model when a run begins."
        else:
            self.base_url = (base_url or os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")).rstrip("/")
            self.model = model or os.getenv("OLLAMA_MODEL", "qwen3.5:4b")
            self.backend = "Ollama"
            self.start_hint = "Start the Ollama app (CPU server on 11434) and retry."
        # Keep the context the server already loaded (8192) to avoid a model reload per call.
        self.num_ctx = int(num_ctx or os.getenv("OLLAMA_NUM_CTX", "8192"))
        self.timeout = timeout
        self.progress = progress
        self.log_path = log_path
        self.calls = 0

    def _log(self, record):
        if self.log_path:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    async def available(self):
        if self.api == "openai":
            async with httpx.AsyncClient(timeout=10, trust_env=False) as http:
                r = await http.get(self.base_url + "/v1/models", headers={"Authorization": f"Bearer {self.api_key}"})
                r.raise_for_status()
                names = set()
                for m in r.json().get("data", []):
                    names.add(m.get("id"))
                    names.update(m.get("aliases") or [])
            names.discard(None)
            return self.model in names, sorted(names), [{"name": self.model}]
        async with httpx.AsyncClient(timeout=10, trust_env=False) as http:
            tags = (await http.get(self.base_url + "/api/tags")).json()
            names = {m.get("name") for m in tags.get("models", [])}
            try:
                loaded = (await http.get(self.base_url + "/api/ps")).json().get("models", [])
            except Exception:
                loaded = []
        return self.model in names, sorted(n for n in names if n), loaded

    async def raw_chat_openai(self, messages, fmt=None, max_tokens=256):
        body = {"model": self.model, "stream": False, "messages": messages, "temperature": 0,
                "max_tokens": max_tokens, "cache_prompt": True, "chat_template_kwargs": {"enable_thinking": False}}
        if fmt is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": fmt}}
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as http:
            response = await http.post(self.base_url + "/v1/chat/completions", json=body,
                                       headers={"Authorization": f"Bearer {self.api_key}"})
        if response.status_code != 200:
            raise LLMError(f"llama.cpp HTTP {response.status_code}: {response.text[:300]}")
        data = response.json()
        t = data.get("timings") or {}
        metrics = {
            "wall_seconds": round(time.monotonic() - started, 2), "load_seconds": 0.0,
            "prompt_tokens": t.get("prompt_n"), "prompt_seconds": round((t.get("prompt_ms") or 0) / 1000, 2),
            "output_tokens": t.get("predicted_n"), "output_seconds": round((t.get("predicted_ms") or 0) / 1000, 2),
            "cached_tokens": t.get("cache_n"),
        }
        if metrics["prompt_tokens"] and metrics["prompt_seconds"]:
            metrics["prompt_tok_per_s"] = round(metrics["prompt_tokens"] / metrics["prompt_seconds"], 1)
        if metrics["output_tokens"] and metrics["output_seconds"]:
            metrics["output_tok_per_s"] = round(metrics["output_tokens"] / metrics["output_seconds"], 2)
        message = (data.get("choices") or [{}])[0].get("message") or {}
        return message.get("content") or "", metrics

    async def raw_chat(self, messages, fmt=None, max_tokens=256):
        if self.api == "openai":
            return await self.raw_chat_openai(messages, fmt, max_tokens)
        body = {"model": self.model, "stream": False, "think": False, "keep_alive": "30m",
                "messages": messages,
                "options": {"temperature": 0, "num_ctx": self.num_ctx, "num_predict": max_tokens}}
        if fmt is not None:
            body["format"] = fmt
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as http:
            response = await http.post(self.base_url + "/api/chat", json=body)
        if response.status_code != 200:
            raise LLMError(f"Ollama HTTP {response.status_code}: {response.text[:300]}")
        data = response.json()
        ns = 1e9
        metrics = {
            "wall_seconds": round(time.monotonic() - started, 2),
            "load_seconds": round(data.get("load_duration", 0) / ns, 2),
            "prompt_tokens": data.get("prompt_eval_count"),
            "prompt_seconds": round(data.get("prompt_eval_duration", 0) / ns, 2),
            "output_tokens": data.get("eval_count"),
            "output_seconds": round(data.get("eval_duration", 0) / ns, 2),
        }
        if metrics["prompt_tokens"] and metrics["prompt_seconds"]:
            metrics["prompt_tok_per_s"] = round(metrics["prompt_tokens"] / metrics["prompt_seconds"], 1)
        if metrics["output_tokens"] and metrics["output_seconds"]:
            metrics["output_tok_per_s"] = round(metrics["output_tokens"] / metrics["output_seconds"], 2)
        return data.get("message", {}).get("content", ""), metrics

    async def json(self, purpose, system, user, schema, max_tokens=256, retries=1):
        """Return a dict matching `schema`, or raise LLMError so the caller can use its fallback."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last_error = None
        for attempt in range(retries + 1):
            self.calls += 1
            call = self.calls
            if self.progress:
                self.progress.busy("model", f"call {call} ({purpose}), ~{len(system) + len(user)} prompt characters")
                self.progress.event("model", f"Call {call} started: {purpose}")
            try:
                text, metrics = await self.raw_chat(messages, schema, max_tokens)
            except (httpx.HTTPError, LLMError) as exc:
                last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                self._log({"call": call, "purpose": purpose, "error": last_error})
                if self.progress:
                    self.progress.event("model_error", f"Call {call} failed: {last_error}")
                    self.progress.idle()
                continue
            record = {"call": call, "purpose": purpose, "attempt": attempt + 1, **metrics, "output": text[:2000]}
            try:
                value = json.loads(text)
                validate(value, schema)
                record["valid"] = True
            except (json.JSONDecodeError, ValidationError) as exc:
                record["valid"] = False
                last_error = f"invalid output: {str(exc)[:200]}"
                value = None
            self._log(record)
            if self.progress:
                rate = metrics.get("prompt_tok_per_s")
                self.progress.event("model", f"Call {call} done in {metrics['wall_seconds']}s "
                                    f"(load {metrics['load_seconds']}s; {metrics['prompt_tokens']} prompt tokens"
                                    f"{f' at {rate}/s' if rate else ''}; {metrics['output_tokens']} output tokens)"
                                    + ("" if value is not None else f"; {last_error}"))
                self.progress.idle()
            if value is not None:
                return value
        raise LLMError(last_error or "model call failed")
