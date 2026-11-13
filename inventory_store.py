"""Immutable account snapshot + append-only local dataset events. No LLM paging.

Works for any Datavera account. The account (username) and the companies it may publish for are read
from agent_config.json ("username", "owner", "companies"). The newest inventory/baseline_*.json is the
snapshot; create the first one with:  python inventory_store.py bootstrap

    python inventory_store.py stats | search TEXT | check TITLE | export | sync | bootstrap
"""
import hashlib  # noqa: F401  (kept for compatibility with earlier imports)
import json
import os
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from filelock import FileLock

DEFAULT_ROOT = Path(__file__).resolve().parent


def now():
    return datetime.now(timezone.utc).isoformat()


def normal(text):
    text = unicodedata.normalize("NFKD", str(text or "")).casefold()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def unbox(result):
    if getattr(result, "is_error", False) or getattr(result, "isError", False):
        raise ValueError("MCP tool reported an error")
    value = result.data if hasattr(result, "data") else result
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object from MCP")
    if value.get("status") == "error":
        raise ValueError("MCP returned an error envelope")
    if value.get("status") == "success" and isinstance(value.get("data"), dict):
        return value["data"]
    return value


def account_settings(root):
    """(username, set of company slugs this catalogue covers) from agent_config.json."""
    path = Path(root) / "agent_config.json"
    cfg = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    username = cfg.get("username") or ""
    companies = set(cfg.get("companies") or [])
    owner = cfg.get("owner") or ""
    if owner and owner != username:
        companies.add(owner)
    return username, companies


class Inventory:
    def __init__(self, root=None):
        self.root = Path(root or DEFAULT_ROOT)
        self.folder = self.root / "inventory"
        self.username, self.companies = account_settings(self.root)
        baselines = sorted(self.folder.glob("baseline_*.json"))
        if not baselines:
            raise ValueError("No local catalogue yet. Create it once with: python inventory_store.py bootstrap")
        self.base_path = baselines[-1]
        self.events_path = self.folder / "events.jsonl"
        self.lock = FileLock(str(self.folder / "inventory.lock"), timeout=30)
        self.load()

    def load(self):
        with self.base_path.open(encoding="utf-8-sig") as f:
            self.base = json.load(f)
        rows = self.base.get("records", [])
        if not self.base.get("complete") or len(rows) != self.base.get("reported_total"):
            raise ValueError("Inventory bootstrap is missing or incomplete")
        self.records = {r["dataset_id"]: r for r in rows}
        if len(self.records) != len(rows):
            raise ValueError("Duplicate IDs in inventory bootstrap")
        scope = self.base.get("scope", {})
        if not self.username:
            self.username = scope.get("personal_creator", "")
        if not self.companies and scope.get("company"):
            self.companies = {scope["company"]}
        for r in rows:
            if r.get("company"):
                self.companies.add(r["company"])
        self.last_event_at = None
        if self.events_path.exists():
            # Lock protects readers from observing a partially appended line.
            with self.lock:
                with self.events_path.open(encoding="utf-8") as f:
                    for line in f:
                        event = json.loads(line)
                        record = event["record"]
                        self.records[record["dataset_id"]] = record
                        self.last_event_at = event["recorded_at"]
        return self

    def summary(self):
        rows = list(self.records.values())
        by_company = dict(Counter(r["company"] for r in rows if r.get("company")))
        return {"total": len(rows), "personal": sum(not r.get("company") for r in rows), "by_company": by_company,
                # older key names, kept for scripts written against them
                "company_datavera": by_company.get("datavera", 0), "personal_labatt": sum(not r.get("company") for r in rows),
                "statuses": dict(Counter(r.get("status", "unknown") for r in rows)),
                "snapshot_captured_at": self.base["captured_at"], "last_local_event_at": self.last_event_at,
                "scope": f"account {self.username or '?'} and companies {sorted(self.companies) or 'none'}; all statuses; "
                         "not other platform creators",
                "source_metadata_note": "Bootstrap listing provides IDs/titles/statuses, not full source metadata. "
                                        "Semantic duplicate checks are advisory."}

    def append(self, record, event_type, evidence=None):
        record = dict(record)
        if not record.get("dataset_id") or not record.get("title"):
            raise ValueError("A confirmed dataset ID and title are required")
        if record.get("company") not in (None, "") and record.get("company") not in self.companies:
            raise ValueError("Dataset is outside this inventory's authorised scope")
        with self.lock:
            # Re-read latest events inside this lock to avoid cross-process lost updates.
            latest = dict(self.records)
            if self.events_path.exists():
                with self.events_path.open(encoding="utf-8") as f:
                    for line in f:
                        prior = json.loads(line)
                        latest[prior["record"]["dataset_id"]] = prior["record"]
            old = latest.get(record["dataset_id"], {})
            merged = {**old, **record}
            if merged == old:
                self.records = latest
                return False
            event = {"schema_version": 1, "event_type": event_type, "recorded_at": now(),
                     "record": merged, "evidence": evidence or {}}
            with self.events_path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            latest[merged["dataset_id"]] = merged
            self.records = latest
            self.last_event_at = event["recorded_at"]
            return True

    def search(self, query, limit=12):
        query = normal(query)
        tokens = set(query.split())
        stop = {"the", "and", "of", "in", "a", "to", "for", "data", "dataset", "datasets", "with", "from", "by"}
        tokens -= stop
        matches = []
        for r in self.records.values():
            title = normal(r.get("title"))
            text = normal(" ".join(str(r.get(k) or "") for k in
                                   ("title", "source", "source_url", "geo_scope", "geography", "time_period")))
            overlap = len(tokens & set(text.split())) / max(1, len(tokens))
            similarity = SequenceMatcher(None, query, title).ratio()
            score = max(overlap, similarity) if query else 0
            if query and (overlap or similarity >= 0.42 or query in text):
                matches.append((score, r))
        matches.sort(key=lambda item: (-item[0], item[1].get("title", "")))
        return [{"score": round(score, 3), **{k: r.get(k) for k in
                 ("dataset_id", "title", "company", "status", "view_url", "source_url", "time_period")}}
                for score, r in matches[:max(1, min(int(limit), 30))]]

    def check(self, title, source_url="", exclude_id=None):
        self.load()
        exact = [r for r in self.records.values()
                 if r["dataset_id"] != exclude_id and normal(r.get("title")) == normal(title)]
        source_matches = [r for r in self.records.values() if source_url and
                          r["dataset_id"] != exclude_id and r.get("source_url") == source_url]
        possible = [r for r in self.search(title, 12) if r["dataset_id"] != exclude_id]
        return {"title": title, "source_url": source_url, "checked_at": now(),
                "decision": "duplicate_title" if exact else "no_exact_title_match",
                "exact_matches": [{k: r.get(k) for k in ("dataset_id", "title", "company", "status")} for r in exact],
                "same_source_matches": [{k: r.get(k) for k in ("dataset_id", "title", "company", "status")} for r in source_matches[:12]],
                "possible_matches": possible,
                "limitation": "No exact match does not prove semantic uniqueness; inspect related titles and source coverage."}

    def export(self, destination):
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=False)
        personal = self.username or "personal"
        for owner in sorted(self.companies) + [personal]:
            rows = [r for r in self.records.values() if (r.get("company") or personal) == owner]
            lines = [f"# {owner} dataset inventory", "", f"{len(rows)} unique datasets. All statuses included.", "",
                     "| Title | Status | ID | Link |", "|---|---|---|---|"]
            for r in sorted(rows, key=lambda row: row.get("title", "").casefold()):
                title = r["title"].replace("|", "\\|").replace("\n", " ")
                lines.append(f"| {title} | {r.get('status', 'unknown')} | {r['dataset_id']} | [Open]({r.get('view_url') or 'https://www.datavera.io/?dataset=' + r['dataset_id']}) |")
            (destination / (owner + "_datasets.md")).write_text("\n".join(lines) + "\n", encoding="utf-8")
        (destination / "inventory_summary.json").write_text(json.dumps(self.summary(), indent=2), encoding="utf-8")
        return destination


async def list_all(client, output):
    """Deterministic pagination of list_my_datasets (all statuses), saving every page as evidence."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    seen, expected, page, identities = {}, None, 1, []
    while True:
        result = unbox(await client.call_tool("list_my_datasets", {"page": page, "per_page": 50}))
        if expected is None:
            expected = result["total"]
        if result.get("total") != expected or result.get("page") != page:
            raise ValueError("Inventory changed during refresh; no cache changes applied. Retry later.")
        rows = result["datasets"]
        (output / f"page_{page}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        for row in rows:
            seen[row["dataset_id"]] = row
        identities.extend(r["dataset_id"] for r in rows)
        print(f"Catalogue page {page}: {len(seen)}/{expected}", flush=True)
        if not result["has_more"]:
            break
        if not rows or page >= 10000:
            raise ValueError("Invalid pagination; no cache changes applied")
        page += 1
    if len(seen) != expected or len(identities) != len(seen):
        raise ValueError("Missing/repeated IDs during pagination; no cache changes applied")
    return seen, expected, page


async def sync_inventory(client, inventory, output):
    """One explicit deterministic refresh; no model, no per-run automatic paging."""
    seen, expected, pages = await list_all(client, output)
    for row in seen.values():
        inventory.append(row, "server_sync", {"refresh_folder": str(output)})
    # Removed server records remain locally as evidence; never erase historical entries.
    return {"server_total": expected, "local_total": len(inventory.records), "pages": pages}


async def bootstrap(client, root, output, username, companies):
    """Create the first immutable snapshot for a new installation."""
    folder = Path(root) / "inventory"
    folder.mkdir(exist_ok=True)
    if list(folder.glob("baseline_*.json")):
        raise ValueError("A catalogue snapshot already exists; use 'sync' to refresh it")
    seen, expected, pages = await list_all(client, output)
    records = []
    for row in seen.values():
        records.append({**row, "creator": username, "owner_scope": "company" if row.get("company") else "personal",
                        "source_url": row.get("source_url"), "source_metadata_available": bool(row.get("source_url"))})
    stamp = datetime.now(timezone.utc)
    base = {"schema_version": 1, "scope": {"company": sorted(companies)[0] if companies else None,
                                           "companies": sorted(companies), "personal_creator": username,
                                           "status": "all", "other_creators_included": False},
            "captured_at": stamp.isoformat(), "complete": True, "reported_total": expected, "pages_fetched": pages,
            "page_size": 50, "records": records}
    path = folder / f"baseline_{stamp.strftime('%Y%m%d')}.json"
    path.write_text(json.dumps(base, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"baseline": str(path), "datasets": expected}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["stats", "search", "check", "export", "sync", "bootstrap"])
    parser.add_argument("text", nargs="?", default="")
    args = parser.parse_args()
    if args.command in ("sync", "bootstrap"):
        import asyncio
        from connection import datavera_client

        async def main():
            username, companies = account_settings(DEFAULT_ROOT)
            async with datavera_client() as client:
                identity = unbox(await client.call_tool("whoami", {}))
                if username and identity.get("username") != username:
                    raise ValueError(f"The API key belongs to {identity.get('username')!r}, but agent_config.json "
                                     f"says {username!r}")
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
                if args.command == "bootstrap":
                    folder = DEFAULT_ROOT / "outputs" / f"inventory_bootstrap_{stamp}"
                    print(json.dumps(await bootstrap(client, DEFAULT_ROOT, folder, identity.get("username"), companies)))
                else:
                    folder = DEFAULT_ROOT / "outputs" / f"inventory_sync_{stamp}"
                    print(await sync_inventory(client, Inventory(), folder))
        asyncio.run(main())
    else:
        inv = Inventory()
        if args.command == "stats":
            print(json.dumps(inv.summary(), indent=2))
        elif args.command == "search":
            print(json.dumps(inv.search(args.text), ensure_ascii=False, indent=2))
        elif args.command == "check":
            print(json.dumps(inv.check(args.text), ensure_ascii=False, indent=2))
        else:
            print(inv.export(DEFAULT_ROOT / "outputs" / datetime.now(timezone.utc).strftime("inventory_export_%Y%m%dT%H%M%S_%fZ")))
