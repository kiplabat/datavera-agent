#!/usr/bin/env python3
"""A stand-in for llama-server for engine tests: same command-line flags and OpenAI-style endpoints, scripted
answers. Tool calls: if tools are offered and the last user message contains 'list files', it calls list_files
once; 'write' → write_file; then answers. JSON-schema requests get a minimal valid object."""
import argparse
import json
import sys
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

p = argparse.ArgumentParser()
p.add_argument("-m")
p.add_argument("--port", type=int)
p.add_argument("--host")
p.add_argument("--api-key")
p.add_argument("--alias")
p.add_argument("--list-devices", action="store_true")
args, _ = p.parse_known_args()
if args.list_devices:
    print("Available devices:\n  Vulkan0: Fake GPU (8192 MiB, 8000 MiB free)")
    sys.exit(0)
app = FastAPI()


def minimal(schema):
    if "oneOf" in schema:
        return minimal(schema["oneOf"][0])
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    t = schema.get("type")
    if t == "object":
        out = {}
        for k in schema.get("required", []):
            out[k] = minimal(schema["properties"][k])
        if "memories" in schema.get("properties", {}):
            out["memories"] = [{"scope": "global", "kind": "preference", "text": "Prefers short answers with tables."}]
        if "lessons" in schema.get("properties", {}):
            out["lessons"] = ["Narrow subjects find downloadable CSV files more often than broad ones."]
        if "steps" in schema.get("properties", {}):
            out["steps"] = [{"title": "Look at the folder", "detail": ""}, {"title": "Report", "detail": ""}]
        return out
    if t == "array":
        return []
    if t == "string":
        return "fake summary of the earlier conversation" if schema.get("maxLength", 0) > 1000 else "x" * max(schema.get("minLength", 1), 1)
    if t in ("integer", "number"):
        return 0
    if t == "boolean":
        return False
    return None


def decide(body):
    msgs = body["messages"]
    tools = {t["function"]["name"] for t in body.get("tools") or []}
    last_user = next((m for m in reversed(msgs) if m["role"] == "user"), {"content": ""})
    after_tool = msgs and msgs[-1]["role"] == "tool"
    text = last_user.get("content") or ""
    if tools and not after_tool:
        if "list files" in text and "list_files" in tools:
            return {"tool": ("list_files", {"path": "."})}
        if "write" in text and "write_file" in tools:
            return {"tool": ("write_file", {"path": "note.txt", "content": "hello"})}
        if "whoami" in text:
            name = next((n for n in tools if n.endswith("whoami")), None)
            if name:
                return {"tool": (name, {})}
    if after_tool:
        return {"text": f"Done. The tool said: {msgs[-1]['content'][:60]}"}
    return {"text": "Hello from the fake model. " + ("I remember: " + msgs[0]["content"][-80:] if "remembered" in msgs[0]["content"] else "")}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.middleware("http")
async def auth(request: Request, call_next):
    if request.url.path.startswith("/v1") and request.headers.get("authorization") != f"Bearer {args.api_key}":
        return JSONResponse({"error": "unauthorised"}, 401)
    return await call_next(request)


@app.get("/v1/models")
async def models():
    return {"data": [{"id": args.alias, "aliases": [args.alias]}]}


TIMINGS = {"prompt_n": 50, "prompt_ms": 100.0, "predicted_n": 10, "predicted_ms": 200.0,
           "prompt_per_second": 500.0, "predicted_per_second": 50.0, "cache_n": 0}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    rf = body.get("response_format")
    if rf:
        content = json.dumps(minimal(rf["json_schema"]["schema"]))
        return {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                "timings": TIMINGS}
    d = decide(body)
    if not body.get("stream"):
        msg = {"role": "assistant", "content": d.get("text", "")}
        if "tool" in d:
            msg["tool_calls"] = [{"id": "call_1", "type": "function", "function": {"name": d["tool"][0], "arguments": json.dumps(d["tool"][1])}}]
        return {"choices": [{"message": msg, "finish_reason": "stop"}], "timings": TIMINGS}

    def gen():
        def chunk(delta, finish=None):
            return "data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"
        yield chunk({"role": "assistant", "content": None})
        if body.get("chat_template_kwargs", {}).get("enable_thinking"):
            yield chunk({"reasoning_content": "thinking…"})
        if "tool" in d:
            name, a = d["tool"]
            yield chunk({"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": name, "arguments": ""}}]})
            yield chunk({"tool_calls": [{"index": 0, "function": {"arguments": json.dumps(a)}}]})
            yield chunk({}, "tool_calls")
        else:
            for word in d["text"].split(" "):
                yield chunk({"content": word + " "})
                time.sleep(0.005)
            yield chunk({}, "stop")
        yield "data: " + json.dumps({"choices": [], "timings": TIMINGS, "usage": {}}) + "\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
