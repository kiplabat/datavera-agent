"""Runs the Datavera dataset-publisher pipeline (agent.py and friends) for the desktop app.

The app passes the plugin's settings (DVA_PLUGIN_SETTINGS, incl. the API key), its data folder
(DVA_PLUGIN_DATA), the browser helper's address (DVA_BROWSER_MCP_URL) and the local model server
(DV_LLM_API=openai, LLM_BASE_URL, LLM_API_KEY, LLM_MODEL). Results are printed as one `::result {json}` line.

    pipeline_entry.py run [--topic T] [--source-url U] [--stop-after S]
    pipeline_entry.py sync | stats | status
"""
import contextlib
import io
import json
import os
import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = HERE / "pipeline"                       # copied here when the installer is built
if not (CODE / "agent.py").exists():
    CODE = HERE.parents[3]                     # development: the repository root (…/datavera-agent)
ACCOUNT_URL = "https://api.datavera.io/v1/mcp/account"


def prepare():
    settings = json.loads(os.environ.get("DVA_PLUGIN_SETTINGS") or "{}")
    home = Path(os.environ.get("DVA_PLUGIN_DATA") or HERE / "data")
    home.mkdir(parents=True, exist_ok=True)
    if not settings.get("api_key"):
        print("The Datavera plugin has no API key yet. Open Plugins → Datavera, paste your key and press "
              "'Check and connect'.", flush=True)
        print("::result " + json.dumps({"summary": "Not connected to Datavera yet (Plugins → Datavera)."}))
        sys.exit(2)
    os.environ.update({"DATAVERA_AGENT_HOME": str(home), "DATAVERA_MCP_URL": ACCOUNT_URL,
                       "DATAVERA_AUTH_MODE": "bearer", "DATAVERA_MCP_TOKEN": settings["api_key"]})
    cfg_path = home / "agent_config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig")) if cfg_path.exists() else {}
    if os.environ.get("DVA_BROWSER_MCP_URL"):
        cfg["browser_mcp_url"] = os.environ["DVA_BROWSER_MCP_URL"]
    if settings.get("owner"):
        cfg["owner"] = settings["owner"]
    cfg["stop_after_default"] = "submit" if settings.get("auto_submit", True) else "draft"
    topics = [t.strip() for t in str(settings.get("topics") or "").replace("\n", ",").split(",") if len(t.strip()) > 2]
    if topics:
        cfg["topics"] = topics[:40]
    else:
        cfg.pop("topics", None)
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    sys.path.insert(0, str(CODE))
    return home


def latest_run(home):
    runs = sorted((home / "outputs" / "runs").glob("*"))
    return runs[-1] if runs else None


def main():
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(errors="replace", line_buffering=True)
    args = sys.argv[1:] or ["run"]
    home = prepare()
    command = args[0]
    if command in ("sync", "stats", "run") and not list((home / "inventory").glob("baseline_*.json")):
        print("Your catalogue of existing datasets isn't built yet. Open Plugins → Datavera and press "
              "'Save, check and connect' (it builds it once).", flush=True)
        print("::result " + json.dumps({"summary": "Set up the Datavera plugin first (Plugins → Datavera → "
                                                   "Save, check and connect)."}), flush=True)
        return 2
    if command in ("sync", "stats"):
        sys.argv = [str(CODE / "inventory_store.py"), command]
        runpy.run_path(str(CODE / "inventory_store.py"), run_name="__main__")
        return 0
    import agent  # noqa: E402  (the pipeline)
    if command == "status":
        sys.argv = ["agent.py", "status"]
        return agent.main()
    before = latest_run(home)
    sys.argv = ["agent.py", *args]
    print("::progress " + json.dumps({"text": "Researching a dataset (this takes several minutes)…"}), flush=True)
    try:
        code = agent.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if exc.code and not isinstance(exc.code, int):
            print(str(exc.code), flush=True)
    run = latest_run(home)
    result = {"code": code}
    if run and run != before:
        summary = (run / "SUMMARY.md").read_text(encoding="utf-8") if (run / "SUMMARY.md").exists() else ""
        result["run_id"] = run.name
        result["run_folder"] = str(run)
        draft = run / "draft.json"
        if draft.exists():
            with contextlib.suppress(Exception):
                d = json.loads(draft.read_text(encoding="utf-8"))
                result["dataset"] = {k: d.get(k) for k in ("dataset_id", "edit_url", "view_url", "status")}
        cover = run / "package" / "cover.png"
        if cover.exists():
            result["cover"] = str(cover)
        nice = summary.replace("python agent.py resume", "pipeline resume")
        if code == 0 and result.get("dataset"):
            head = "**Published to Datavera.**" if "Submitted" in summary else "**Draft created on Datavera.**"
            link = result["dataset"].get("edit_url")
            nice = f"{head} {'[Open it](' + link + ')' if link else ''}\n\n" + nice
        elif code != 0:
            nice = ("**No dataset this time.** That is normal for some subjects; the pages and files it tried are "
                    "remembered and skipped next time.\n\n" + nice)
        result["summary_markdown"] = nice
    print("::result " + json.dumps(result, ensure_ascii=False), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
