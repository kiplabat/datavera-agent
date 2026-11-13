"""Where things live.

DATA   = the user's data folder (database, workspaces, user skills/harnesses/plugins, logs).
MODELS = model files (can be moved to another drive in Settings; defaults to DATA/models).
ENGINE = this package's folder (built-in harnesses, skills and plugins ship beside it).
RES    = the packaged app's resources folder (bundled llama.cpp, embedded Python), if any.
"""
import os
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
BUILTIN = ENGINE / "builtin"


class Paths:
    def __init__(self, data_dir=None, resources_dir=None):
        default = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "DataveraAgent"
        self.data = Path(data_dir or os.environ.get("DVA_DATA_DIR") or default).resolve()
        res = resources_dir or os.environ.get("DVA_RESOURCES")
        self.resources = Path(res).resolve() if res else None
        for sub in ("workspaces", "skills", "harnesses", "plugins", "plugin-data", "jobs", "logs", "runtimes",
                    "uploads", "exports"):
            (self.data / sub).mkdir(parents=True, exist_ok=True)

    @property
    def db(self):
        return self.data / "datavera-agent.sqlite3"

    def models_dir(self, settings=None):
        custom = (settings or {}).get("models_dir")
        folder = Path(custom) if custom else self.data / "models"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def workspace(self, chat_id):
        folder = self.data / "workspaces" / chat_id
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def plugin_data(self, plugin_id):
        folder = self.data / "plugin-data" / plugin_id
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def bundled_runtime(self, backend):
        """llama.cpp shipped inside the installer (CPU build)."""
        if self.resources:
            folder = self.resources / "llama" / backend
            if folder.exists():
                return folder
        return None

    @property
    def python(self):
        """The Python that runs pipelines, skills' scripts and run_python: the engine's own interpreter."""
        return sys.executable
