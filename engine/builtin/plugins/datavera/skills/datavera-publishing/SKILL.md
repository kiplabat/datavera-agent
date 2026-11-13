---
name: datavera-publishing
description: Use when publishing, drafting, validating or fixing a dataset on Datavera by hand with the Datavera connector (not the automatic publisher pipeline).
---

# Publishing a dataset to Datavera with the connector

The Datavera connector's tools are named like `mcp__datavera_account__<tool>`. Changes ask the user first.

## Before anything
1. `whoami` — confirms the account, companies, limits and whether submissions are reviewed.
2. `get_publishing_guide` — the current rules for charts, licences and fields. Follow it over this summary.
3. Check for duplicates: `list_my_datasets` / `search_datasets` with the source URL or title.

## Building the dataset
- Every number in the title, summary, highlights and analysis must come from the data (compute it with
  run_python if needed). Exclude aggregate rows (World, regions) and incomplete current periods.
- Licence: only CC-BY-4.0, CC-BY-SA-4.0, CC0, ODbL, or `Custom` for clearly stated open terms (e.g. Open
  Government Licence, public-domain statements about the work). Non-commercial / no-derivatives / unstated
  terms: do not publish.
- Required fields: title, tag (category), summary, source, source_url, charts. Strongly recommended: cover
  (1800×1013 PNG), key_highlights, analysis, methodology, citation, license, geo_scope, time_period.

## Steps
1. Upload each chart CSV and the cover: `upload_file` (≤5 MB, base64; pace uploads — 10 per minute) or
   `upload_from_url`. Check columns with `inspect_upload`.
2. `validate_dataset` with the full payload. Fix every error; read the warnings and quality preview.
3. `create_dataset` (creates a draft; pass a stable `client_request_id` so a retry cannot duplicate it).
4. Show the user the edit link. Only `submit_dataset` when they agree (or their settings say auto-submit).
