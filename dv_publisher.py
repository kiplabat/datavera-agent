"""Datavera MCP publishing with write-ahead journals, server idempotency and local inventory events.

Library used by agent.py, plus a CLI compatible with the old publish_inventory.py commands:
    python dv_publisher.py validate payload.json [--owner YOUR_USERNAME_OR_COMPANY]
    python dv_publisher.py create payload.json [--owner YOUR_USERNAME_OR_COMPANY]
    python dv_publisher.py submit DATASET_ID
    python dv_publisher.py reconcile DATASET_ID
Field names follow the live schema (create: company, client_request_id; submit: id).
"""
import argparse
import asyncio
import base64
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from inventory_store import Inventory, normal, now, unbox

RECEIPTS = "inventory_publisher"


class PublishError(RuntimeError):
    pass


def dataset_id_of(result):
    nested = result.get("dataset") if isinstance(result.get("dataset"), dict) else {}
    return result.get("dataset_id") or result.get("id") or nested.get("dataset_id") or nested.get("id")


def record_for(detail, fallback=None, creator=None):
    data = {**(fallback or {}), **{k: v for k, v in detail.items() if v is not None}}
    identifier = dataset_id_of(data)
    if not identifier or not data.get("title"):
        raise PublishError("Confirmed dataset record is missing its ID/title")
    return {"dataset_id": identifier, "title": data["title"], "company": data.get("company") or None,
            "creator": creator or data.get("creator"), "status": data.get("status", "unknown"), "source": data.get("source"),
            "source_url": data.get("source_url"), "data_url": data.get("data_url"),
            "data_sha256": data.get("data_sha256"),
            "geo_scope": data.get("geo_scope") or data.get("geography"), "time_period": data.get("time_period"),
            "updated_at": data.get("updated_at"),
            "view_url": data.get("view_url") or "https://www.datavera.io/?dataset=" + str(identifier),
            "edit_url": data.get("edit_url"), "source_metadata_available": bool(data.get("source_url"))}


def inventory_conflicts(inv, title="", source_url="", data_url="", data_sha256="", exclude_id=None):
    """Hard duplicate signals: exact title, same original source page, same data file."""
    out = []
    for r in inv.records.values():
        if r.get("dataset_id") == exclude_id:
            continue
        why = []
        if title and normal(r.get("title")) == normal(title):
            why.append("same title")
        if source_url and r.get("source_url") and r["source_url"].rstrip("/") == source_url.rstrip("/"):
            why.append("same source page")
        if data_url and r.get("data_url") == data_url:
            why.append("same data file URL")
        if data_sha256 and r.get("data_sha256") == data_sha256:
            why.append("same data file content")
        if why:
            out.append({"dataset_id": r["dataset_id"], "title": r.get("title"), "status": r.get("status"),
                        "company": r.get("company"), "why": why})
    return out


class Publisher:
    def __init__(self, root, client, progress=None, owner=None, username=None):
        self.root = Path(root)
        self.client = client
        self.progress = progress
        cfg_path = self.root / "agent_config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig")) if cfg_path.exists() else {}
        self.username = username or cfg.get("username") or ""
        self.owner = owner or cfg.get("owner") or self.username
        if not self.username:
            raise PublishError("agent_config.json has no username; run the setup again")
        self.receipts = self.root / "outputs" / RECEIPTS
        self.receipts.mkdir(parents=True, exist_ok=True)
        self.inv = Inventory(self.root)
        self.tools = {}
        self._last_upload = 0.0

    def say(self, kind, msg):
        if self.progress:
            self.progress.event(kind, msg)
        else:
            print(f"[{kind}] {msg}", flush=True)

    async def call(self, name, args):
        if self.progress:
            self.progress.busy("datavera", name)
        try:
            result = await self.client.call_tool(name, args, raise_on_error=False)
        finally:
            if self.progress:
                self.progress.idle()
        if getattr(result, "is_error", False):
            text = " ".join(getattr(c, "text", "") for c in (result.content or []))
            raise PublishError(f"{name} failed: {text[:400]}")
        value = result.data if getattr(result, "data", None) is not None else None
        if value is None:
            text = " ".join(getattr(c, "text", "") for c in (result.content or []))
            value = json.loads(text) if text.strip().startswith(("{", "[")) else {"text": text}
        if isinstance(value, str):
            value = json.loads(value)
        if isinstance(value, dict) and value.get("status") == "success" and isinstance(value.get("data"), dict):
            value = value["data"]
        return value

    async def check_account(self):
        identity = await self.call("whoami", {})
        if identity.get("username") != self.username:
            raise PublishError(f"Connected account is {identity.get('username')!r}, expected {self.username!r}")
        if self.owner != self.username and not any(c.get("slug") == self.owner for c in identity.get("companies", [])):
            raise PublishError(f"No access to company {self.owner!r}")
        self.tools = {t.name: t for t in await self.client.list_tools()}
        need = {"upload_file", "inspect_upload", "validate_dataset", "create_dataset", "submit_dataset", "get_my_dataset"}
        missing = need - set(self.tools)
        if missing:
            raise PublishError(f"Datavera MCP is missing tools: {sorted(missing)}")
        return identity

    def schema_props(self, name):
        tool = self.tools.get(name)
        if not tool:
            return {}
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
        return schema.get("properties", {})

    # ------------------------------------------------------------------ uploads
    async def upload(self, path, filename=None):
        path = Path(path)
        data = path.read_bytes()
        if len(data) > 5_000_000:
            raise PublishError(f"{path.name} is {len(data)/1e6:.1f} MB; inline uploads are limited to 5 MB")
        wait = 6.5 - (time.monotonic() - self._last_upload)  # account limit: 10 uploads/minute
        if wait > 0:
            await asyncio.sleep(wait)
        result = await self.call("upload_file", {"filename": filename or path.name,
                                                 "content_base64": base64.b64encode(data).decode("ascii")})
        self._last_upload = time.monotonic()
        upload_id = result.get("upload_id") or result.get("id")
        if not upload_id:
            raise PublishError(f"upload_file returned no upload_id for {path.name}")
        self.say("upload", f"{filename or path.name}: {upload_id}")
        return upload_id, result

    async def inspect(self, upload_id):
        return await self.call("inspect_upload", {"upload_id": upload_id})

    # ------------------------------------------------------------------ validate / create / submit
    def prepare(self, payload):
        payload = dict(payload)
        if "company_slug" in payload:
            raise PublishError("Use the live payload field company, not company_slug")
        if self.owner == self.username:
            payload.pop("company", None)
        else:
            if payload.get("company") not in (None, self.owner):
                raise PublishError("Payload attribution conflicts with the configured owner")
            payload["company"] = self.owner
        return payload

    async def validate(self, payload):
        payload = self.prepare(payload)
        body = {k: v for k, v in payload.items() if k != "client_request_id"}
        result = await self.call("validate_dataset", body)
        ok = result.get("valid") is True and not result.get("errors")
        self.say("validate", f"valid={result.get('valid')}; {len(result.get('errors') or [])} errors; "
                             f"{len(result.get('warnings') or [])} warnings; quality preview {result.get('quality_score_preview')}")
        return ok, result

    def _journal(self, name, data):
        path = self.receipts / name
        with path.open("x", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        return path

    async def create(self, payload, idempotency_seed, provenance=None, allow_existing_match=False):
        """Create a draft once. Re-running with the same seed returns the recorded/server-side same draft."""
        payload = self.prepare(payload)
        key = hashlib.sha256(f"{self.owner}|{idempotency_seed}".encode()).hexdigest()
        requested = self.receipts / f"create_{key}.requested.json"
        confirmed = self.receipts / f"create_{key}.confirmed.json"
        if confirmed.exists():
            result = json.loads(confirmed.read_text(encoding="utf-8"))
            self.say("create", f"Already created earlier (receipt {confirmed.name}); not creating again")
            return result, key
        if "client_request_id" in self.schema_props("create_dataset"):
            payload["client_request_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, key))
        elif requested.exists():
            raise PublishError("An earlier create may have succeeded and the server offers no idempotency key. "
                               "Run inventory_store.py sync and reconcile before retrying.")
        prov = provenance or {}
        if not requested.exists():
            conflicts = inventory_conflicts(self.inv, payload.get("title", ""), payload.get("source_url", ""),
                                            prov.get("data_url", ""), prov.get("data_sha256", ""))
            if conflicts and not allow_existing_match:
                raise PublishError("Local catalogue already has a matching dataset: " + json.dumps(conflicts[:3]))
            self._journal(requested.name, {"operation": "create", "requested_at": now(), "payload": payload,
                                           "provenance": prov})
        else:
            self.say("create", "Retrying an unconfirmed create with the same client_request_id (server-side idempotent)")
        result = await self.call("create_dataset", payload)
        identifier = dataset_id_of(result)
        if not identifier:
            raise PublishError("create_dataset returned no dataset ID; inspect the receipt and reconcile")
        self._journal(confirmed.name, result)
        await self.record(identifier, "draft_created", {**payload, **result, **prov, "status": result.get("status") or "draft"},
                          {"receipt": str(requested), "server_result": result})
        return result, key

    async def record(self, identifier, event_type, fallback, evidence):
        try:
            detail = await self.call("get_my_dataset", {"id": identifier})
        except PublishError:
            detail = {}
        record = record_for(detail, fallback, self.username)
        # Unknown (None) fields must not erase values already recorded for this dataset.
        record = {k: v for k, v in record.items() if v is not None}
        changed = self.inv.append(record, event_type, evidence)
        self.say("inventory", f"{'Appended' if changed else 'Unchanged'}: {record['dataset_id']} ({record['status']})")
        return record

    async def submit(self, identifier):
        detail = await self.call("get_my_dataset", {"id": identifier})
        local = self.inv.records.get(identifier, {})
        record = record_for(detail, local, self.username)
        if record.get("company") and record.get("company") not in self.inv.companies:
            raise PublishError("Dataset owner is outside this local catalogue")
        if record["status"] not in ("draft", "rejected"):
            self.say("submit", f"Server status is {record['status']!r}; nothing to submit")
            self.inv.append({k: v for k, v in record.items() if v is not None}, "server_reconciled", {"observed_at": now()})
            return record
        dup = [c for c in inventory_conflicts(self.inv, record["title"], exclude_id=identifier)]
        if dup:
            raise PublishError("Another catalogue entry has the same title: " + json.dumps(dup[:2]))
        key = hashlib.sha256(f"submit|{identifier}|{record['status']}|{detail.get('updated_at')}".encode()).hexdigest()
        self._journal(f"submit_{key}.requested.json", {"operation": "submit", "requested_at": now(), "record": record})
        result = await self.call("submit_dataset", {"id": identifier})
        self._journal(f"submit_{key}.confirmed.json", result)
        return await self.record(identifier, "dataset_submitted", {**record, "status": result.get("status") or record["status"]},
                                 {"server_result": result})

    async def reconcile(self, identifier):
        detail = await self.call("get_my_dataset", {"id": identifier})
        record = record_for(detail, self.inv.records.get(identifier, {}), self.username)
        self.inv.append({k: v for k, v in record.items() if v is not None}, "server_reconciled", {"observed_at": now()})
        return record


async def _cli(args):
    from connection import ROOT, datavera_client
    async with datavera_client() as client:
        pub = Publisher(ROOT, client, owner=getattr(args, "owner", None))
        await pub.check_account()
        print("[inventory] " + json.dumps(pub.inv.summary()), flush=True)
        if args.command in ("validate", "create"):
            payload = json.loads(Path(args.payload).read_text(encoding="utf-8-sig"))
            ok, result = await pub.validate(payload)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if args.command == "create":
                if not ok:
                    raise PublishError("Server validation did not pass; nothing created")
                seed = payload.get("source_url", "") + "|" + hashlib.sha256(
                    json.dumps(payload, sort_keys=True).encode()).hexdigest()
                result, _ = await pub.create(payload, seed)
                print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "submit":
            print(json.dumps(await pub.submit(args.id), ensure_ascii=False, indent=2))
        else:
            print(json.dumps(await pub.reconcile(args.id), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    from filelock import FileLock
    from connection import ROOT
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "create"):
        p = sub.add_parser(name)
        p.add_argument("payload")
        p.add_argument("--owner", help="your username (personal) or a company slug; default from agent_config.json")
    for name in ("submit", "reconcile"):
        p = sub.add_parser(name)
        p.add_argument("id")
    args = parser.parse_args()
    (ROOT / "outputs").mkdir(exist_ok=True)
    try:
        with FileLock(str(ROOT / "outputs" / "agent.lock"), timeout=0):
            asyncio.run(_cli(args))
    except Exception as exc:
        print("Publisher stopped: " + type(exc).__name__ + ": " + str(exc)[:500])
        print("Inspect outputs\\inventory_publisher receipts and reconcile before retrying. Credentials omitted.")
        raise SystemExit(1)
