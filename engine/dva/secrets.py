"""Secrets (API keys, tokens) go to the operating system's credential store: Windows Credential Manager,
macOS Keychain. If no store is available (some Linux setups), they fall back to a file only this user can read,
and the UI says so. Secrets are never returned to the UI after they are saved."""
import json
import os
import stat
from pathlib import Path

SERVICE = "DataveraAgent"


class Secrets:
    def __init__(self, data_dir):
        self.fallback_path = Path(data_dir) / "secrets.json"
        self.backend = "file"
        if os.environ.get("DVA_SECRETS") == "file":
            return
        try:
            import keyring
            keyring.set_password(SERVICE, "__probe__", "ok")
            if keyring.get_password(SERVICE, "__probe__") == "ok":
                keyring.delete_password(SERVICE, "__probe__")
                self.backend = type(keyring.get_keyring()).__name__
                self._kr = keyring
        except Exception:
            self.backend = "file"

    @property
    def secure(self):
        return self.backend != "file"

    def _load(self):
        if self.fallback_path.exists():
            return json.loads(self.fallback_path.read_text(encoding="utf-8"))
        return {}

    def _save(self, values):
        self.fallback_path.write_text(json.dumps(values), encoding="utf-8")
        if os.name != "nt":
            self.fallback_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def get(self, name):
        if self.secure:
            try:
                return self._kr.get_password(SERVICE, name)
            except Exception:
                return None
        return self._load().get(name)

    def set(self, name, value):
        if not value:
            return self.delete(name)
        if self.secure:
            self._kr.set_password(SERVICE, name, value)
        else:
            values = self._load()
            values[name] = value
            self._save(values)

    def delete(self, name):
        if self.secure:
            try:
                self._kr.delete_password(SERVICE, name)
            except Exception:
                pass
        else:
            values = self._load()
            values.pop(name, None)
            self._save(values)

    def has(self, name):
        return bool(self.get(name))
