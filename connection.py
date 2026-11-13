"""Connection configuration. Never print the URL, token, or raw exceptions."""
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastmcp import Client

CODE = Path(__file__).resolve().parent
# Where settings, the catalogue and run outputs live. The desktop app sets DATAVERA_AGENT_HOME to its own data
# folder; the command-line install keeps everything beside the code, as before.
ROOT = Path(os.environ.get("DATAVERA_AGENT_HOME") or CODE).resolve()
load_dotenv(ROOT / ".env")


def datavera_client():
    url = os.environ.get("DATAVERA_MCP_URL", "").strip()
    if urlsplit(url).scheme != "https":
        raise ValueError("Configure the actual HTTPS Datavera MCP URL in .env")
    mode = os.environ.get("DATAVERA_AUTH_MODE", "bearer")
    if mode == "bearer":
        token = os.environ.get("DATAVERA_MCP_TOKEN", "").strip()
        if not token:
            raise ValueError("Configure a separately provisioned Datavera token")
        return Client(url, auth=token)
    if mode == "url":
        return Client(url)
    raise ValueError("Supported starter auth modes: bearer or url; see README for OAuth")
