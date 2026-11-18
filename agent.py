"""Datavera local agent: research -> acquire -> analyse -> package -> validate -> draft -> submit.

A fixed, checkpointed pipeline. Python does the browsing, downloading, arithmetic, charts and MCP calls.
The local model answers at most four bounded questions per run (topic, candidate, column roles, wording),
each with a JSON schema whose choices are enumerated from real evidence, and each with a fallback.

    python agent.py doctor [--browser] [--model] [--datavera] [--url URL]
    python agent.py run [--stop-after STAGE] [--submit] [--source-url URL] [--topic TEXT] [--owner USERNAME_OR_COMPANY]
    python agent.py resume RUN_ID [--stop-after STAGE] [--submit]
    python agent.py status [RUN_ID]
"""
import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

for _name in ("NO_PROXY", "no_proxy"):
    os.environ[_name] = ",".join(filter(None, [os.environ.get(_name, ""), "127.0.0.1", "localhost", "::1"]))

import pandas as pd  # noqa: E402
from filelock import FileLock, Timeout  # noqa: E402

import connection  # noqa: E402  (loads .env; never prints secrets)
from dv_acquire import AcquireError, download, parse, profile, unpack_zip  # noqa: E402
from dv_analysis import (AnalysisError, allowed_numbers, analyse, candidate_roles, check_roles, cover,  # noqa: E402
                         fmt, heuristic_roles, highlights, on_topic, pct, template_text, unsupported_numbers)
from dv_browser import Browser, BrowserError  # noqa: E402
from dv_discovery import (data_links, detect_licence, followup_links, host_of, licence_links, page_key,  # noqa: E402
                          site_of)
from dv_llm import LLMError, LocalModel  # noqa: E402
from dv_progress import RunProgress, safe_text  # noqa: E402
from dv_publisher import Publisher, PublishError, inventory_conflicts  # noqa: E402
from inventory_relevance import ResearchInventory  # noqa: E402

ROOT = connection.ROOT
STAGES = ["plan", "search", "inspect", "select", "acquire", "roles", "analyse", "narrative", "package",
          "validate", "draft", "submit"]
DEFAULT_PORTALS = ["data.humdata.org", "zenodo.org", "catalog.data.gov", "data.gov.uk", "open.africa"]
DEFAULT_AVOID = ["codesights.com", "dtbse.com", "kaggle.com", "statista.com", "scribd.com", "researchgate.net",
                 "pinterest.com", "youtube.com", "reddit.com", "quora.com", "medium.com", "linkedin.com", "facebook.com"]
CATEGORIES = ["Agriculture", "Climate", "Culture", "Demographics", "Economy", "Education", "Energy", "Environment",
              "Finance", "Health", "Infrastructure", "Maps", "Politics", "Science", "Society", "Sport", "Tech", "Trade"]
datavera_client_factory = connection.datavera_client  # replaced only by the offline test harness


class StageFailed(RuntimeError):
    """A stage could not produce usable output; the reason is user-facing."""


def utcnow():
    return datetime.now(timezone.utc)


def load_config():
    from dv_config import load_config as _load
    return _load()


def jdump(path, value):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def jload(path, default=None):
    return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else default


# --------------------------------------------------------------------------- ledger (durable cross-run memory)

class Ledger:
    """Append-only record of topics, pages and data files already tried, so repeat runs move on."""

    def __init__(self, root):
        self.path = root / "agent_state" / "ledger.jsonl"
        self.path.parent.mkdir(exist_ok=True)

    def add(self, **entry):
        entry = {"time": utcnow().isoformat(), **entry}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def rows(self):
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def seen_data_urls(self):
        return {r["data_url"] for r in self.rows() if r.get("kind") == "candidate" and r.get("outcome") != "transient"}

    def rejected_pages(self):
        return {r["page"] for r in self.rows() if r.get("kind") == "page" and r.get("outcome") != "ok"}

    def recent_topics(self, runs):
        topics = [r["topic"] for r in self.rows() if r.get("kind") == "topic"]
        return set(topics[-runs:]) if runs else set()


# --------------------------------------------------------------------------- run state

class Run:
    def __init__(self, run_id, args, config, fresh):
        self.id = run_id
        self.folder = ROOT / "outputs" / "runs" / run_id
        self.folder.mkdir(parents=True, exist_ok=not fresh)
        for sub in ("pages", "data", "package"):
            (self.folder / sub).mkdir(exist_ok=True)
        self.state_path = self.folder / "state.json"
        self.state = jload(self.state_path) or {"run_id": run_id, "created": utcnow().isoformat(), "done": [],
                                                "owner": args.owner or config["owner"],
                                                "source_url": args.source_url, "topic": args.topic}
        self.config = config
        self.progress = RunProgress(self.folder)
        self.ledger = Ledger(ROOT)

    def save(self):
        jdump(self.state_path, self.state)

    def done(self, stage, **outputs):
        self.state.setdefault("outputs", {})[stage] = outputs
        if stage not in self.state["done"]:
            self.state["done"].append(stage)
        self.state["last_stage"] = stage
        self.save()

    def out(self, stage):
        return self.state.get("outputs", {}).get(stage, {})


# --------------------------------------------------------------------------- stages

async def stage_plan(run, model, inv):
    cfg, p = run.config, run.progress
    if run.state.get("source_url"):
        topic = run.state.get("topic") or "operator-supplied source"
        run.done("plan", topic=topic, query=None, how="operator supplied --source-url")
        return
    if run.state.get("topic"):
        topic = run.state["topic"]
        run.done("plan", topic=topic, query=f"{topic} data csv download", how="operator supplied --topic")
        return
    recent = run.ledger.recent_topics(cfg["seed_cooldown_runs"])
    offered = []
    rotation = len([r for r in run.ledger.rows() if r.get("kind") == "topic"])
    topics = cfg["topics"][rotation % len(cfg["topics"]):] + cfg["topics"][:rotation % len(cfg["topics"])]
    for topic in topics:
        if topic in recent:
            continue
        related = inv.search(topic, 3)
        strong = [r for r in related if r["score"] >= 0.75 and len(r.get("matched_terms", [])) >= 2]
        if strong:
            p.event("catalogue", f"Skipping topic {topic!r}: already covered by {strong[0]['title'][:70]!r}")
            continue
        offered.append(topic)
        if len(offered) >= cfg["offer_topics"]:
            break
    if not offered:
        raise StageFailed("Every configured topic is recent or already covered; add topics to agent_config.json")
    menu = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(offered))
    schema = {"type": "object", "properties": {
        "choice": {"type": "integer", "enum": list(range(1, len(offered) + 1))},
        "query": {"type": "string", "maxLength": 90}}, "required": ["choice", "query"]}
    try:
        answer = await model.json("choose topic", "You plan research for a data-visualisation publisher. Reply with JSON only.",
                                  "Pick the topic most likely to have a downloadable CSV or XLSX table from an original "
                                  "publisher (statistics office, government agency, research institute), then write a web "
                                  "search query of at most 10 words to find that file. No URLs, no site: filters, no quotes.\n"
                                  + menu, schema, max_tokens=80)
        topic = offered[answer["choice"] - 1]
        query = re.sub(r"(?i)https?://\S+|site:\S+|[\"']", " ", answer["query"]).strip()
        query = " ".join(query.split()[:12])
        how = "model"
        if len(query) < 8 or "datavera" in query.lower():
            query, how = f"{topic} data csv download", "model topic; template query"
    except LLMError as exc:
        topic, query, how = offered[0], f"{offered[0]} data csv download", f"fallback ({exc})"
    run.ledger.add(kind="topic", topic=topic, run=run.id)
    p.event("plan", f"Topic: {topic}; query: {query} ({how})")
    run.done("plan", topic=topic, query=query, offered=offered, how=how)


async def stage_search(run, browser):
    plan, p = run.out("plan"), run.progress
    if run.state.get("source_url"):
        run.done("search", results=[{"url": run.state["source_url"], "label": "operator supplied"}], engine=None)
        return
    rejected = run.ledger.rejected_pages()
    avoid = set(run.config.get("avoid_sites", DEFAULT_AVOID))
    portals = run.config.get("portal_sites", DEFAULT_PORTALS)
    turn = len([r for r in run.ledger.rows() if r.get("kind") == "topic"])
    queries = [plan["query"]]
    if portals:  # one open-data portal per run, rotating: these show file links and licences on the page
        queries.append(f"{plan['topic']} site:{portals[turn % len(portals)]}")
    per_query, engines = [], []
    for query in dict.fromkeys(queries):
        found, engine = await browser.search(query)
        engines.append(engine)
        kept = []
        for row in found:
            if page_key(row["url"]) in rejected:
                continue
            if site_of(row["url"]) in avoid:
                p.event("search", f"Skipping aggregator/secondary site {site_of(row['url'])}")
                continue
            kept.append(row)
        per_query.append(kept)
    results = []
    for i in range(max((len(k) for k in per_query), default=0)):  # interleave so each query is represented
        for kept in per_query:
            if i < len(kept) and page_key(kept[i]["url"]) not in {page_key(r["url"]) for r in results}:
                results.append(kept[i])
    jdump(run.folder / "search_results.json", results)
    if not results:
        raise StageFailed("Search returned no usable external result links (all engines empty, blocked, or already tried)")
    p.event("search", f"{len(results)} candidate pages to inspect")
    run.done("search", results=results[:run.config["max_result_pages"]], engines=engines)


async def stage_inspect(run, browser):
    cfg, p = run.config, run.progress
    results = run.out("search")["results"]
    deadline = time.monotonic() + cfg["browse_minutes"] * 60
    seen_data = run.ledger.seen_data_urls()
    query = (run.out("plan").get("query") or run.out("plan").get("topic") or "")
    candidates, pages, budget, already = [], [], cfg["max_pages_total"], []

    async def visit(url, label):
        nonlocal budget
        if budget <= 0 or time.monotonic() > deadline:
            return None
        budget -= 1
        page, err = await browser.open(url, label)
        if page is None:
            p.event("inspect", f"Could not read {url}: {err}")
        return page

    for row in results:
        if budget <= 0 or time.monotonic() > deadline:
            p.event("inspect", "Browsing budget reached")
            break
        page = await visit(row["url"], row.get("label", ""))
        if page is None:
            pages.append({"url": row["url"], "outcome": "unreadable"})
            continue
        links = page.get("links", [])
        found = data_links(links, page["url"], query)
        lic = detect_licence(page.get("text", ""), [l.get("href", "") for l in links])
        lic_page = page["url"] if lic["accepted"] else None
        if not lic["accepted"] and lic["licence"] is None and "non-commercial" not in lic["reason"] and "no-derivatives" not in lic["reason"]:
            for lurl in licence_links(links, page["url"], limit=1):
                lp = await visit(lurl, "reuse terms")
                if lp:
                    lic2 = detect_licence(lp.get("text", ""), [l.get("href", "") for l in lp.get("links", [])])
                    if lic2["accepted"] or lic2["licence"] is None and lic2["label"]:
                        lic, lic_page = lic2, lp["url"]
                break
        if not any(d["supported"] for d in found):
            for f in followup_links(links, page["url"], limit=1):
                fp = await visit(f["url"], f["label"])
                if fp:
                    more = data_links(fp.get("links", []), fp["url"], query)
                    for d in more:
                        d["found_on"] = fp["url"]
                    found += more
                    if not lic["accepted"]:
                        lic3 = detect_licence(fp.get("text", ""), [l.get("href", "") for l in fp.get("links", [])])
                        if lic3["accepted"]:
                            lic, lic_page = lic3, fp["url"]
        supported = [d for d in found if d["supported"]]
        unsupported = sorted({d["format"] for d in found if not d["supported"]})
        outcome = "ok"
        if not supported:
            outcome = "unsupported_only:" + ",".join(unsupported) if unsupported else "no_data_links"
        elif not lic["accepted"]:
            outcome = "terms:" + (lic["reason"] or "unknown")
        pages.append({"url": page["url"], "title": page.get("title", ""), "data_links": len(supported),
                      "hidden_data_links": sum(d["hidden"] for d in supported), "unsupported": unsupported,
                      "licence": lic, "licence_page": lic_page, "outcome": outcome})
        p.event("inspect", f"{page.get('title', '')[:60]!r}: {len(supported)} table links"
                           + (f" ({sum(d['hidden'] for d in supported)} in hidden panels)" if supported else "")
                           + (f"; other formats {unsupported}" if unsupported else "")
                           + f"; terms: {lic['label'] or 'none found'} -> {outcome}")
        if outcome != "ok":
            run.ledger.add(kind="page", page=page_key(row["url"]), outcome=outcome, run=run.id)
            continue
        source_name = page.get("site_name") or re.split(r"\s[|\-–—]\s", page.get("title", ""))[-1].strip() or host_of(page["url"])
        for d in supported[:3]:
            if d["url"] in seen_data:
                p.event("inspect", f"Skipping already-tried data file {d['url']}")
                already.append(d["url"])
                continue
            candidates.append({"id": f"C{len(candidates) + 1}", "page_url": page["url"], "page_title": page.get("title", ""),
                               "page_description": page.get("description", ""), "source_name": source_name[:120],
                               "data_url": d["url"], "data_label": d["label"], "format": d["format"],
                               "found_on": d.get("found_on", page["url"]), "hidden": d["hidden"], "score": d["score"],
                               "licence": lic["licence"], "licence_label": lic["label"],
                               "licence_evidence": lic["evidence"][:300], "licence_page": lic_page})
        if len({c["page_url"] for c in candidates}) >= cfg["min_candidates"]:
            break
    jdump(run.folder / "pages_inspected.json", pages)
    jdump(run.folder / "candidates.json", candidates)
    if not candidates and already:
        raise StageFailed(f"Every usable file found ({len(already)}) was already tried or published by an earlier run "
                          "(agent_state/ledger.jsonl). No duplicate was created. Try another topic.")
    if not candidates:
        reasons = {}
        for pg in pages:
            reasons[pg["outcome"].split(":")[0]] = reasons.get(pg["outcome"].split(":")[0], 0) + 1
        raise StageFailed(f"No page offered a tabular file with recognisable reuse terms. Page outcomes: {reasons}. "
                          "Those pages are recorded and will be skipped next run.")
    run.done("inspect", candidates=len(candidates), pages=len(pages))


async def stage_select(run, model, inv):
    p = run.progress
    cands = jload(run.folder / "candidates.json", [])
    viable = []
    for c in cands:
        conflicts = inventory_conflicts(inv, source_url=c["page_url"], data_url=c["data_url"])
        related = [r for r in inv.search(c["page_title"], 3) if r["score"] >= 0.8 and len(r.get("matched_terms", [])) >= 2]
        if conflicts or related:
            why = conflicts[0]["why"] if conflicts else ["similar title"]
            match = (conflicts or related)[0]["title"]
            p.event("catalogue", f"{c['id']} matches existing {match[:70]!r} ({', '.join(why)}); skipped")
            run.ledger.add(kind="candidate", data_url=c["data_url"], page=c["page_url"], outcome="catalogue_match",
                           reason=f"{why}: {match}", run=run.id)
            continue
        viable.append(c)
    if not viable:
        raise StageFailed("All candidates match datasets already in the local catalogue")
    viable.sort(key=lambda c: -c["score"])
    options = viable[:5]
    order = list(options)
    if len(options) > 1:
        menu = "\n".join(f"{i + 1}. {c['page_title'][:80]} | {c['source_name'][:40]} | file: {c['data_label'][:60] or c['data_url'][-60:]} "
                         f"({c['format']}) | terms: {c['licence_label']}" for i, c in enumerate(options))
        schema = {"type": "object", "properties": {"choice": {"type": "integer", "enum": list(range(1, len(options) + 1))}},
                  "required": ["choice"]}
        try:
            ans = await model.json("choose candidate", "You select source data for charts. Reply with JSON only.",
                                   f"Topic: {run.out('plan').get('topic')}\nWhich file is most likely a clean table about the "
                                   "topic covering several years or several places/groups? Prefer official publishers.\n" + menu,
                                   schema, max_tokens=20)
            chosen = options[ans["choice"] - 1]
            order = [chosen] + [c for c in options if c is not chosen]
            p.event("select", f"Model chose {chosen['id']}: {chosen['data_url']}")
        except LLMError as exc:
            p.event("select", f"Model choice unavailable ({exc}); using highest-scoring candidate")
    jdump(run.folder / "selection_order.json", order + viable[5:])
    run.done("select", order=[c["id"] for c in order + viable[5:]])


async def stage_acquire(run):
    p, cfg = run.progress, run.config
    by_id = {c["id"]: c for c in jload(run.folder / "candidates.json", [])}
    order = [by_id[i] for i in run.out("select")["order"]]
    for c in order[:cfg["max_acquire_attempts"]]:
        p.busy("acquire", f"downloading {c['data_url']}")
        try:
            meta = await download(c["data_url"], run.folder / "data", max_bytes=cfg["max_download_mb"] * 1_000_000)
            path, fmt, archive = meta["path"], c["format"], None
            if fmt == "zip":
                path, fmt, members, cite = unpack_zip(path, run.folder / "data")
                archive = {"members": members[:20], "table": Path(path).name.replace("unzipped_", ""), "citation": cite}
                p.event("acquire", f"{c['id']}: archive contains {members[:6]}; using {archive['table']}"
                                   + (f"; metadata citation: {cite[:100]}" if cite else ""))
            df, notes = parse(path, fmt)
            if archive:
                notes.insert(0, f"extracted {archive['table']} from the ZIP archive")
            prof = profile(df)
            roles = candidate_roles(prof)
            if prof["rows"] < 10:
                raise AcquireError(f"only {prof['rows']} rows")
            if not roles["measure"] or not (roles["time"] or roles["category"]):
                raise AcquireError("no numeric measure with a time or category column")
            # Hard duplicate check on content as well as URL
            conflicts = inventory_conflicts(run.inventory, data_sha256=meta["sha256"])
            if conflicts:
                raise AcquireError(f"same file content already published as {conflicts[0]['title']!r}")
        except (AcquireError, Exception) as exc:  # one bad file must not end the run
            transient = "timed out" in str(exc).lower() or "connect" in type(exc).__name__.lower()
            reason = f"{type(exc).__name__}: {safe_text(exc)[:200]}"
            p.event("acquire", f"{c['id']} rejected: {reason}")
            run.ledger.add(kind="candidate", data_url=c["data_url"], page=c["page_url"],
                           outcome="transient" if transient else "unsuitable", reason=reason, run=run.id)
            continue
        finally:
            p.idle()
        table = run.folder / "data" / "source_table.csv"
        df.to_csv(table, index=False)
        jdump(run.folder / "data" / "profile.json", prof)
        p.event("acquire", f"{c['id']}: {meta['bytes']/1000:.0f} KB, {prof['rows']} rows x {len(df.columns)} columns; "
                           f"measures {roles['measure'][:4]}, time {roles['time'][:2]}, groups {roles['category'][:3]}")
        run.ledger.add(kind="candidate", data_url=c["data_url"], page=c["page_url"], outcome="selected", run=run.id)
        run.done("acquire", candidate=c, download=meta, parse_notes=notes, table=str(table), columns=list(df.columns),
                 archive=archive)
        return
    raise StageFailed("None of the selected files could be used as a table (see progress.log); they are recorded and skipped next time")


def read_table(run):
    return pd.read_csv(run.out("acquire")["table"], low_memory=False)


async def stage_roles(run, model):
    p = run.progress
    prof = jload(run.folder / "data" / "profile.json")
    cand = run.out("acquire")["candidate"]
    roles_c = candidate_roles(prof)
    lines = []
    for col in prof["columns"]:
        if col["name"] in roles_c["time"] + roles_c["measure"] + roles_c["category"]:
            lines.append(f"- {col['name']} | {col['kind']} | {col['distinct']} distinct | e.g. {', '.join(col['examples'])}")
    none = "(none)"
    schema = {"type": "object", "properties": {
        "measure": {"type": "string", "enum": roles_c["measure"][:25]},
        "time": {"type": "string", "enum": roles_c["time"][:10] + [none]},
        "category": {"type": "string", "enum": roles_c["category"][:15] + [none]},
        "aggregate": {"type": "string", "enum": ["sum", "mean"]},
        "measure_label": {"type": "string", "maxLength": 50},
        "unit": {"type": "string", "maxLength": 20},
        "category_label": {"type": "string", "maxLength": 30},
        "tag": {"type": "string", "enum": CATEGORIES}},
        "required": ["measure", "time", "category", "aggregate", "measure_label", "unit", "category_label", "tag"]}
    heur = heuristic_roles(prof)
    try:
        ans = await model.json(
            "choose columns", "You prepare charts from a table. Reply with JSON only.",
            f"Table from: {cand['page_title'][:100]} ({cand['source_name'][:60]}), file {cand['data_label'][:60]}. "
            f"{prof['rows']} rows. Columns:\n" + "\n".join(lines) +
            "\nChoose the most interesting numeric measure, its time column and a grouping column if useful. "
            "aggregate: sum for counts/amounts that add up across rows; mean for rates, indices, prices, temperatures. "
            "measure_label: short plain-English name of the measure; unit: its unit or empty; "
            "category_label: plain name of the grouping, or empty.", schema, max_tokens=120)
        roles = {"measure": ans["measure"], "time": None if ans["time"] == none else ans["time"],
                 "category": None if ans["category"] == none else ans["category"], "aggregate": ans["aggregate"]}
        labels = {"measure_label": ans["measure_label"].strip() or ans["measure"],
                  "unit": ans["unit"].strip(), "category_label": ans["category_label"].strip(), "tag": ans["tag"]}
        how = "model"
    except LLMError as exc:
        roles, how = heur, f"heuristic ({exc})"
        labels = {"measure_label": heur["measure"], "unit": "", "category_label": heur.get("category") or "", "tag": "Science"}
    df = read_table(run)
    try:
        check_roles(df, roles)
    except AnalysisError as exc:
        p.event("roles", f"Model roles unusable ({exc}); using heuristic roles")
        roles, how = heur, "heuristic"
    # Guard 1: several rows per period but no grouping would mix entities (e.g. countries with "World").
    if roles.get("time") and not roles.get("category") and heur.get("category") \
            and int(df.groupby(roles["time"]).size().max()) > 1:
        roles["category"] = heur["category"]
        labels["category_label"] = labels.get("category_label") or heur["category"]
        p.event("roles", f"Several rows per {roles['time']}: grouping by {heur['category']!r} so groups are not mixed")
        how += " + grouping guard"
    # Guard 2: the label must describe the chosen column, not something the model imagined.
    vocab = set(re.findall(r"[a-z]{4,}", " ".join([roles["measure"], cand["page_title"], cand["data_label"]]).lower()))
    if not set(re.findall(r"[a-z]{4,}", labels["measure_label"].lower())) & vocab:
        p.event("roles", f"Label {labels['measure_label']!r} does not match column {roles['measure']!r}; using the column name")
        labels["measure_label"], labels["unit"] = roles["measure"], ""
        how += " + label guard"
    p.event("roles", f"measure={roles['measure']!r} ({roles['aggregate']}), time={roles['time']!r}, "
                     f"group={roles['category']!r}; label {labels['measure_label']!r} [{how}]")
    run.done("roles", roles=roles, labels=labels, heuristic=heur, how=how)


async def stage_analyse(run):
    out = run.out("roles")
    df = read_table(run)
    folder = run.folder / "package"
    try:
        result = analyse(df, out["roles"], out["labels"], folder)
    except AnalysisError as exc:
        if out["roles"] == out["heuristic"]:
            raise StageFailed(f"Analysis failed: {exc}")
        run.progress.event("analyse", f"Chosen roles failed ({exc}); retrying with heuristic roles")
        result = analyse(df, out["heuristic"], {**out["labels"], "measure_label": out["heuristic"]["measure"]}, folder)
    result["highlights"] = highlights(result["facts"], out["labels"])
    jdump(run.folder / "analysis.json", result)
    for h in result["highlights"]:
        run.progress.event("finding", h)
    if result["facts"].get("incomplete_periods_removed"):
        run.progress.event("analyse", f"Excluded incomplete current period: {result['facts']['incomplete_periods_removed']}")
    if result["facts"].get("aggregate_groups_removed"):
        run.progress.event("analyse", f"Excluded aggregate rows: {result['facts']['aggregate_groups_removed'][:8]}")
    if result["facts"].get("warning"):
        run.progress.event("REVIEW", result["facts"]["warning"])
    run.done("analyse", files=result["files"], charts=len(result["charts"]), time_period=result["time_period"])


def compact_facts(result, labels):
    f = result["facts"]
    unit = f.get("unit") or ""
    lines = [f"measure: {labels['measure_label']} ({'total' if f['aggregate'] == 'sum' else 'average'}); rows used {f['rows_used']}"]
    if f.get("time"):
        t = f["time"]
        lines.append(f"period {t['first_period']} to {t['last_period']} ({t['periods']} periods): from {fmt(t['first_value'], unit)} "
                     f"to {fmt(t['last_value'], unit)}; change {pct(t['change_pct']) if t['change_pct'] is not None else 'n/a'}; "
                     f"peak {fmt(t['max_value'], unit)} in {t['max_period']}; low {fmt(t['min_value'], unit)} in {t['min_period']}")
    if f.get("ranking"):
        r = f["ranking"]
        tops = "; ".join(f"{x['name']} {fmt(x['value'], unit)}" + (f" ({pct(x['share_pct'])})" if x.get("share_pct") is not None else "")
                         for x in r["top"])
        lines.append(f"{r['groups']} groups ({f.get('ranking_scope')}); top: {tops}; lowest {r['bottom']['name']} "
                     f"{fmt(r['bottom']['value'], unit)}; median {fmt(r['median_group_value'], unit)}")
    if f.get("decades"):
        lines.append("decade averages: " + "; ".join(f"{k} {fmt(v, unit)}" for k, v in f["decades"].items()))
    if f.get("distribution"):
        d = f["distribution"]
        lines.append(f"row values: median {fmt(d['median'], unit)}, 10th pct {fmt(d['p10'], unit)}, 90th pct {fmt(d['p90'], unit)}")
    return "\n".join(lines)


def clean_plain(text):
    text = re.sub(r"[*#`]+", "", str(text))
    text = re.sub(r"(?m)^\s*[-•]\s+", "", text)
    return text.strip()


async def stage_narrative(run, model, inv):
    p = run.progress
    result = jload(run.folder / "analysis.json")
    labels = run.out("roles")["labels"]
    cand = run.out("acquire")["candidate"]
    meta = {"source": cand["source_name"]}
    facts_text = compact_facts(result, labels)
    template = template_text(result["facts"], labels, meta)
    allowed = allowed_numbers(result["facts"], facts_text + " " + result["time_period"] + " " + cand["page_title"])
    schema = {"type": "object", "properties": {
        "title": {"type": "string", "maxLength": 95}, "subtitle": {"type": "string", "maxLength": 180},
        "summary": {"type": "string", "maxLength": 380}, "analysis": {"type": "string", "maxLength": 1600}},
        "required": ["title", "subtitle", "summary", "analysis"]}
    text, how = dict(template), "template"
    try:
        ans = await model.json(
            "write text", "You write short, factual captions for data charts. Reply with JSON only. Plain text, no Markdown.",
            f"Source: {meta['source']} — {cand['page_title'][:100]}\nFACTS (the only facts you may use):\n{facts_text}\n"
            "Write: title (specific, under 90 characters), subtitle (one line), summary (2 sentences), analysis "
            "(2 short paragraphs separated by a blank line, ending with one sentence on limitations). Use only numbers "
            "that appear in FACTS, written the same way. Do not add causes, context or numbers not in FACTS.",
            schema, max_tokens=520)
        how = "model"
        vocab = {w for w in re.findall(r"[a-z]{4,}", " ".join([
            labels.get("measure_label", ""), labels.get("category_label", ""), cand["page_title"], cand["data_label"],
            run.out("plan").get("topic") or "", run.out("roles")["roles"]["measure"]]).lower())}
        vocab -= {"data", "dataset", "download", "table", "file", "with", "from", "this", "that", "total", "value"}
        for key in ("title", "subtitle", "summary", "analysis"):
            value = clean_plain(ans.get(key, ""))
            bad = unsupported_numbers(value, allowed)
            problem = ("empty" if not value else f"unsupported numbers {bad[:4]}" if bad
                       else "does not mention the measured subject" if key != "analysis" and not on_topic(value, vocab)
                       else None)
            if problem:
                p.event("narrative", f"Model {key} rejected: {problem}; using computed template text")
                how = "model with template fallbacks"
                continue
            text[key] = value
    except LLMError as exc:
        p.event("narrative", f"Model text unavailable ({exc}); using computed template text")
    if inventory_conflicts(inv, title=text["title"]):
        text["title"] = template["title"]
        if inventory_conflicts(inv, title=text["title"]):
            text["title"] = (template["title"] + f" ({meta['source'][:30]})")[:100]
    jdump(run.folder / "text.json", {**text, "how": how, "facts_text": facts_text})
    p.event("narrative", f"Title: {text['title']} [{how}]")
    run.done("narrative", how=how, title=text["title"])


async def stage_package(run):
    result = jload(run.folder / "analysis.json")
    text = jload(run.folder / "text.json")
    acq, labels, roles = run.out("acquire"), run.out("roles")["labels"], run.out("roles")["roles"]
    cand, dl = acq["candidate"], acq["download"]
    pkg = run.folder / "package"
    cover_path = cover(result, text["title"], cand["source_name"], pkg / "cover.png")
    today = utcnow().strftime("%Y-%m-%d")
    used = ", ".join(f"{k} '{v}'" for k, v in (("time", roles.get("time")), ("group", roles.get("category")),
                                               ("measure", roles["measure"])) if v)
    methodology = (
        f"Data file: {cand['data_url']} (downloaded {today} UTC; SHA-256 {dl['sha256'][:16]}…; {dl['bytes']} bytes), "
        f"linked from {cand['found_on']}. Parsing: {'; '.join(acq['parse_notes'])}. Columns used: {used}. "
        f"Rows with a missing or non-numeric measure were removed ({result['facts']['rows_dropped']} rows); "
        f"{result['facts']['rows_used']} rows were used. Values were "
        f"{'summed' if roles['aggregate'] == 'sum' else 'averaged'} per "
        f"{result['time_field'] or 'group'}{' and group' if result['time_field'] and result['category_field'] else ''}. "
        "Charts are built by Datavera from the uploaded CSV files; every figure in the highlights is computed from "
        f"those files. Reuse terms: {cand['licence_label']} (stated on {cand['licence_page']}).")
    citation = f"{cand['source_name']}. {cand['page_title'][:150]}. {cand['page_url']} (accessed {today})."
    partial = result["facts"].get("incomplete_periods_removed") or []
    if partial:
        methodology += f" The current, incomplete period ({', '.join(partial)}) was excluded."
    removed = result["facts"].get("aggregate_groups_removed") or []
    if removed:
        methodology += (f" Rows for totals and regional aggregates were excluded so groups are not double-counted: "
                        f"{', '.join(removed[:12])}{' and others' if len(removed) > 12 else ''}.")
    upstream = (acq.get("archive") or {}).get("citation")
    if upstream:  # the publisher's own statement of where the numbers come from
        citation = f"{upstream} Retrieved from {cand['page_url']} (accessed {today})."
        methodology += f" The publisher's metadata cites: {upstream}"
    charts = []
    for ch in result["charts"]:
        ch = {k: v for k, v in ch.items() if v is not None}
        charts.append(ch)
    first_label = {}
    for ch in result["charts"]:
        first_label.setdefault(ch["file"], ch["label"])
    downloads = [{"file": key, "label": f"{first_label.get(key, key.replace('_', ' ').title())} (CSV)"[:120],
                  "access": "attribution"} for key in result["files"]]
    topic = run.out("plan").get("topic") if run.out("plan").get("how", "").startswith(("model", "fallback")) else None
    tags = list(dict.fromkeys(t for t in [topic, labels.get("measure_label"),
                                          labels.get("category_label"), cand["source_name"][:40]] if t))[:8]
    payload = {
        "title": text["title"], "subtitle": text.get("subtitle", ""), "tag": labels.get("tag", "Science"), "tags": tags,
        "summary": text["summary"][:399], "key_highlights": result["highlights"], "analysis": text["analysis"],
        "methodology": methodology[:3900], "source": cand["source_name"][:300], "source_url": cand["page_url"][:500],
        "citation": citation[:500], "time_period": result["time_period"][:50], "license": cand["licence"],
        "charts": charts, "downloads": downloads, "cover_file": "cover",
        "model": f"{os.getenv('OLLAMA_MODEL', 'qwen3.5:4b')} (local Ollama) + Datavera local agent",
        "visibility": "public", "access_tier": "free"}
    payload = {k: v for k, v in payload.items() if v not in ("", None, [])}
    files = dict(result["files"])
    files["cover"] = str(cover_path)
    jdump(pkg / "payload_template.json", payload)
    jdump(pkg / "files.json", files)
    run.progress.event("package", f"{len(charts)} charts, {len(files)} files, cover {cover_path.name}; licence {cand['licence']}")
    run.done("package", payload=str(pkg / "payload_template.json"), files=files)


def bind_uploads(payload, uploads):
    payload = json.loads(json.dumps(payload))
    for ch in payload["charts"]:
        ch["upload_id"] = uploads[ch.pop("file")]
    for d in payload.get("downloads", []):
        d["upload_id"] = uploads[d.pop("file")]
    payload["cover_upload_id"] = uploads[payload.pop("cover_file")]
    return payload


async def stage_validate(run, pub):
    p = run.progress
    pkg = run.out("package")
    template = jload(pkg["payload"])
    uploads = run.state.get("uploads", {})
    fresh = uploads and time.time() - uploads.get("_at", 0) < 20 * 3600
    if not fresh:
        uploads = {}
        for key, path in pkg["files"].items():
            name = ("cover.png" if key == "cover" else f"{key}.csv")
            uid, info = await pub.upload(path, name)
            uploads[key] = uid
            if key != "cover":
                insp = await pub.inspect(uid)
                cols = {str(c.get("name")) for c in (insp.get("columns") or insp.get("profile", {}).get("columns") or [])
                        if isinstance(c, dict)}
                local = set(pd.read_csv(path, nrows=1).columns)
                if cols and not local <= cols:
                    raise StageFailed(f"Datavera read different columns for {name}: {sorted(cols)} vs {sorted(local)}")
        uploads["_at"] = time.time()
        run.state["uploads"] = uploads
        run.save()
    payload = bind_uploads(template, uploads)
    jdump(run.folder / "package" / "payload.json", payload)
    ok, result = await pub.validate(payload)
    jdump(run.folder / "validation.json", result)
    for e in result.get("errors") or []:
        p.event("validate", f"ERROR {e.get('field')}: {e.get('message')}")
    for w in result.get("warnings") or []:
        p.event("validate", f"warning: {str(w)[:160]}")
    if not ok:
        raise StageFailed("Datavera validation failed; see validation.json. Nothing was created.")
    run.done("validate", quality_score_preview=result.get("quality_score_preview"))


async def stage_draft(run, pub):
    payload = jload(run.folder / "package" / "payload.json")
    acq = run.out("acquire")
    seed = f"{acq['candidate']['page_url']}|{acq['download']['sha256']}"
    prov = {"data_url": acq["candidate"]["data_url"], "data_sha256": acq["download"]["sha256"], "run_id": run.id}
    result, key = await pub.create(payload, seed, prov)
    did = result.get("dataset_id") or result.get("id")
    run.ledger.add(kind="candidate", data_url=acq["candidate"]["data_url"], page=acq["candidate"]["page_url"],
                   outcome="drafted", dataset_id=did, run=run.id)
    run.progress.event("draft", f"Draft {did} created as {run.state['owner']}. Review: {result.get('edit_url')}")
    jdump(run.folder / "draft.json", result)
    run.done("draft", dataset_id=did, edit_url=result.get("edit_url"))


async def stage_submit(run, pub):
    did = run.out("draft")["dataset_id"]
    warning = (jload(run.folder / "analysis.json") or {}).get("facts", {}).get("warning")
    if warning and not run.state.get("explicit_submit"):
        run.progress.event("REVIEW", f"Not submitted automatically: {warning} The draft is waiting for you: "
                                     f"{run.out('draft').get('edit_url')}  (to submit anyway: "
                                     f"python agent.py resume {run.id} --submit)")
        raise StageFailed("Left as a draft for your review (see REVIEW above)")
    record = await pub.submit(did)
    run.ledger.add(kind="candidate", data_url=run.out("acquire")["candidate"]["data_url"],
                   page=run.out("acquire")["candidate"]["page_url"], outcome="submitted", dataset_id=did,
                   status=record["status"], run=run.id)
    run.progress.event("submit", f"{did}: server status {record['status']!r} (moderated accounts go to review)")
    run.done("submit", dataset_id=did, status=record["status"])


# --------------------------------------------------------------------------- driver

async def execute(run, stop_after):
    p, cfg = run.progress, run.config
    run.inventory = ResearchInventory(ROOT)  # stops on a missing/corrupt baseline or event history
    rules = (connection.CODE / "INVENTORY_RULES.md").read_bytes()
    s = run.inventory.summary()
    p.event("catalogue", f"{s['total']} local records ({s['personal']} personal; companies {s['by_company']}); "
                         f"rules sha256 {hashlib.sha256(rules).hexdigest()[:12]}; no remote paging")
    p.event("run", f"Run {run.id}; folder {run.folder}; stop after '{stop_after}'; owner {run.state['owner']}")
    last = STAGES.index(stop_after)
    todo = [s for s in STAGES[:last + 1] if s not in run.state["done"]]
    if not todo:
        p.event("run", "Nothing to do: requested stages are already complete")
        return
    model = LocalModel(p, run.folder / "model_calls.jsonl", timeout=cfg["model_timeout_seconds"])
    needs_model = any(s in todo for s in ("plan", "select", "roles", "narrative"))
    if needs_model:
        try:
            present, names, loaded = await model.available()
        except Exception as exc:
            raise StageFailed(f"The {model.backend} model server is not reachable at {model.base_url} "
                              f"({type(exc).__name__}). {model.start_hint}") from exc
        if not present:
            raise StageFailed(f"{model.backend} model {model.model!r} not found at {model.base_url}; available: {names}")
        p.event("model", f"{model.model} available; loaded now: {[m.get('name') for m in loaded] or 'none (first call loads it)'}")
    async with contextlib.AsyncExitStack() as stack:
        browser = pub = None
        if any(s in todo for s in ("search", "inspect")):
            browser = await stack.enter_async_context(
                Browser(cfg["browser_mcp_url"], p, run.folder / "pages", engines=cfg["search_engines"],
                        templates=cfg.get("search_url_templates")))
            p.event("browser", "Playwright MCP connected; page reading uses DOM extraction (no click references)")
        if any(s in todo for s in ("validate", "draft", "submit")):
            client = await stack.enter_async_context(datavera_client_factory())
            pub = Publisher(ROOT, client, p, owner=run.state["owner"], username=cfg["username"])
            ident = await pub.check_account()
            p.event("datavera", f"Account {ident.get('username')}; submit mode: {ident.get('on_submit') or ident.get('publication_mode')}")
        for stage in todo:
            started = time.monotonic()
            p.event("stage", f"--- {stage} ---")
            p.busy(stage, stage)
            if stage == "plan":
                await stage_plan(run, model, run.inventory)
            elif stage == "search":
                await stage_search(run, browser)
            elif stage == "inspect":
                await stage_inspect(run, browser)
            elif stage == "select":
                await stage_select(run, model, run.inventory)
            elif stage == "acquire":
                await stage_acquire(run)
            elif stage == "roles":
                await stage_roles(run, model)
            elif stage == "analyse":
                await stage_analyse(run)
            elif stage == "narrative":
                await stage_narrative(run, model, run.inventory)
            elif stage == "package":
                await stage_package(run)
            elif stage == "validate":
                await stage_validate(run, pub)
            elif stage == "draft":
                await stage_draft(run, pub)
            elif stage == "submit":
                await stage_submit(run, pub)
            p.idle()
            p.event("stage", f"{stage} done in {time.monotonic() - started:.0f}s")
    p.event("run", f"Finished through '{stop_after}'. Model calls this session: {model.calls}.")
    summary_path = run.folder / "SUMMARY.md"
    summary_path.write_text(render_summary(run), encoding="utf-8")
    p.event("output", str(summary_path))


def render_summary(run, failed=False):
    st = run.state
    lines = [f"# Run {run.id}", "", f"Stages complete: {', '.join(st['done'])}", ""]
    acq = run.out("acquire")
    if acq:
        c = acq["candidate"]
        lines += [f"Source page: {c['page_url']}", f"Data file: {c['data_url']}",
                  f"Reuse terms: {c['licence_label']} ({c['licence']}), evidence on {c['licence_page']}", ""]
    if (run.folder / "text.json").exists():
        t = jload(run.folder / "text.json")
        lines += [f"Title: {t['title']}", f"Text source: {t['how']}", ""]
    if (run.folder / "analysis.json").exists() and jload(run.folder / "analysis.json")["facts"].get("warning"):
        lines += ["REVIEW: " + jload(run.folder / "analysis.json")["facts"]["warning"], ""]
    if (run.folder / "analysis.json").exists():
        lines += ["Highlights (computed):"] + [f"- {h}" for h in jload(run.folder / "analysis.json")["highlights"]] + [""]
    if run.out("validate"):
        lines.append(f"Datavera validation passed; quality preview {run.out('validate').get('quality_score_preview')}")
    if run.out("draft"):
        lines.append(f"Draft: {run.out('draft')['dataset_id']} — review at {run.out('draft')['edit_url']}")
    if run.out("submit"):
        lines.append(f"Submitted: status {run.out('submit')['status']}")
    nxt = next((s for s in STAGES if s not in st["done"]), None)
    if failed and nxt in ("plan", "search", "inspect", "select", "acquire"):
        lines += ["", "Next: start a new run (python agent.py run); rejected pages/files are recorded and will be skipped."]
    elif nxt:
        lines += ["", f"Next: python agent.py resume {run.id} --stop-after {nxt}" + (" --submit" if nxt == "submit" else "")]
    return "\n".join(lines) + "\n"


async def main_run(args):
    cfg = load_config()
    if args.command == "resume":
        if not (ROOT / "outputs" / "runs" / args.run_id).exists():
            raise SystemExit(f"No run folder {args.run_id}")
        run = Run(args.run_id, args, cfg, fresh=False)
        if args.redo_from:
            if run.state.get("outputs", {}).get("draft"):
                raise SystemExit("This run already created a draft; redoing earlier stages would diverge from it. Start a new run.")
            keep = STAGES[:STAGES.index(args.redo_from)]
            run.state["done"] = [s for s in run.state["done"] if s in keep]
            run.state["outputs"] = {k: v for k, v in run.state.get("outputs", {}).items() if k in keep}
            run.state.pop("failed", None)
            if STAGES.index(args.redo_from) <= STAGES.index("validate"):
                run.state.pop("uploads", None)  # files will change; upload fresh copies
            run.save()
            run.progress.event("run", f"Redoing from '{args.redo_from}' with the current code; earlier stages kept")
    else:
        run_id = utcnow().strftime("%Y%m%dT%H%M%SZ")
        run = Run(run_id, args, cfg, fresh=True)
    stop = "submit" if args.submit else (args.stop_after or cfg["stop_after_default"])
    run.state["explicit_submit"] = bool(args.submit)
    if stop not in STAGES:
        raise SystemExit(f"--stop-after must be one of {STAGES}")
    heartbeat = asyncio.create_task(run.progress.heartbeat())
    try:
        await asyncio.wait_for(execute(run, stop), timeout=cfg["run_minutes"] * 60)
        return 0
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
            reason = "interrupted"
        elif isinstance(exc, asyncio.TimeoutError):
            reason = f"run exceeded {cfg['run_minutes']} minutes"
        else:
            reason = f"{type(exc).__name__}: {safe_text(exc)[:600]}"
        run.state["failed"] = {"at": utcnow().isoformat(), "reason": reason,
                               "stage": next((s for s in STAGES if s not in run.state["done"]), None)}
        run.save()
        run.progress.event("failed", reason)
        (run.folder / "SUMMARY.md").write_text(render_summary(run, failed=True) + f"\nFAILED: {reason}\n", encoding="utf-8")
        run.progress.event("output", str(run.folder / "SUMMARY.md"))
        if isinstance(exc, (StageFailed, BrowserError, PublishError, LLMError, AcquireError, AnalysisError)):
            return 2
        raise
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


def status(args):
    runs = sorted((ROOT / "outputs" / "runs").glob("*"))
    if args.run_id:
        runs = [ROOT / "outputs" / "runs" / args.run_id]
    for folder in runs[-10:]:
        st = jload(folder / "state.json", {})
        fail = st.get("failed", {}).get("reason", "")
        print(f"{folder.name}: done={','.join(st.get('done', []))} {('FAILED ' + fail[:120]) if fail else ''}")
    led = Ledger(ROOT).rows()
    print(f"Ledger: {sum(r.get('kind') == 'candidate' for r in led)} data files, "
          f"{sum(r.get('kind') == 'page' for r in led)} rejected pages, {sum(r.get('kind') == 'topic' for r in led)} topics")
    return 0


def main():
    for stream in (sys.stdout, sys.stderr):  # scheduled/redirected runs on Windows use cp1252
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("doctor", help="Zero-mutation checks; --browser and --model are deeper")
    d.add_argument("--browser", action="store_true")
    d.add_argument("--model", action="store_true")
    d.add_argument("--datavera", action="store_true")
    d.add_argument("--url", default="https://psl.noaa.gov/marine-heatwaves/")
    d.add_argument("--query", default="marine heatwave data csv")
    for name in ("run", "resume"):
        r = sub.add_parser(name)
        if name == "resume":
            r.add_argument("run_id")
        r.add_argument("--stop-after", choices=STAGES)
        if name == "resume":
            r.add_argument("--redo-from", choices=STAGES[STAGES.index("roles"):STAGES.index("draft")],
                           help="Recompute from this stage (keeps search/download), e.g. after an update")
        r.add_argument("--submit", action="store_true", help="Submit the draft for review (implies --stop-after submit)")
        r.add_argument("--source-url", help="Skip search and inspect this source page")
        r.add_argument("--topic", help="Skip topic choice and search this topic")
        r.add_argument("--owner", help="publish as this company slug, or as your username (personal)")
    s = sub.add_parser("status")
    s.add_argument("run_id", nargs="?")
    args = parser.parse_args()
    (ROOT / "outputs").mkdir(exist_ok=True)
    if args.command == "status":
        return status(args)
    try:
        with FileLock(str(ROOT / "outputs" / "agent.lock"), timeout=0):
            if args.command == "doctor":
                import doctor
                return asyncio.run(doctor.main(args))
            return asyncio.run(main_run(args))
    except Timeout:
        print("Another agent or publisher run holds outputs\\agent.lock; not starting a second one.")
        return 3
    except KeyboardInterrupt:
        print("Stopped. Resume later with: python agent.py resume RUN_ID")
        return 130


if __name__ == "__main__":
    sys.exit(main())
