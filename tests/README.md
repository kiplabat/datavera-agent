# Developer tests (not needed to use the agent)

- `test_units.py ROOT DATA_DIR ZIP_DIR` - offline checks of licences, links, parsing, analysis, guards, covers:
  `python tests/test_units.py . tests/data tests/site/files`
- End-to-end with stand-ins: serve `tests/site` on 127.0.0.1:8765, run `fake_ollama.py 11999`, `fake_datavera.py 8790`
  and Playwright MCP on 8931, set `OLLAMA_BASE_URL=http://127.0.0.1:11999`, point `search_url_templates` at
  `http://127.0.0.1:8765/search.html?q={q}`, then `python tests/agent_wrapper.py . run`.
  `fake_datavera.py` mirrors response shapes of the live Datavera MCP; it is not the real server.
