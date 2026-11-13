"""Talking to the local llama-server (OpenAI-compatible API): streaming replies, native tool calls, JSON-schema
answers (grammar-constrained, so always valid), reasoning ('thinking') split from the answer, and timings."""
import json

import httpx
from jsonschema import ValidationError, validate


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, runtime):
        self.runtime = runtime

    def _body(self, messages, *, tools=None, schema=None, max_tokens=1024, temperature=0.4, thinking=False,
              stream=False, tool_choice=None, stop=None):
        body = {"messages": messages, "max_tokens": max_tokens, "temperature": temperature, "stream": stream,
                "cache_prompt": True, "chat_template_kwargs": {"enable_thinking": bool(thinking)}}
        if stream:
            body["stream_options"] = {"include_usage": True}
        if tools:
            body["tools"] = tools
            if tool_choice:
                body["tool_choice"] = tool_choice
        if schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema,
                                                                              "strict": True}}
        if stop:
            body["stop"] = stop
        return body

    async def complete(self, messages, *, schema=None, tools=None, max_tokens=1024, temperature=0.2,
                       thinking=False, timeout=900):
        """One non-streamed call. With a schema, returns the parsed object in result['json'] (or raises)."""
        body = self._body(messages, tools=tools, schema=schema, max_tokens=max_tokens, temperature=temperature,
                          thinking=thinking)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as http:
            r = await http.post(self.runtime.base_url + "/v1/chat/completions", json=body,
                                headers=self.runtime.headers)
        if r.status_code != 200:
            raise LLMError(f"model server HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        out = {"content": msg.get("content") or "", "reasoning": msg.get("reasoning_content") or "",
               "tool_calls": msg.get("tool_calls") or [], "timings": data.get("timings") or {},
               "usage": data.get("usage") or {}, "finish_reason": choice.get("finish_reason")}
        if schema is not None:
            text = out["content"].strip()
            try:
                value = json.loads(text)
                validate(value, schema)
            except (json.JSONDecodeError, ValidationError) as exc:
                raise LLMError(f"model returned invalid JSON ({str(exc)[:160]})") from exc
            out["json"] = value
        return out

    async def json(self, messages, schema, *, max_tokens=512, retries=1, temperature=0.1):
        last = None
        for _ in range(retries + 1):
            try:
                return (await self.complete(messages, schema=schema, max_tokens=max_tokens,
                                            temperature=temperature))["json"]
            except LLMError as exc:
                last = exc
        raise last

    async def stream(self, messages, *, tools=None, max_tokens=2048, temperature=0.4, thinking=False, timeout=1800,
                     tool_choice=None):
        """Yields ('reasoning', text), ('token', text), then ('end', {...tool_calls, timings, finish_reason})."""
        body = self._body(messages, tools=tools, max_tokens=max_tokens, temperature=temperature, thinking=thinking,
                          stream=True, tool_choice=tool_choice)
        calls = {}
        timings, usage, finish = {}, {}, None
        content = []
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10), trust_env=False) as http:
            async with http.stream("POST", self.runtime.base_url + "/v1/chat/completions", json=body,
                                   headers=self.runtime.headers) as r:
                if r.status_code != 200:
                    text = (await r.aread()).decode("utf-8", errors="replace")
                    raise LLMError(f"model server HTTP {r.status_code}: {text[:300]}")
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        raise LLMError(str(chunk["error"])[:300])
                    timings = chunk.get("timings") or timings
                    usage = chunk.get("usage") or usage
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("reasoning_content"):
                            yield "reasoning", delta["reasoning_content"]
                        if delta.get("content"):
                            content.append(delta["content"])
                            yield "token", delta["content"]
                        for tc in delta.get("tool_calls") or []:
                            slot = calls.setdefault(tc.get("index", len(calls)),
                                                    {"id": tc.get("id"), "name": "", "arguments": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] += fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
                        if choice.get("finish_reason"):
                            finish = choice["finish_reason"]
        tool_calls = []
        for i, slot in sorted(calls.items()):
            try:
                args = json.loads(slot["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {"_unparsed": slot["arguments"]}
            tool_calls.append({"id": slot["id"] or f"call_{i}", "name": slot["name"], "arguments": args})
        yield "end", {"tool_calls": tool_calls, "timings": timings, "usage": usage, "finish_reason": finish,
                      "content": "".join(content)}


def estimate_tokens(text):
    """Rough token count without a tokenizer (good enough for budgeting)."""
    return int(len(text or "") / 3.6) + 4
