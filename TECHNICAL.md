# Datavera local agent — technical notes

## Pipeline

`agent.py run` executes fixed, checkpointed stages. Python does the browsing, downloading, arithmetic,
charts, cover and Datavera calls. The local model (Ollama, default `qwen3.5:4b`) answers at most four
bounded questions per run, each constrained by a JSON schema whose choices come from real evidence, and
each with a non-model fallback.

```
plan → search → inspect → select → acquire → roles → analyse → narrative → package → validate → draft → submit
 (Q1)                       (Q2)              (Q3)               (Q4)
```

| Stage | What happens |
|---|---|
| plan | Picks a topic from `topics` (skips recent ones and topics the catalogue already covers); model writes a search query |
| search | DuckDuckGo/Bing results pages in a real browser (Playwright MCP); one extra query restricted to an open-data portal (rotating) |
| inspect | Each page read with one DOM extraction (`browser_evaluate`: includes hidden tabs). Finds CSV/TSV/XLSX/JSON/ZIP links and reuse terms (on the page or one linked terms page); follows one "Download" page if needed |
| select | Drops candidates matching the catalogue (same source page, file URL, file hash or similar title); model picks among ≤ 5 |
| acquire | Downloads (25 MB cap), unzips (60 MB cap), parses, profiles; tries up to 3 candidates |
| roles | Model picks measure / time / grouping / sum-or-mean from the real column names; guards fix mixing entities and off-topic labels |
| analyse | pandas: drops the incomplete current period and aggregate rows (World, continents, income groups, non-ISO rows larger than any country), builds chart CSVs, facts and highlights |
| narrative | Model writes title/subtitle/summary/analysis from the computed facts only; any field with a number not in the facts, or not about the subject, is replaced by computed template text |
| package | Cover PNG (1800×1013, #F7F5F2), chart specs, methodology (file URL, SHA-256, steps, exclusions), citation |
| validate | Uploads files (`upload_file`), checks columns (`inspect_upload`), `validate_dataset` |
| draft | `create_dataset` with `client_request_id` = hash(source page + file hash): retries can't duplicate |
| submit | `submit_dataset`; skipped (left as draft) when the analysis raised a REVIEW warning, unless `--submit` was given |

Every stage writes its result into `outputs/runs/<RUN_ID>/state.json`; `resume` continues from the next stage.

## Commands

| Command | |
|---|---|
| `python agent.py run` | One run, to `stop_after_default` (default `submit`) |
| `python agent.py run --stop-after validate` | Stop after any stage |
| `python agent.py run --topic "…"` / `--source-url URL` | Choose the subject / skip search and inspect one page |
| `python agent.py run --owner SLUG` | Publish as a company (or your username for personal) |
| `python agent.py resume RUN_ID [--stop-after S] [--submit]` | Continue a run |
| `python agent.py resume RUN_ID --redo-from analyse` | Recompute analysis→validate with current code, keeping the download |
| `python agent.py status` | Recent runs and ledger size |
| `python agent.py doctor [--browser] [--model] [--datavera]` | Checks; never publishes |
| `python setup_account.py [--check]` | Connect/replace the API key, owner, topics |
| `python inventory_store.py stats / search TEXT / export / sync / bootstrap` | Local catalogue |
| `python dv_publisher.py validate|create payload.json`, `submit ID`, `reconcile ID` | Hand-made payloads |
| `run-agent.ps1` / `run-agent.sh` | Start Ollama + Playwright MCP if needed, run, show the result, clean up |

Only one run at a time (`outputs/agent.lock`).

## Settings

`agent_config.default.json` (shipped; replaced by updates) is overlaid by `agent_config.json` (yours; only
your choices). Keys: `username`, `owner` (your username = personal, or a company slug), `companies`,
`topics`, `stop_after_default` (`submit`/`draft`/`validate`), `search_engines`, `portal_sites`, `avoid_sites`,
`max_result_pages`, `max_pages_total`, `browse_minutes`, `run_minutes`, `model_timeout_seconds`,
`max_download_mb`, `seed_cooldown_runs`. `.env` holds `DATAVERA_MCP_TOKEN` (your key), `DATAVERA_MCP_URL`,
`OLLAMA_MODEL`, `OLLAMA_BASE_URL`.

## Files a run leaves (`outputs/runs/<RUN_ID>/`)

`SUMMARY.md`, `progress.log`, `events.jsonl`, `model_calls.jsonl` (timing per model call), `search_results.json`,
`pages/page_NN.json`, `pages_inspected.json` (accept/reject reason per page), `candidates.json`, `data/`,
`analysis.json`, `text.json`, `package/` (chart CSVs, cover, payload), `validation.json`, `draft.json`.
`agent_state/ledger.jsonl` remembers topics, rejected pages and every data file tried.
`inventory/` holds the catalogue snapshot (`baseline_*.json`) and `events.jsonl` (confirmed IDs/statuses).
`outputs/inventory_publisher/` holds create/submit receipts.

## Licences

CC BY 4.0, CC BY-SA 4.0, CC0, ODbL are mapped by name (including "Creative Commons Attribution International").
Open Government Licence, CC BY 2.x/3.0, PDDL, "Creative Commons Attribution" without a version and explicit
public-domain statements about the work ("work of the U.S. government", "these data are in the public domain")
become `Custom`. Non-commercial, no-derivatives, informal ("freely available"), or no stated terms → rejected.

## Known limits

- Search engines may show consent pages/CAPTCHAs; the run reports it rather than bypassing.
- Download links created only by JavaScript after a click are not seen.
- Analysis is one measure over time and/or ranked by one grouping; drafts are simpler than hand-curated datasets.
- Novelty checks are lexical/identity (titles, URLs, file hashes), not semantic.
- The Mac scripts have not yet been run on a real Mac.
- On a CPU-only laptop the model reads prompts at ~15 tokens/s; a run spends ~4–5 minutes in the model.
