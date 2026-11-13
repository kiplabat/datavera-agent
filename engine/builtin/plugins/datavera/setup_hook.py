"""'Check and connect' for the Datavera plugin: checks the key with Datavera, lists who you can publish as,
optionally imports the command-line agent's catalogue and history, and builds the catalogue on first use.
Prints one `::result {json}` line. Never prints the key."""
import asyncio
import contextlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline_entry import ACCOUNT_URL, CODE


def done(**result):
    print("::result " + json.dumps(result, ensure_ascii=False), flush=True)
    sys.exit(0)


async def whoami(key):
    from fastmcp import Client
    async with Client(ACCOUNT_URL, auth=key) as client:
        result = await client.call_tool("whoami", {}, raise_on_error=False)
        if getattr(result, "is_error", False):
            raise PermissionError("Datavera refused the key")
        return result.data if getattr(result, "data", None) is not None else json.loads(result.content[0].text)


def import_cli_history(source, home):
    """Copy the earlier command-line agent's catalogue, ledger and receipts (never its .env)."""
    source = Path(source)
    copied = []
    for rel in ("inventory", "agent_state", Path("outputs") / "inventory_publisher"):
        src, dst = source / rel, home / rel
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dst)
            copied.append(str(rel))
    cfg = source / "agent_config.json"
    if cfg.exists() and not (home / "agent_config.json").exists():
        shutil.copy2(cfg, home / "agent_config.json")
        copied.append("agent_config.json")
    return copied


def main():
    settings = json.loads(os.environ.get("DVA_PLUGIN_SETTINGS") or "{}")
    home = Path(os.environ["DVA_PLUGIN_DATA"])
    home.mkdir(parents=True, exist_ok=True)
    key = (settings.get("api_key") or "").strip()
    if not key:
        done(ok=False, message="Paste your Datavera API key first, then press Check and connect.")
    if not key.startswith(("dv_sk_", "dv_co_")):
        done(ok=False, message="That doesn't look like a Datavera key: it should start with dv_sk_ (personal) "
                               "or dv_co_ (company).")
    try:
        identity = asyncio.run(whoami(key))
    except Exception as exc:
        done(ok=False, message=f"Datavera did not accept this key ({type(exc).__name__}). Check you copied all of "
                               "it. If you made a new key on datavera.io, the old one stopped working.")
    username = identity.get("username", "")
    scopes = set((identity.get("key") or {}).get("scopes") or [])
    if scopes and not {"read", "publish"} <= scopes:
        done(ok=False, message="This key can't publish. On datavera.io (Account → API Keys) tick both Read and "
                               "Publish, generate a new key and paste it here.")
    companies = [c.get("slug") for c in identity.get("companies", []) if c.get("slug")]
    owners = ([] if key.startswith("dv_co_") else [username]) + companies
    owner = settings.get("owner") if settings.get("owner") in owners else \
        (companies[0] if key.startswith("dv_co_") and companies else username)
    notes = []
    if settings.get("import_from"):
        src = Path(settings["import_from"])
        if (src / "inventory").exists():
            copied = import_cli_history(src, home)
            notes.append("Imported " + (", ".join(copied) if copied else "nothing new") + f" from {src}.")
        else:
            notes.append(f"{src} has no 'inventory' folder, so nothing was imported.")
    cfg_path = home / "agent_config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig")) if cfg_path.exists() else {}
    if cfg.get("username") and cfg["username"] != username:
        done(ok=False, message=f"This data folder belongs to Datavera user '{cfg['username']}', but the key is for "
                               f"'{username}'. Use that account's key.")
    cfg.update(username=username, owner=owner, companies=companies)
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

    os.environ.update({"DATAVERA_AGENT_HOME": str(home), "DATAVERA_MCP_URL": ACCOUNT_URL,
                       "DATAVERA_AUTH_MODE": "bearer", "DATAVERA_MCP_TOKEN": key})
    sys.path.insert(0, str(CODE))
    import inventory_store  # noqa: E402
    from connection import datavera_client  # noqa: E402
    if not list((home / "inventory").glob("baseline_*.json")):
        print("Building your catalogue of existing datasets (one time; about a minute)…", flush=True)

        async def boot():
            async with datavera_client() as client:
                folder = home / "outputs" / datetime.now(timezone.utc).strftime("inventory_bootstrap_%Y%m%dT%H%M%S_%fZ")
                return await inventory_store.bootstrap(client, home, folder, username, set(companies))
        (home / "outputs").mkdir(exist_ok=True)
        boot_result = asyncio.run(boot())
        notes.append(f"Catalogue built: {boot_result['datasets']} existing datasets remembered.")
    else:
        with contextlib.suppress(Exception):
            inv = inventory_store.Inventory(home)
            notes.append(f"Catalogue: {inv.summary()['total']} datasets remembered.")
    mode = identity.get("on_submit") or identity.get("publication_mode") or ""
    message = (f"Connected as {identity.get('display_name') or username}. Publishing as {owner}."
               + (f" On submit: {mode}." if mode else "") + (" " + " ".join(notes) if notes else ""))
    done(ok=True, message=message, settings={"owner": owner, "username": username, "companies": companies},
         options={"owner": owners})


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
