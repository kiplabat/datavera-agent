"""Connect this computer's agent to a Datavera account. Asks a few simple questions.

    python setup_account.py            first-time setup (or to change account/key)
    python setup_account.py --check    just check the saved key still works

The key is typed in hidden, checked with Datavera, and saved only in the .env file next to this script.
"""
import argparse
import asyncio
import getpass
import json
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ACCOUNT_URL = "https://api.datavera.io/v1/mcp/account"
DEFAULT_MODEL = "qwen3.5:4b"


def say(text=""):
    print(text, flush=True)


def ask(question, default=""):
    try:
        answer = input(question).strip()
    except EOFError:
        answer = ""
    return answer or default


async def whoami(key):
    from fastmcp import Client
    async with Client(ACCOUNT_URL, auth=key) as client:
        result = await client.call_tool("whoami", {}, raise_on_error=False)
        if getattr(result, "is_error", False):
            raise RuntimeError("Datavera refused the key")
        data = result.data if getattr(result, "data", None) is not None else json.loads(result.content[0].text)
        return data


def read_env():
    values = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    return values


def write_env(values):
    path = ROOT / ".env"
    lines = ["# Private settings for the Datavera local agent. Never share this file.",
             *(f"{k}={v}" for k, v in values.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def get_key():
    for attempt in range(3):
        say()
        say("Paste your Datavera API key and press Enter.")
        say("(Nothing appears while you paste - that is normal, it keeps the key secret.)")
        key = getpass.getpass("API key: ").strip()
        if not key.startswith(("dv_sk_", "dv_co_")):
            say("That doesn't look like a Datavera key. It should start with dv_sk_ . Try again.")
            continue
        say("Checking the key with Datavera...")
        try:
            identity = asyncio.run(whoami(key))
        except Exception as exc:  # never print the key or the raw error (it may echo headers)
            say(f"That key did not work ({type(exc).__name__}). Check you copied all of it, or make a new one.")
            continue
        return key, identity
    raise SystemExit("Setup stopped: no working key. Make a new key on datavera.io (Account -> API Keys) and run setup again.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    env = read_env()

    if args.check:
        if not env.get("DATAVERA_MCP_TOKEN"):
            raise SystemExit("No key saved yet. Run: python setup_account.py")
        identity = asyncio.run(whoami(env["DATAVERA_MCP_TOKEN"]))
        say(f"OK: the saved key works for Datavera user '{identity.get('username')}'.")
        return

    say("=" * 64)
    say(" Connect the agent to your Datavera account")
    say("=" * 64)
    key, identity = get_key()
    username = identity.get("username", "")
    scopes = set((identity.get("key") or {}).get("scopes") or [])
    say(f"Hello {identity.get('display_name') or username}! The key works.")
    if scopes and not {"read", "publish"} <= scopes:
        raise SystemExit("This key can't publish. On datavera.io go to Account -> API Keys, tick BOTH 'Read' and "
                         "'Publish', press Generate key, and run setup again with the new key.")

    companies = [c.get("slug") for c in identity.get("companies", []) if c.get("slug")]
    owner = username
    if key.startswith("dv_co_") and companies:
        owner = companies[0]
        say(f"This is a company key, so datasets will be published as '{owner}'.")
    elif companies:
        say()
        say("Who should the datasets be published as?")
        options = [username] + companies
        for i, name in enumerate(options, 1):
            say(f"  {i}. {name}" + ("  (you, personally)" if i == 1 else "  (company)"))
        choice = ask("Type a number and press Enter [1]: ", "1")
        owner = options[int(choice) - 1] if choice.isdigit() and 1 <= int(choice) <= len(options) else username
    say(f"Datasets will be published as: {owner}")
    say(f"When a dataset is submitted, Datavera will: {identity.get('on_submit') or identity.get('publication_mode')}")

    say()
    say("Which subjects should the agent look for? Type a few, separated by commas,")
    say("or just press Enter for a ready-made mixed list.")
    topics_text = ask("Subjects: ")

    # agent_config.json holds only this person's choices; everything else comes from agent_config.default.json,
    # so updates can improve the defaults without overwriting anyone's settings.
    cfg_path, default_path = ROOT / "agent_config.json", ROOT / "agent_config.default.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig")) if cfg_path.exists() else {}
    cfg.update(username=username, owner=owner, companies=companies)
    if topics_text:
        topics = [t.strip() for t in topics_text.split(",") if len(t.strip()) > 2][:40]
        if topics:
            cfg["topics"] = topics
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

    write_env({"OLLAMA_MODEL": env.get("OLLAMA_MODEL", DEFAULT_MODEL),
               "OLLAMA_BASE_URL": env.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
               "DATAVERA_MCP_URL": ACCOUNT_URL, "DATAVERA_AUTH_MODE": "bearer", "DATAVERA_MCP_TOKEN": key})
    say("Saved your settings (the key is stored only on this computer, in the file .env).")

    inventory = ROOT / "inventory"
    if not list(inventory.glob("baseline_*.json")):
        say()
        say("Making a list of the datasets you already have, so the agent never repeats one...")
        os.environ["DATAVERA_MCP_TOKEN"] = key
        os.environ["DATAVERA_MCP_URL"] = ACCOUNT_URL
        os.environ["DATAVERA_AUTH_MODE"] = "bearer"
        import inventory_store
        from connection import datavera_client
        from datetime import datetime, timezone

        async def boot():
            async with datavera_client() as client:
                folder = ROOT / "outputs" / datetime.now(timezone.utc).strftime("inventory_bootstrap_%Y%m%dT%H%M%S_%fZ")
                return await inventory_store.bootstrap(client, ROOT, folder, username, set(companies))
        (ROOT / "outputs").mkdir(exist_ok=True)
        result = asyncio.run(boot())
        say(f"Done: {result['datasets']} existing datasets remembered.")
    say()
    say("All set.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nSetup cancelled. Nothing secret was saved unless it said 'Saved your settings'.")
