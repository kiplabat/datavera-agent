"""End-to-end engine checks against a running engine process (python -m dva serve) with stand-ins:
fake_llama_server.py replaces llama-server (scripted answers), fake_mcp_server.py is a stdio MCP connector.

    cd engine && python -m pytest -q tests/test_engine.py
Optional real-model check: set DVA_TEST_LLAMA_SERVER=/path/to/llama-server and DVA_TEST_GGUF=/path/to/model.gguf
"""
import io
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import httpx
import pytest

ENGINE = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent


def tiny_gguf(path, arch="llama", name="fake-model"):
    def s(text):
        b = text.encode()
        return struct.pack("<Q", len(b)) + b
    kvs = [("general.architecture", arch), ("general.name", name)]
    data = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0) + struct.pack("<Q", len(kvs))
    for k, v in kvs:
        data += s(k) + struct.pack("<I", 8) + s(v)
    Path(path).write_bytes(data + b"\0" * 1024)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def engine():
    tmp = Path(tempfile.mkdtemp(prefix="dva-test-"))
    wrapper = tmp / ("fake-llama-server.cmd" if os.name == "nt" else "fake-llama-server")
    if os.name == "nt":
        wrapper.write_text(f'@"{sys.executable}" "{TESTS / "fake_llama_server.py"}" %*\n')
    else:
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{TESTS / "fake_llama_server.py"}" "$@"\n')
        wrapper.chmod(0o755)
    port = free_port()
    token = "test-token"
    env = {**os.environ, "DVA_TOKEN": token, "DVA_LLAMA_SERVER": str(wrapper), "DVA_SECRETS": "file",
           "PYTHONPATH": str(ENGINE), "OLLAMA_MODELS": str(tmp / "ollama"), "HF_HOME": str(tmp / "hf")}
    log = tmp / "engine-output.log"
    proc = subprocess.Popen([sys.executable, "-m", "dva", "serve", "--port", str(port), "--data-dir", str(tmp / "data")],
                            cwd=str(ENGINE), env=env, stdout=log.open("w"), stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        if "DVA_READY" in log.read_text() or proc.poll() is not None:
            break
        time.sleep(0.1)
    assert "DVA_READY" in log.read_text(), log.read_text()[-3000:]
    print("engine log:", log)
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"Authorization": f"Bearer {token}"},
                          timeout=60, trust_env=False)
    yield {"client": client, "tmp": tmp, "port": port, "token": token}
    try:
        client.post("/api/shutdown")
    except httpx.HTTPError:
        pass
    proc.wait(10)


def wait_job(c, job_id, timeout=60, approve=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = c.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        if approve and job["status"] == "waiting":
            chat = c.get(f"/api/chats/{job['chat_id']}").json()
            for ap in chat["pending_approvals"]:
                c.post(f"/api/approvals/{ap['approval_id']}", json={"allow": approve == "allow"})
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} still {job['status']}")


def last_assistant(c, chat_id):
    msgs = c.get(f"/api/chats/{chat_id}").json()["messages"]
    return [m for m in msgs if m["role"] == "assistant"][-1]


def test_auth_and_bootstrap(engine):
    c = engine["client"]
    assert httpx.get(f"http://127.0.0.1:{engine['port']}/api/bootstrap", trust_env=False).status_code == 401
    boot = c.get("/api/bootstrap").json()
    assert boot["version"]
    ids = {h["id"] for h in boot["harnesses"]}
    assert {"chat", "agent", "planner", "researcher", "data-analyst", "datavera:dataset-publisher"} <= ids
    assert "custom" in boot["runtime"]["installed"]


def test_model_import_and_settings(engine):
    c, tmp = engine["client"], engine["tmp"]
    tiny_gguf(tmp / "fake-model-Q4_K_M.gguf")
    m = c.post("/api/models/import", json={"path": str(tmp / "fake-model-Q4_K_M.gguf")}).json()
    assert m["id"] == "fake-model-q4-k-m" and m["quant"] == "Q4_K_M"
    rows = c.get("/api/models").json()
    assert rows["default"] == m["id"] and rows["models"][0]["fit"]["cpu"] in ("fits", "tight", "too_big")
    upd = c.put(f"/api/models/{m['id']}", json={"compute": "cpu", "ctx": 4096}).json()
    assert upd["settings"]["compute"] == "cpu" and upd["settings"]["ctx"] == 4096


def test_agent_tool_loop_and_learning(engine):
    c, tmp = engine["client"], engine["tmp"]
    c.put("/api/settings", json={"learn_delay_seconds": 0})
    folder = tmp / "project-files"
    folder.mkdir(exist_ok=True)
    (folder / "data.csv").write_text("year,value\n2020,1\n2021,2\n")
    prj = c.post("/api/projects", json={"name": "Test project", "folders": [str(folder)],
                                        "instructions": "Answer briefly."}).json()
    chat = c.post("/api/chats", json={"project_id": prj["id"], "harness": "agent"}).json()
    job = c.post(f"/api/chats/{chat['id']}/messages", json={"text": "please list files"}).json()
    assert wait_job(c, job["job_id"])["status"] == "done"
    msg = last_assistant(c, chat["id"])
    tools = [s for s in msg["meta"]["steps"] if s["type"] == "tool"]
    assert tools and tools[0]["name"] == "list_files" and tools[0]["ok"] and "data.csv" in tools[0]["result"]
    assert msg["content"].startswith("Done.")
    # learning pass runs after the turn (delay 0) and stores a global preference
    for _ in range(50):
        mem = c.get("/api/memory").json()["memories"]
        if mem:
            break
        time.sleep(0.2)
    assert any("short answers" in m["text"] for m in mem)


def test_approval_flow(engine):
    c, tmp = engine["client"], engine["tmp"]
    folder = tmp / "project-files"
    chat = c.post("/api/chats", json={"harness": "agent", "folders": [str(folder)]}).json()
    job = c.post(f"/api/chats/{chat['id']}/messages", json={"text": "write a note"}).json()
    assert wait_job(c, job["job_id"], approve="deny")["status"] == "done"
    step = [s for s in last_assistant(c, chat["id"])["meta"]["steps"] if s["type"] == "tool"][0]
    assert step["declined"] and not (folder / "note.txt").exists()
    job = c.post(f"/api/chats/{chat['id']}/messages", json={"text": "write a note"}).json()
    assert wait_job(c, job["job_id"], approve="allow")["status"] == "done"
    assert (folder / "note.txt").read_text() == "hello"


def test_sandbox_blocks_outside_paths(engine):
    import asyncio
    sys.path.insert(0, str(ENGINE))
    from dva.tools import ToolContext, ToolDenied
    ctx = ToolContext(app=None, chat={"id": "x"}, project=None, workspace=engine["tmp"] / "ws",
                      folders=[str(engine["tmp"] / "project-files")], readonly=[], emit=lambda *a, **k: None,
                      approve=None)
    with pytest.raises(ToolDenied):
        ctx.resolve("/etc/passwd")
    with pytest.raises(ToolDenied):
        ctx.resolve("../../outside.txt", write=True)
    assert ctx.resolve("data.csv").name == "data.csv"


def test_chat_and_planner_harnesses(engine):
    c = engine["client"]
    chat = c.post("/api/chats", json={"harness": "chat"}).json()
    job = c.post(f"/api/chats/{chat['id']}/messages", json={"text": "hello"}).json()
    assert wait_job(c, job["job_id"])["status"] == "done"
    msg = last_assistant(c, chat["id"])
    assert "remember" in msg["content"].lower()  # memory reached the prompt
    chat = c.post("/api/chats", json={"harness": "planner"}).json()
    job = c.post(f"/api/chats/{chat['id']}/messages", json={"text": "tidy my notes"}).json()
    assert wait_job(c, job["job_id"])["status"] == "done"
    msg = last_assistant(c, chat["id"])
    assert len(msg["meta"]["plan"]) == 2 and all(s["state"] == "done" for s in msg["meta"]["plan"])


def test_skills_and_harnesses(engine):
    c = engine["client"]
    names = {s["name"] for s in c.get("/api/skills").json()["skills"]}
    assert {"skill-creator", "data-analysis", "datavera-publishing"} <= names
    s = c.post("/api/skills", json={"name": "Weekly Report", "description": "Use when writing the weekly report",
                                    "body": "1. Summarise\n2. List risks"}).json()
    assert s["id"] == "weekly-report" and s["editable"]
    raw = c.get("/api/skills/weekly-report/detail").json()["raw"]
    c.put("/api/skills/weekly-report/raw", json={"raw": raw.replace("List risks", "List risks and owners")})
    assert "owners" in c.get("/api/skills/weekly-report/detail").json()["body"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("my-skill/SKILL.md", "---\nname: zipped-skill\ndescription: Use when testing zips\n---\nDo it.")
        z.writestr("my-skill/helper.py", "print(1)")
    up = c.post("/api/skills-upload", files={"file": ("skill.zip", buf.getvalue())}).json()
    assert up["name"] == "zipped-skill" and "helper.py" in up["files"]
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("SKILL.md", "---\nname: evil\ndescription: x\n---\n")
        z.writestr("../escape.txt", "x")
    assert c.post("/api/skills-upload", files={"file": ("e.zip", bad.getvalue())}).status_code == 400
    h = c.post("/api/harnesses", json={"spec": {"name": "Note taker", "type": "loop", "mode": "agent",
                                                "tools": ["files"], "max_steps": 4},
                                       "prompt": "Take notes."}).json()
    assert h["id"] == "note-taker" and h["system_prompt"] == "Take notes."
    assert c.post("/api/harnesses", json={"spec": {"name": "x", "type": "loop", "max_steps": 999}}).status_code == 400


def test_schedules(engine):
    c = engine["client"]
    p = c.post("/api/schedules/parse", json={"when": "weekdays at 9:15"}).json()
    assert p["cron"] == "15 9 * * 1-5" and p["when"] == "Weekdays at 09:15"
    assert c.post("/api/schedules/parse", json={"when": "every 5 minutes"}).status_code == 400
    s = c.post("/api/schedules", json={"name": "Morning check", "when": "every day at 07:00", "prompt": "hello",
                                       "harness": "chat"}).json()
    assert s["cron"] == "0 7 * * *" and s["next_run"]
    run = c.post(f"/api/schedules/{s['id']}/run").json()
    assert wait_job(c, run["job_id"])["status"] == "done"
    chat = c.get(f"/api/chats/{run['chat_id']}").json()
    assert chat["origin"] == "schedule" and chat["title"].startswith("Morning check")


def test_connector_stdio(engine):
    c = engine["client"]
    conn = c.post("/api/connectors", json={"name": "Test MCP", "transport": "stdio", "command": "{python}",
                                           "args": [str(TESTS / "fake_mcp_server.py")]}).json()
    tested = c.post(f"/api/connectors/{conn['id']}/test").json()
    assert tested["ok"], tested
    names = {t["name"]: t for t in tested["tools"]}
    assert names["whoami"]["readonly"] and not names["set_value"]["readonly"]
    chat = c.post("/api/chats", json={"harness": "agent", "connectors": [conn["id"]]}).json()
    job = c.post(f"/api/chats/{chat['id']}/messages", json={"text": "whoami please"}).json()
    assert wait_job(c, job["job_id"])["status"] == "done"
    step = [s for s in last_assistant(c, chat["id"])["meta"]["steps"] if s["type"] == "tool"][0]
    assert step["ok"] and "tester" in step["result"]


def test_plugins(engine):
    c = engine["client"]
    plugins = {p["id"]: p for p in c.get("/api/plugins").json()["plugins"]}
    dv = plugins["datavera"]
    assert dv["enabled"] and not dv["configured"] and dv["connectors"][0]["url"].startswith("https://")
    c.put("/api/plugins/datavera/settings", json={"api_key": "not-a-key", "topics": "glaciers"})
    dv = {p["id"]: p for p in c.get("/api/plugins").json()["plugins"]}["datavera"]
    assert dv["values"]["api_key"] is True and dv["values"]["topics"] == "glaciers"  # secret never returned
    result = c.post("/api/plugins/datavera/setup").json()
    assert result["ok"] is False and "dv_sk_" in result["message"]
    created = c.post("/api/plugins-create", json={"name": "My kit", "description": "test",
                                                  "skills": ["weekly-report"], "harnesses": ["note-taker"]}).json()
    assert created["id"] == "my-kit"
    export = c.get("/api/plugins/my-kit/export")
    assert export.status_code == 200 and zipfile.ZipFile(io.BytesIO(export.content)).namelist()


def test_memory_api_and_secret_filter(engine):
    c = engine["client"]
    ok = c.post("/api/memory", json={"scope": "global", "text": "Works on Datavera"})
    assert ok.status_code == 200
    bad = c.post("/api/memory", json={"scope": "global", "text": "my key is dv_sk_abcdef123456"})
    assert bad.status_code == 400


def test_events_stream(engine):
    with httpx.stream("GET", f"http://127.0.0.1:{engine['port']}/api/events?token={engine['token']}&since=0",
                      timeout=10, trust_env=False) as r:
        kinds = set()
        for line in r.iter_lines():
            if line.startswith("data:"):
                kinds.add(json.loads(line[5:])["kind"])
            if {"chat", "job"} <= kinds:
                break
    assert {"chat", "job"} <= kinds


@pytest.mark.skipif(not os.environ.get("DVA_TEST_LLAMA_SERVER"), reason="real llama-server not configured")
def test_real_llama_server():
    tmp = Path(tempfile.mkdtemp(prefix="dva-real-"))
    port = free_port()
    env = {**os.environ, "DVA_TOKEN": "t", "DVA_LLAMA_SERVER": os.environ["DVA_TEST_LLAMA_SERVER"],
           "DVA_SECRETS": "file", "PYTHONPATH": str(ENGINE)}
    proc = subprocess.Popen([sys.executable, "-m", "dva", "serve", "--port", str(port), "--data-dir", str(tmp)],
                            cwd=str(ENGINE), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert proc.stdout.readline().startswith("DVA_READY")
    c = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"Authorization": "Bearer t"}, timeout=120,
                     trust_env=False)
    try:
        m = c.post("/api/models/import", json={"path": os.environ["DVA_TEST_GGUF"]}).json()
        c.put(f"/api/models/{m['id']}", json={"compute": "cpu", "ctx": 2048})
        c.put("/api/settings", json={"max_reply_tokens": 24, "learn_enabled": False})
        status = c.post(f"/api/models/{m['id']}/load", json={}).json()
        assert status["state"] == "ready" and status["compute"] == "cpu"
        chat = c.post("/api/chats", json={"harness": "chat", "model": m["id"]}).json()
        job = c.post(f"/api/chats/{chat['id']}/messages", json={"text": "hi"}).json()
        assert wait_job(c, job["job_id"], timeout=120)["status"] == "done"
        msg = last_assistant(c, chat["id"])
        assert msg["meta"]["stats"]["output_tokens"] > 0
        bench = c.post(f"/api/models/{m['id']}/benchmark").json()
        assert bench["reply_seconds"] > 0 and "json_valid" in bench
        r = c.post("/api/runtime/unload").json()
        assert r.get("ok"), r
    finally:
        c.post("/api/shutdown")
        proc.wait(10)


def test_pipeline_harness(engine):
    c = engine["client"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("echo/harness.json", json.dumps({
            "name": "Echo pipeline", "type": "pipeline", "entry": "run.py", "args": ["go"],
            "args_map": {"topic": "--topic"}, "inputs": {"type": "object", "properties": {"topic": {"type": "string"}}}}))
        z.writestr("echo/run.py", "import json, sys\nprint('::progress ' + json.dumps({'text': 'working'}))\n"
                                  "print('line one')\nprint('::result ' + json.dumps({'summary_markdown': 'Echo: ' + ' '.join(sys.argv[1:])}))\n")
    h = c.post("/api/harnesses-upload", files={"file": ("echo.zip", buf.getvalue())}).json()
    assert h["type"] == "pipeline"
    chat = c.post("/api/chats", json={"harness": h["id"]}).json()
    job = c.post(f"/api/chats/{chat['id']}/messages", json={"text": "honey bees"}).json()
    assert wait_job(c, job["job_id"])["status"] == "done"
    msg = last_assistant(c, chat["id"])
    assert msg["content"] == "Echo: go --topic honey bees"
    assert "line one" in c.get(f"/api/jobs/{job['job_id']}").json()["log"]


def test_datavera_pipeline_without_key(engine):
    c = engine["client"]
    c.put("/api/plugins/datavera/settings", json={"api_key": None})
    chat = c.post("/api/chats", json={"harness": "datavera:dataset-publisher"}).json()
    job = c.post(f"/api/chats/{chat['id']}/messages", json={"text": "run"}).json()
    done = wait_job(c, job["job_id"], timeout=90)
    msg = last_assistant(c, chat["id"])
    assert done["status"] == "done", done
    assert "Set up the Datavera plugin first" in json.dumps(msg["meta"]["steps"])


def test_finds_models_from_other_apps(engine):
    c, tmp = engine["client"], engine["tmp"]
    # an Ollama-style store: manifest name:tag -> blob without extension
    blob = tmp / "ollama" / "blobs" / ("sha256-" + "a" * 64)
    blob.parent.mkdir(parents=True)
    tiny_gguf(blob, name="Qwen3.5 4B")
    os.truncate(blob, 60 * 2**20)
    mf = tmp / "ollama" / "manifests" / "registry.ollama.ai" / "library" / "qwen3.5" / "4b"
    mf.parent.mkdir(parents=True)
    mf.write_text(json.dumps({"layers": [{"mediaType": "application/vnd.ollama.image.model", "digest": "sha256:" + "a" * 64}]}))
    # a folder the user added (LM Studio style), with a split model and a tiny vocab file that must be ignored
    lm = tmp / "lmstudio" / "lmstudio-community" / "gemma"
    lm.mkdir(parents=True)
    for name in ("gemma-3-4b-it-Q4_K_M.gguf", "big-Q8_0-00001-of-00002.gguf", "big-Q8_0-00002-of-00002.gguf", "vocab.gguf"):
        tiny_gguf(lm / name, name=name)
        if name != "vocab.gguf":
            os.truncate(lm / name, 60 * 2**20)
    c.put("/api/settings", json={"model_scan_folders": [str(tmp / "lmstudio")]})
    found = c.get("/api/models/local").json()["found"]
    names = {f["name"]: f for f in found}
    assert "qwen3.5:4b" in names and names["qwen3.5:4b"]["source"] == "Ollama"
    assert "gemma-3-4b-it-Q4_K_M" in names and "big-Q8_0-00001-of-00002" in names
    assert not any(n.startswith("big-Q8_0-00002") or "vocab" in n for n in names)
    m = c.post("/api/models/import", json={"path": names["qwen3.5:4b"]["path"], "name": "qwen3.5:4b"}).json()
    assert m["id"] == "qwen3.5-4b" and m["path"].endswith("a" * 64)
    again = c.post("/api/models/import", json={"path": names["qwen3.5:4b"]["path"], "name": "qwen3.5:4b"}).json()
    assert again["id"] == m["id"]  # no duplicates
    assert next(f for f in c.get("/api/models/local").json()["found"] if f["name"] == "qwen3.5:4b")["added_as"] == m["id"]
    c.delete(f"/api/models/{m['id']}")
    assert blob.exists()  # removing it from the app never deletes another app's file


# ---------------------------------------------------------------------- loop guard, follow-ups, teams, vision, web
def chat_messages(c, chat_id):
    return c.get(f"/api/chats/{chat_id}").json()["messages"]


def test_loop_guard_stops_a_looping_agent(engine):
    c, tmp = engine["client"], engine["tmp"]
    chat = c.post("/api/chats", json={"harness": "agent", "folders": [str(tmp / "project-files")]}).json()
    job = c.post(f"/api/chats/{chat['id']}/messages", json={"text": "keep looping please"}).json()
    assert wait_job(c, job["job_id"])["status"] == "done"
    msg = last_assistant(c, chat["id"])
    tools = [s for s in msg["meta"]["steps"] if s["type"] == "tool"]
    assert tools[0]["ok"] and all(s.get("blocked") for s in tools[1:]) and len(tools) <= 8
    assert "repeating" in msg["meta"]["guard"] or "nothing new" in msg["meta"]["guard"]
    assert any(s["type"] == "note" and "Stopped using tools" in s["text"] for s in msg["meta"]["steps"])


def test_queued_follow_up_runs_after_the_reply(engine):
    c = engine["client"]
    chat = c.post("/api/chats", json={"harness": "chat"}).json()
    first = c.post(f"/api/chats/{chat['id']}/messages", json={"text": "a slow reply please"}).json()
    time.sleep(0.6)
    queued = c.post(f"/api/chats/{chat['id']}/messages", json={"text": "and a second thing", "mode": "queue"}).json()
    assert queued["state"] == "queue"
    assert wait_job(c, first["job_id"])["status"] == "done"
    for _ in range(100):
        msgs = chat_messages(c, chat["id"])
        if len([m for m in msgs if m["role"] == "assistant"]) == 2 and not c.get(f"/api/chats/{chat['id']}").json()["running_job"]:
            break
        time.sleep(0.2)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[2]["content"] == "and a second thing" and not msgs[2]["meta"].get("pending")


def test_steer_reaches_the_running_agent(engine):
    c, tmp = engine["client"], engine["tmp"]
    chat = c.post("/api/chats", json={"harness": "agent", "folders": [str(tmp / "project-files")]}).json()
    job = c.post(f"/api/chats/{chat['id']}/messages", json={"text": "list files slowly"}).json()
    time.sleep(0.5)
    r = c.post(f"/api/chats/{chat['id']}/messages", json={"text": "only count the csv files", "mode": "steer"}).json()
    assert r["state"] == "steer"
    assert wait_job(c, job["job_id"])["status"] == "done"
    msg = last_assistant(c, chat["id"])
    assert any(s["type"] == "steer" for s in msg["meta"]["steps"])
    assert "only count the csv files" in msg["content"]
    time.sleep(0.5)
    assert len([m for m in chat_messages(c, chat["id"]) if m["role"] == "assistant"]) == 1  # not run twice


def test_stop_keeps_queued_messages_unsent(engine):
    c = engine["client"]
    chat = c.post("/api/chats", json={"harness": "chat"}).json()
    first = c.post(f"/api/chats/{chat['id']}/messages", json={"text": "slow again"}).json()
    time.sleep(0.4)
    q = c.post(f"/api/chats/{chat['id']}/messages", json={"text": "queued then stopped"}).json()
    c.post(f"/api/chats/{chat['id']}/stop")
    assert wait_job(c, first["job_id"])["status"] == "cancelled"
    time.sleep(0.5)
    row = next(m for m in chat_messages(c, chat["id"]) if m["id"] == q["message_id"])
    assert row["meta"].get("not_sent")
    again = c.post(f"/api/chats/{chat['id']}/messages/{q['message_id']}/resend").json()
    assert again["state"] == "started" and wait_job(c, again["job_id"])["status"] == "done"


def test_team_runs_agents_in_parallel(engine):
    c = engine["client"]
    chat = c.post("/api/chats", json={"harness": "chat"}).json()
    team = [{"name": "Alpha", "harness": "chat", "task": "say hello"},
            {"name": "Beta", "harness": "chat", "task": "say goodbye", "limits": {"max_reply_tokens": 300}}]
    job = c.post(f"/api/chats/{chat['id']}/messages", json={"text": "greet in two ways", "team": team}).json()
    assert wait_job(c, job["job_id"], timeout=90)["status"] == "done"
    full = c.get(f"/api/chats/{chat['id']}").json()
    assert {a["agent_name"] for a in full["agents"]} == {"Alpha", "Beta"}
    msg = last_assistant(c, chat["id"])
    team_step = next(s for s in msg["meta"]["steps"] if s["type"] == "team")
    assert all(a["status"] == "done" and a["content"] for a in team_step["agents"])
    beta = next(a for a in full["agents"] if a["agent_name"] == "Beta")
    assert c.get(f"/api/chats/{beta['id']}").json()["settings"]["max_reply_tokens"] == 300
    listed = {r["id"] for r in c.get("/api/chats").json()["chats"]}
    assert beta["id"] not in listed            # agents' chats stay out of the sidebar


def test_per_chat_limits_and_harnesses(engine):
    c = engine["client"]
    ids = {h["id"] for h in c.get("/api/harnesses").json()["harnesses"]}
    assert {"coder", "writer", "document-reader", "vision-analyst", "team-lead", "researcher"} <= ids
    chat = c.post("/api/chats", json={"harness": "chat"}).json()
    upd = c.put(f"/api/chats/{chat['id']}", json={"settings": {"max_reply_tokens": "512", "max_steps": 0,
                                                               "temperature": "0.1", "junk": 5}}).json()
    assert upd["settings"] == {"max_reply_tokens": 512, "temperature": 0.1}


def test_research_without_internet_says_so(engine):
    c = engine["client"]
    c.put("/api/settings", json={"browser_search": False})
    chat = c.post("/api/chats", json={"harness": "researcher"}).json()
    job = c.post(f"/api/chats/{chat['id']}/messages", json={"text": "electricity statistics for Kenya"}).json()
    assert wait_job(c, job["job_id"], timeout=120)["status"] == "done"
    msg = last_assistant(c, chat["id"])
    kinds = [s.get("name") for s in msg["meta"]["steps"] if s["type"] == "tool"]
    assert kinds and set(kinds) <= {"web_search", "web_fetch", "extract", "download_file"}
    assert any(s["type"] == "note" and "No relevant sources" in s["text"] for s in msg["meta"]["steps"])


def test_vision_attachment_reaches_vision_model(engine):
    c, tmp = engine["client"], engine["tmp"]
    from PIL import Image
    tiny_gguf(tmp / "eye-model-Q4_K_M.gguf", name="eye-model")
    tiny_gguf(tmp / "mmproj-eye-F16.gguf", arch="clip", name="eye-proj")
    m = c.post("/api/models/import", json={"path": str(tmp / "eye-model-Q4_K_M.gguf")}).json()
    assert m["settings"].get("mmproj", "").endswith("mmproj-eye-F16.gguf")   # found next to the model
    chat = c.post("/api/chats", json={"harness": "chat", "model": m["id"]}).json()
    img = tmp / "pic.png"
    Image.new("RGB", (40, 30), (200, 50, 50)).save(img)
    att = {"name": "pic.png", "path": str(img), "size": img.stat().st_size}
    job = c.post(f"/api/chats/{chat['id']}/messages", json={"text": "what is this?", "attachments": [att]}).json()
    assert wait_job(c, job["job_id"])["status"] == "done"
    assert "1 images" in last_assistant(c, chat["id"])["content"]
    log = (tmp / "data" / "logs" / "llama-server.log").read_text()
    assert "--mmproj" in log


def test_web_parsing_and_relevance_offline():
    sys.path.insert(0, str(ENGINE))
    import base64
    from dva.tools import web
    real = "https://www.epra.go.ke/statistics/energy-2024.pdf"
    ck = "https://www.bing.com/ck/a?!&&p=abc&u=a1" + base64.urlsafe_b64encode(real.encode()).decode().rstrip("=")
    assert web.unwrap(ck) == real
    assert web.unwrap("//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fa", "https://html.duckduckgo.com/html/") == "https://example.org/a"
    bing = f"""<ol><li class="b_algo"><h2><a href="{ck}">Kenya energy statistics 2024 (EPRA)</a></h2>
      <div class="b_caption"><p>Electricity generation and capacity in Kenya</p></div></li>
      <li class="b_algo"><h2><a href="https://outlook.live.com/">Outlook – free personal email</a></h2>
      <p>Sign in to Outlook</p></li></ol>"""
    rows = web.parse_bing(bing, "https://www.bing.com/search?q=x")
    terms = web.terms_of("Kenya electricity generation statistics 2024")
    kept = [r for r in rows if web.score(r, terms) > 0]
    assert [r["url"] for r in kept] == [real]
    ddg = """<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fkplc.co.ke%2Felectricity-kenya">
      Electricity in Kenya – KPLC</a><a class="result__snippet">Kenya generation mix</a></div>"""
    assert web.parse_ddg(ddg, "https://html.duckduckgo.com/html/")[0]["url"] == "https://kplc.co.ke/electricity-kenya"
    title, text, files = web.html_to_text('<html><title>T</title><body><main><h2>Data</h2><p>Table</p>'
                                          '<a href="/files/data.csv">CSV</a></main></body></html>', "https://x.org/p")
    assert "## Data" in text and files[0]["url"] == "https://x.org/files/data.csv"


def test_loop_guard_rules():
    sys.path.insert(0, str(ENGINE))
    from dva.runner import LoopGuard

    class DB:
        def setting(self, k, d=None):
            return d

    class T:
        limits, harness, db = {"max_searches": 3}, {}, DB()
    g = LoopGuard(T())
    assert g.check("web_search", {"query": "kenya electricity generation 2024"}) is None
    g.record("web_search", {"query": "kenya electricity generation 2024"}, True, "results A")
    assert "nearly the same" in g.check("web_search", {"query": "electricity generation kenya 2024 data"})
    assert "already called" in g.check("web_search", {"query": "kenya electricity generation 2024"})
    for q in ("solar farms", "wind power turbines"):
        g.record("web_search", {"query": q}, True, "NO RELEVANT RESULTS")
    assert "limit" in g.check("web_search", {"query": "geothermal output olkaria"})
    g.record("read_file", {"path": "a"}, False, "Error: x")
    assert g.nudge() and g.should_stop() is None
    for i in range(3):
        g.record("read_file", {"path": str(i)}, False, "Error: x")
    assert "nothing new" in g.should_stop()
