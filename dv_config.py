"""Settings = shipped defaults (agent_config.default.json) overlaid with the user's agent_config.json."""
import json
import os
from pathlib import Path

CODE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("DATAVERA_AGENT_HOME") or CODE).resolve()  # the user's settings (see connection.py)


def load_config(require_user=True):
    cfg = {}
    for path in (CODE / "agent_config.default.json", ROOT / "agent_config.json"):
        if path.exists():
            cfg.update(json.loads(path.read_text(encoding="utf-8-sig")))
    if require_user and not cfg.get("username"):
        raise SystemExit("This computer isn't connected to a Datavera account yet. Run the setup first "
                         "(python setup_account.py).")
    cfg["owner"] = cfg.get("owner") or cfg.get("username", "")
    return cfg
