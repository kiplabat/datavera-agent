# Datavera Agent — desktop app (Windows first)

A desktop app for local AI agents: chat, projects, memory, skills, plugins, connectors (MCP), harnesses and
scheduled tasks. **Models run only on this computer** (llama.cpp, CPU or GPU). Datavera ships as the built-in
plugin; the command-line agent in the repository root keeps working and is what the Datavera plugin runs.

```
app/                Electron shell (window, tray, start with Windows) + React UI (Vite)
  electron/         main.cjs (starts the engine, window, tray), preload.cjs (safe bridge)
  src/              the UI: pages/ (Chat, Projects, Models, Plugins, Skills, Harnesses, Schedules, Activity, Memory, Settings)
  scripts/          dev.mjs (development), prepare-resources.mjs (gathers what the installer ships)
  build/            icons
engine/             Python engine (FastAPI on 127.0.0.1 + a per-launch token) — see engine/README.md
```

## Run it in development (Windows, PowerShell)

Needs Python 3.12 and Node.js 20+.

```powershell
cd D:\datavera\datavera-agent\engine
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
cd ..\app
npm install
npm run dev
```

In development the engine runs from `engine\.venv` and there is no bundled llama.cpp yet. Either run
`node scripts\prepare-resources.mjs --skip-python` once (downloads the CPU build into `app\resources\llama\cpu`),
or install a runtime from **Models → Hardware & GPU** inside the app.

## Build the Windows installer

```powershell
cd D:\datavera\datavera-agent\app
npm install
npm run dist:win
```

`dist:win` does three things:
1. `vite build` — the UI into `app\dist`.
2. `prepare-resources.mjs --platform win` — copies the engine and the Datavera pipeline code into
   `app\resources\engine`, downloads the embeddable Python 3.12 (`python.org`) and installs the engine's packages
   into it (`pip --target … --platform win_amd64`), and downloads the llama.cpp CPU build of the pinned release
   (`engine\dva\versions.json`, currently `b12352`). Downloads are cached in `app\.cache`.
3. `electron-builder --win` — writes `app\release\DataveraAgent-Setup-2.0.0.exe` (per-user install, no admin
   rights, desktop + Start-menu shortcuts).

The installer is **not code-signed** yet, so Windows SmartScreen will warn ("More info → Run anyway") until a
signing certificate is added (`win.certificateFile` / Azure Trusted Signing in `electron-builder.yml`).

## Where things are on the user's computer

| What | Where |
|---|---|
| App | `%LOCALAPPDATA%\Programs\Datavera Agent` |
| Data (database, chats' workspaces, your skills/harnesses/plugins, logs) | `%APPDATA%\Datavera Agent` |
| Models | `%APPDATA%\Datavera Agent\models` by default; change in **Settings → Models folder** (e.g. `D:\models`) |
| Keys and tokens | Windows Credential Manager (service `DataveraAgent`) |
| Datavera pipeline data (catalogue, runs) | `%APPDATA%\Datavera Agent\plugin-data\datavera` |

## How the pieces talk

- Electron starts `python -m dva serve` with a random token in the environment, waits for `DVA_READY <port>`, and
  opens the window on `http://127.0.0.1:<port>/`. The engine refuses requests without the token and from other hosts.
- Closing the window hides it to the tray: chats, long tasks and schedules continue. **Quit** from the tray stops them.
- Node programs (the Playwright browser helper, `{node}` connectors) run on the app's own Electron
  (`ELECTRON_RUN_AS_NODE`), so users don't need Node.js installed.

## Not done yet (honest list)

- macOS build (the code paths exist; never run on a Mac).
- Code signing and auto-update.
- Vision models (images in chat) — llama.cpp supports them with an `mmproj` file; the UI does not yet.
