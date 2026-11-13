"""Settings = shipped defaults (agent_config.default.json) overlaid with the user's agent_config.json."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_config(require_user=True):
    cfg = {}
    for name in ("agent_config.default.json", "agent_config.json"):
        path = ROOT / name
        if path.exists():
            cfg.update(json.loads(path.read_text(encoding="utf-8-sig")))
    if require_user and not cfg.get("username"):
        raise SystemExit("This computer isn't connected to a Datavera account yet. Run the setup first "
                         "(python setup_account.py).")
    cfg["owner"] = cfg.get("owner") or cfg.get("username", "")
    return cfg
