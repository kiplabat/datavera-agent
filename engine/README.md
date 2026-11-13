# Datavera Agent engine — technical reference

Python 3.12, FastAPI on `127.0.0.1` (token-protected), SQLite, llama.cpp. Started by the desktop app
(`python -m dva serve --data-dir … --resources …` with `DVA_TOKEN` in the environment).

## Modules (`dva/`)

| Module | Role |
|---|---|
| `server.py` | HTTP API + server-sent events (`/api/events`) + serves the built UI |
| `app_state.py` | Wires the services; `start_turn()` saves the user message and starts the turn as a task; `post_message()` handles follow-ups while a chat is busy (`queue` → run right after, `steer` → handed to the running agent at its next step; after Stop they stay unsent); per-chat limits (`chats.settings`) |
| `runner.py` | One chat turn: builds the prompt (harness + project instructions + memory + skills index + summary), runs the harness (chat / agent / planner / research / team loop, or a pipeline), streams events, saves one assistant message with ordered `meta.steps`. `LoopGuard` stops loops in every tool-using turn: exact and near-duplicate calls are refused (searches: ≥60 % word overlap), per-tool caps (web_search 6 by default), a "change approach" note after 3 unproductive results, a forced write-up after 6 (or 5 refused repeats, or the time limit) |
| `llm.py` | OpenAI-compatible client for llama-server: streaming, native tool calls, JSON-schema answers, reasoning split. Calls go to the server leased by the current task (context variable) unless `server=` is given |
| `runtime.py` | llama.cpp builds (bundled CPU; Vulkan/CUDA downloaded from the pinned release), **several `llama-server`s at once** (one per model, up to `max_loaded_models`, memory-checked; idle ones unloaded LRU-first), model *leases*, "simultaneous replies" (`-np`, context × slots), vision projector (`--mmproj`, `--no-mmproj-offload` on CPU; falls back to no vision if it does not load), idle unload, CPU/GPU plans |
| `models.py` | Recommended catalogue, **browse ranked for this computer** (HF `expand[]=gguf` parameter counts → size at Q4, fit, rough speed; trusted publishers filter), Hugging Face search and file listing with a recommended quant, downloads with phases, stall detection (45 s), 8 retries with backoff, readable errors, persisted across restarts (paused → Resume), vision parts (mmproj F16 preferred) with the model or added later, GGUF import (sibling or Ollama projector picked up), fit estimates, speed test |
| `media.py` | Images (resized JPEG data URLs) and video frames (PyAV, evenly spaced with timestamps) for vision models |
| `hardware.py` | CPU, RAM, GPUs and VRAM (nvidia-smi, Windows registry `qwMemorySize`), power state, backend recommendation |
| `gguf.py` | Reads GGUF headers (architecture, layers, context, template capabilities) |
| `tools/` | Built-in tools: files (sandboxed to granted folders + workspace; deletes go to `.trash`, edits keep `.backups`; reads PDF, Word, PowerPoint, Excel), web (DuckDuckGo html/lite, Mojeek, Bing with redirect decoding; results scored against the query's words so generic bot-check pages are dropped; real-browser fallback; explicit "NO RELEVANT RESULTS"; PDF pages read as text), `run_python`, memory, skills, schedules/harnesses, vision (`look_at_image`, `look_at_video`), `delegate` (helper agents in parallel) |
| `mcp.py` | Connectors: one long-lived MCP session per connector in its own task; tool lists cached; approval by `readOnlyHint` (or name) |
| `skills.py` / `harness.py` / `plugins.py` | Folders with `SKILL.md` / `harness.json` / `plugin.json`; built-in, plugin and user sources; upload/create/copy/export |
| `memory.py` | Scopes global / project / chat / harness; idle learning pass; task lessons; skill-improvement suggestions; chat summaries |
| `jobs.py` / `scheduler.py` | Tasks (every turn, pipeline, scheduled run, plugin action) and cron schedules with plain-English input |
| `services.py` | Starts the Playwright MCP browser helper for pipelines and the browser search fallback |
| `secrets.py` | OS credential store (Windows Credential Manager) with a private-file fallback |

## Formats

**Skill** — a folder with `SKILL.md`:
```markdown
---
name: monthly-report
description: Use when writing the monthly project report.
---
Steps and rules in Markdown. Extra files in the folder can be read with read_skill_file.
```

**Harness** — a folder with `harness.json` (+ optional `prompt.md`):
```json
{"name": "Proofreader", "description": "…", "type": "loop", "mode": "agent",
 "tools": ["files", "memory", "skills"], "connectors": false, "max_steps": 6, "temperature": 0.2,
 "thinking": false, "tool_mode": "native", "memory": true, "learn": true}
```
`mode`: `chat` (no tools), `agent` (tool loop), `planner` (JSON plan → steps → answer), `research` (code-driven:
plan queries → search → rank (official sources first, max 2 pages per site) → read in parallel → extract facts per
page (JSON) → download data files/PDFs → cited report; notes in `workspace/research/<time>/`; options `queries`,
`pages`, `max_downloads`), `team` (a lead splits the request into agents — child chats with their own harness, model
and limits — runs them in parallel, then combines). `tool_mode: "json"` uses grammar-constrained decisions for
models that fumble native tool calls. Limits: `max_reply_tokens`, `max_tool_calls`, `max_searches`,
`time_limit_minutes`, `tool_limits: {"web_fetch": 10}`. Chats can override these (`chats.settings`).

Built-in harnesses: Chat, Agent, Planner, Researcher (research mode), Data analyst, Coder, Writer, Document reader,
Vision analyst, Team lead.
Pipeline harnesses run code: `{"type": "pipeline", "entry": "run.py", "args": ["run"], "args_map": {"topic": "--topic"},
"inputs": {JSON schema}, "input_prompt": "…", "needs": ["browser", "model"], "timeout_minutes": 60}`. The entry
prints progress lines, optional `::progress {"text": …}` and one `::result {"summary_markdown": …}`. With
`needs: model` it gets `DV_LLM_API=openai`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`; with `browser`,
`DVA_BROWSER_MCP_URL`.

**Plugin** — a folder with `plugin.json`: `id`, `name`, `version`, `description`, `settings[]`
(`text|secret|select|boolean|textarea|folder`; secrets go to the credential store), `connectors[]` (`http` with
`auth: {type: bearer, setting: <key>}`, or `stdio` with `command`/`args`; `{node}`, `{python}`,
`{playwright_mcp}` placeholders), `setup` (a script returning `::result {ok, message, settings, options}`),
`actions[]`, `schedule_templates[]`, plus `skills/` and `harnesses/` folders. Scripts receive
`DVA_PLUGIN_SETTINGS` (JSON incl. secrets), `DVA_PLUGIN_DATA`, `DVA_PLUGIN_DIR`.

## The built-in Datavera plugin

`builtin/plugins/datavera`: the account connector (`https://api.datavera.io/v1/mcp/account`), skills
`datavera-publishing` and `datavera-account`, the **dataset-publisher** pipeline harness (runs the repository's
`agent.py` via `pipeline_entry.py`, with data in `plugin-data/datavera`, the app's llama.cpp model and browser
helper), `setup_hook.py` (checks the key with `whoami`, lists owners, optional import of the command-line
agent's catalogue/ledger, first catalogue bootstrap), actions (sync/stats/status) and two schedule templates.
The installer copies the pipeline files into `pipeline/`; in development they are read from the repository root.

## Safety model

- Engine listens on 127.0.0.1 only, requires the per-launch token, rejects other Host headers (DNS rebinding).
- llama-server listens on 127.0.0.1 with its own random API key.
- File tools only touch granted folders + the chat workspace; deletes are moved to `.trash`; edits keep `.backups`.
- Anything that changes something (writes, Python, schedules, connector tools without `readOnlyHint`) asks first,
  unless the chat or scheduled task turns on auto-approve. Unanswered approvals decline after a timeout.
- Memory refuses key/password/card-number-like text. Secrets never return to the UI after saving.
- `run_python` is not sandboxed (it is the user's own computer); it is off-able in Settings and always asks.

## Tests

```powershell
cd engine
.\.venv\Scripts\python.exe -m pytest -q tests\test_engine.py
```
Uses `tests/fake_llama_server.py` (scripted llama-server stand-in) and `tests/fake_mcp_server.py`. To also test a
real llama-server: set `DVA_TEST_LLAMA_SERVER` (path to llama-server) and `DVA_TEST_GGUF` (any small GGUF;
`tests/make_tiny_model.py` builds a random-weight one from llama.cpp's vocab files).
