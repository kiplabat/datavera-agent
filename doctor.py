"""Small diagnostics with explicit pass/fail meaning. Nothing here creates, uploads or submits anything.

    python agent.py doctor             installation, catalogue, Ollama, Playwright port (fast, no inference)
    python agent.py doctor --browser   real browser: open one source page + one search, ZERO model calls
    python agent.py doctor --model     four timed structured calls to the local model, no browser
    python agent.py doctor --datavera  read-only whoami + live tool schemas (no uploads)
"""
import importlib
import json
import os
import platform
import sys
from datetime import datetime, timezone

from connection import ROOT
from dv_progress import RunProgress


def mark(ok):
    return "PASS" if ok else "FAIL"


async def main(args):
    folder = ROOT / "outputs" / datetime.now(timezone.utc).strftime("doctor_%Y%m%dT%H%M%SZ")
    folder.mkdir(parents=True)
    p = RunProgress(folder)
    report = {"python": sys.version.split()[0], "platform": platform.platform(), "checks": {}}
    failures = 0

    def check(name, ok, detail):
        nonlocal failures
        report["checks"][name] = {"ok": bool(ok), "detail": detail}
        failures += 0 if ok else 1
        p.event(mark(ok), f"{name}: {detail}")

    versions = {}
    for mod in ("fastmcp", "mcp", "httpx", "pandas", "matplotlib", "jsonschema", "filelock", "dotenv", "openpyxl", "PIL"):
        try:
            versions[mod] = getattr(importlib.import_module(mod), "__version__", "installed")
        except ImportError:
            versions[mod] = None
    report["versions"] = versions
    missing = [m for m, v in versions.items() if v is None and m != "openpyxl"]
    check("python packages", not missing, f"{versions}" + (f"; MISSING {missing}" if missing else ""))
    if versions.get("openpyxl") is None:
        p.event("note", "openpyxl not installed: XLSX sources will be skipped (CSV/JSON still work)")

    env_names = [k for k in ("OLLAMA_MODEL", "OLLAMA_BASE_URL", "DATAVERA_MCP_URL", "DATAVERA_AUTH_MODE", "DATAVERA_MCP_TOKEN")
                 if os.getenv(k)]
    check(".env names present (values not shown)", {"DATAVERA_MCP_URL", "DATAVERA_MCP_TOKEN"} <= set(env_names), env_names)

    try:
        from inventory_relevance import ResearchInventory
        inv = ResearchInventory(ROOT)
        s = inv.summary()
        check("local catalogue", True, f"{s['total']} records; personal {s['personal']}; companies {s['by_company']}; "
              f"statuses {s['statuses']}; last local event {s['last_local_event_at']}")
    except Exception as exc:
        check("local catalogue", False, f"{type(exc).__name__}: {exc}")

    from dv_config import load_config
    cfg = load_config(require_user=False)
    check("account connected", bool(cfg.get("username")), f"username {cfg.get('username') or 'MISSING - run setup_account.py'}; "
          f"publishes as {cfg.get('owner') or '?'}; after each run: {cfg.get('stop_after_default')}")
    from dv_llm import LocalModel
    model = LocalModel(p, folder / "model_calls.jsonl", timeout=cfg["model_timeout_seconds"])
    try:
        present, names, loaded = await model.available()
        check("Ollama", present, f"{model.base_url}; model {model.model} {'present' if present else 'MISSING'}; "
              f"loaded: {[(m.get('name'), m.get('size_vram'), m.get('context_length')) for m in loaded]}")
    except Exception as exc:
        check("Ollama", False, f"{model.base_url} unreachable: {type(exc).__name__}")

    from dv_browser import Browser, BrowserError
    try:
        async with Browser(cfg["browser_mcp_url"], p, folder, engines=cfg["search_engines"],
                           templates=cfg.get("search_url_templates")) as b:
            check("Playwright MCP", True, f"{cfg['browser_mcp_url']} exposes browser_navigate + browser_evaluate "
                  f"({len(b.tools)} tools)")
            if args.browser:
                await browser_check(b, args, p, report, check)
    except BrowserError as exc:
        check("Playwright MCP", False, str(exc))

    if args.model:
        await model_check(model, p, report, check)

    if args.datavera:
        await datavera_check(p, report, check, folder)

    (folder / "doctor.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    p.event("output", str(folder / "doctor.json"))
    p.event("result", "ALL CHECKS PASSED" if not failures else f"{failures} CHECK(S) FAILED — see above")
    return 0 if not failures else 1


async def browser_check(b, args, p, report, check):
    from dv_discovery import data_links, detect_licence, followup_links, licence_links
    page, err = await b.open(args.url, "doctor source page")
    if page is None:
        check("browser: open source page", False, f"{args.url}: {err}")
        return
    links = page.get("links", [])
    hidden = [l for l in links if not l.get("visible", True)]
    found = data_links(links, page["url"], "marine heatwave")
    lic = detect_licence(page.get("text", ""), [l.get("href", "") for l in links])
    report["browser_page"] = {"url": page["url"], "title": page.get("title"), "links": len(links), "hidden_links": len(hidden),
                              "data_links": found[:15], "licence": lic,
                              "followup_links": followup_links(links, page["url"]),
                              "licence_links": licence_links(links, page["url"])}
    check("browser: open source page", len(links) > 0, f"{page.get('title')!r}: {len(links)} links, {len(hidden)} inside "
          f"hidden panels/tabs, {len(page.get('text', ''))} text characters")
    p.event("info", f"data links found: {[(d['format'], d['url'][-70:]) for d in found[:8]]}")
    p.event("info", f"reuse terms: {lic['label'] or 'none on this page'}; follow-up pages: "
            f"{[f['url'] for f in report['browser_page']['followup_links']]}; terms pages: {report['browser_page']['licence_links']}")
    results, engine = await b.search(args.query)
    report["browser_search"] = {"engine": engine, "results": results}
    check("browser: search", bool(results), f"{len(results)} result links via {engine}" if results else
          "no engine returned result links (check the Edge window for a consent page/CAPTCHA)")


async def model_check(model, p, report, check):
    from dv_llm import LLMError
    topics = ["glacier mass balance", "airport passenger traffic", "honey bee colony losses", "Kenya rainfall statistics",
              "urban rail ridership", "coffee production by country", "volcanic eruptions catalogue", "food price index"]
    menu = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(topics))
    topic_schema = {"type": "object", "properties": {"choice": {"type": "integer", "enum": list(range(1, 9))},
                                                     "query": {"type": "string", "maxLength": 90}}, "required": ["choice", "query"]}
    roles_schema = {"type": "object", "properties": {
        "measure": {"type": "string", "enum": ["Passengers", "Freight tonnes", "Flights"]},
        "time": {"type": "string", "enum": ["Year", "(none)"]},
        "category": {"type": "string", "enum": ["Airport", "Country", "(none)"]},
        "aggregate": {"type": "string", "enum": ["sum", "mean"]},
        "measure_label": {"type": "string", "maxLength": 50}},
        "required": ["measure", "time", "category", "aggregate", "measure_label"]}
    topic_prompt = ("Pick the topic most likely to have a downloadable CSV table from an original publisher, then a web "
                    "search query of at most 10 words. No URLs.\n" + menu)
    calls = [("tiny", "Reply with JSON only.", 'Return {"ok": true}.',
              {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}, 16),
             ("topic", "You plan research. Reply with JSON only.", topic_prompt, topic_schema, 80),
             ("topic (identical repeat: prompt-cache test)", "You plan research. Reply with JSON only.", topic_prompt, topic_schema, 80),
             ("columns", "You prepare charts from a table. Reply with JSON only.",
              "Table: airport statistics, 4,812 rows. Columns:\n- Year | year | 24 distinct | e.g. 2000, 2001\n"
              "- Airport | text | 210 distinct | e.g. Nairobi JKIA, Mombasa\n- Country | text | 9 distinct | e.g. Kenya, Uganda\n"
              "- Passengers | number | 4700 distinct | e.g. 512000\n- Freight tonnes | number | 3000 distinct | e.g. 12.5\n"
              "- Flights | number | 2500 distinct | e.g. 840\nChoose the most interesting measure, time column and grouping. "
              "aggregate: sum for amounts that add up; mean for rates.", roles_schema, 80)]
    results = []
    for name, system, user, schema, max_tokens in calls:
        try:
            value = await model.json(f"doctor {name}", system, user, schema, max_tokens=max_tokens, retries=0)
            ok = True
        except LLMError as exc:
            value, ok = str(exc), False
        results.append({"name": name, "ok": ok, "value": value})
    metrics = [json.loads(l) for l in (model.log_path.read_text(encoding="utf-8").splitlines() if model.log_path.exists() else [])]
    report["model"] = {"results": results, "metrics": metrics}
    check("model: schema-valid JSON on all calls", all(r["ok"] for r in results),
          "; ".join(f"{r['name']}={r['value']}" for r in results)[:600])
    good = [m for m in metrics if "wall_seconds" in m]
    if len(good) >= 3:
        loads = [m["load_seconds"] for m in good[1:]]
        p.event("info", f"wall seconds per call: {[m['wall_seconds'] for m in good]}")
        p.event("info", f"prompt tokens: {[m['prompt_tokens'] for m in good]}; prompt tok/s: {[m.get('prompt_tok_per_s') for m in good]}; "
                        f"output tok/s: {[m.get('output_tok_per_s') for m in good]}")
        if max(loads) > 5:
            p.event("diagnosis", "Model load time on later calls > 5s: the model is being reloaded between calls "
                                 "(context size or keep_alive mismatch, or memory pressure).")
        # Ollama reports the full prompt count even when it reuses a cached prefix, so compare times, not counts.
        t_first, t_rep = good[1]["prompt_seconds"], good[2]["prompt_seconds"]
        if t_first and t_rep < 0.3 * t_first:
            p.event("diagnosis", f"Identical repeat read its prompt in {t_rep}s vs {t_first}s: prompt cache works. "
                                 "Calls that share a long prefix are cheaper; unique prompts still cost full price.")
        elif t_first:
            p.event("diagnosis", f"Identical repeat took {t_rep}s vs {t_first}s to read: no prompt reuse; keep prompts small.")
        rate = [m.get("prompt_tok_per_s") for m in good if m.get("prompt_tok_per_s") and m["prompt_seconds"] > 1]
        if rate:
            p.event("diagnosis", f"Fresh prompt reading ~{sum(rate)/len(rate):.0f} tokens/s: a 2,000-token prompt costs "
                                 f"~{2000/(sum(rate)/len(rate)):.0f}s before any output.")


async def datavera_check(p, report, check, folder):
    import agent
    try:
        async with agent.datavera_client_factory() as client:
            from dv_publisher import Publisher
            pub = Publisher(ROOT, client, p)
            ident = await pub.check_account()
            props = pub.schema_props("create_dataset")
            needed = {"title", "tag", "summary", "source", "source_url", "charts", "company", "cover_upload_id",
                      "client_request_id", "license", "downloads", "key_highlights", "analysis", "methodology", "citation"}
            schemas = {name: (getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)) for name, t in pub.tools.items()}
            (folder / "tool_schemas.json").write_text(json.dumps(schemas, indent=2), encoding="utf-8")
            check("datavera: account", True, f"{ident.get('username')}; companies {[c.get('slug') for c in ident.get('companies', [])]}; "
                  f"{ident.get('on_submit') or ident.get('publication_mode')}")
            check("datavera: create_dataset fields used by the agent", needed <= set(props),
                  f"missing {sorted(needed - set(props))}" if not needed <= set(props) else "all present (incl. client_request_id)")
    except Exception as exc:
        check("datavera: account", False, f"{type(exc).__name__}: {str(exc)[:200]}")
