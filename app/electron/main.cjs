// Datavera Agent — desktop shell.
// Starts the local engine (Python) with a random token, opens the window on it, keeps running in the tray so
// chats, long tasks and schedules continue when the window is closed, and can start with Windows.
const { app, BrowserWindow, Tray, Menu, ipcMain, dialog, shell, Notification, nativeImage, session } = require("electron");
const { spawn } = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const http = require("http");
const path = require("path");

const isDev = !app.isPackaged;
const token = crypto.randomBytes(24).toString("base64url");
let engine = null;
let enginePort = null;
let win = null;
let tray = null;
let quitting = false;
let restarts = 0;

const userData = app.getPath("userData");
const prefsPath = path.join(userData, "shell-prefs.json");
const logsDir = path.join(userData, "logs");
fs.mkdirSync(logsDir, { recursive: true });

function prefs() {
  try { return { keepInTray: true, openAtLogin: false, startHidden: false, ...JSON.parse(fs.readFileSync(prefsPath, "utf8")) }; }
  catch { return { keepInTray: true, openAtLogin: false, startHidden: false }; }
}
function savePrefs(p) { fs.writeFileSync(prefsPath, JSON.stringify({ ...prefs(), ...p }, null, 2)); }

function resources() {
  return isDev ? path.join(__dirname, "..", "resources") : process.resourcesPath;
}

// Which Python runs the engine: DVA_PYTHON, then the installer's own embedded Python, then engine\.venv
// (development), then a Python 3.12 already installed on this computer that has the engine's packages.
function pythonPath() {
  if (process.env.DVA_PYTHON) return process.env.DVA_PYTHON;
  const bundled = path.join(resources(), "python", process.platform === "win32" ? "python.exe" : "bin/python3");
  if (fs.existsSync(bundled)) return bundled;
  const venv = path.join(__dirname, "..", "..", "engine", ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  if (fs.existsSync(venv)) return venv;
  const { execFileSync } = require("child_process");
  const tries = process.platform === "win32" ? [["py", ["-3.12"]], ["python", []]] : [["python3.12", []], ["python3", []]];
  const check = "import sys, fastapi, uvicorn, fastmcp; assert sys.version_info[:2] == (3, 12); print(sys.executable)";
  for (const [cmd, pre] of tries) {
    try {
      const exe = execFileSync(cmd, [...pre, "-c", check], { windowsHide: true, timeout: 20000 }).toString().trim();
      if (exe) { log(`using installed Python: ${exe}`); return exe; }
    } catch {}
  }
  throw new Error("No Python found for the engine. Install the app with its installer, or for development run:\n" +
    "cd engine\npy -3.12 -m venv .venv\n.\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt");
}

function engineDir() {
  const bundled = path.join(resources(), "engine");
  return fs.existsSync(path.join(bundled, "dva")) ? bundled : path.join(__dirname, "..", "..", "engine");
}

function playwrightCli() {
  const candidates = [
    path.join(process.resourcesPath || "", "app.asar.unpacked", "node_modules", "@playwright", "mcp", "cli.js"),
    path.join(__dirname, "..", "node_modules", "@playwright", "mcp", "cli.js"),
  ];
  return candidates.find((p) => fs.existsSync(p)) || "";
}

function log(line) {
  fs.appendFileSync(path.join(logsDir, "shell.log"), `${new Date().toISOString()} ${line}\n`);
}

function startEngine() {
  return new Promise((resolve, reject) => {
    const uiDir = isDev ? "" : path.join(process.resourcesPath, "ui");
    const env = {
      ...process.env,
      DVA_TOKEN: token,
      DVA_UI_DIR: uiDir,
      DVA_NODE: process.execPath,
      DVA_NODE_IS_ELECTRON: "1",
      DVA_PLAYWRIGHT_MCP: playwrightCli(),
      PYTHONUTF8: "1",
      PYTHONIOENCODING: "utf-8",
      PYTHONNOUSERSITE: "1",
      NO_PROXY: "127.0.0.1,localhost",
    };
    const port = process.env.DVA_ENGINE_PORT || "0";
    const args = ["-m", "dva", "serve", "--port", port, "--data-dir", userData, "--resources", resources()];
    let python;
    try { python = pythonPath(); } catch (err) { reject(err); return; }
    log(`starting engine: ${python} ${args.join(" ")}`);
    engine = spawn(python, args, { cwd: engineDir(), env, windowsHide: true });
    const out = fs.createWriteStream(path.join(logsDir, "engine-console.log"), { flags: "a" });
    let ready = false;
    const timer = setTimeout(() => { if (!ready) reject(new Error("The engine did not start within 90 seconds")); }, 90000);
    engine.stdout.on("data", (buf) => {
      const text = buf.toString();
      out.write(text);
      const m = text.match(/DVA_READY (\d+)/);
      if (m && !ready) { ready = true; enginePort = Number(m[1]); clearTimeout(timer); resolve(enginePort); }
    });
    engine.stderr.on("data", (buf) => out.write(buf));
    engine.on("exit", (code) => {
      log(`engine exited with ${code}`);
      engine = null;
      if (!ready) { clearTimeout(timer); reject(new Error(`The engine stopped while starting (code ${code}). See logs/engine-console.log`)); return; }
      if (!quitting && restarts < 3) {
        restarts += 1;
        setTimeout(() => startEngine().then(() => win && win.loadURL(appUrl())).catch(showFatal), 1500);
      }
    });
  });
}

function appUrl() {
  return isDev ? "http://localhost:5173/" : `http://127.0.0.1:${enginePort}/`;
}

function iconPath() {
  const p = path.join(__dirname, "..", "build", process.platform === "win32" ? "icon.ico" : "icon.png");
  return fs.existsSync(p) ? p : path.join(__dirname, "..", "build", "icon.png");
}

function createWindow(show = true) {
  win = new BrowserWindow({
    width: 1320, height: 860, minWidth: 900, minHeight: 600, show: false, title: "Datavera Agent",
    backgroundColor: "#F7F5F2", icon: iconPath(), autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, "preload.cjs"), contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  win.loadURL(appUrl());
  win.once("ready-to-show", () => { if (show) win.show(); });
  win.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: "deny" }; });
  win.webContents.on("will-navigate", (e, url) => {
    if (!url.startsWith(appUrl())) { e.preventDefault(); shell.openExternal(url); }
  });
  win.on("close", (e) => {
    if (!quitting && prefs().keepInTray) {
      e.preventDefault();
      win.hide();
      if (!prefs().trayHintShown && Notification.isSupported()) {
        new Notification({ title: "Datavera Agent is still running", body: "Chats, tasks and schedules continue in the background. Use the tray icon to open or quit." }).show();
        savePrefs({ trayHintShown: true });
      }
    }
  });
}

function showWindow() {
  if (!win) createWindow(true);
  else { win.show(); win.focus(); }
}

function createTray() {
  const image = nativeImage.createFromPath(iconPath()).resize({ width: 16, height: 16 });
  tray = new Tray(image);
  tray.setToolTip("Datavera Agent");
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "Open Datavera Agent", click: showWindow },
    { type: "separator" },
    { label: "Quit (stops tasks and schedules)", click: () => { quitting = true; app.quit(); } },
  ]));
  tray.on("click", showWindow);
}

function showFatal(err) {
  log(`fatal: ${err && err.stack || err}`);
  dialog.showErrorBox("Datavera Agent could not start", `${err.message || err}\n\nLogs: ${logsDir}`);
  quitting = true;
  app.quit();
}

function stopEngine() {
  return new Promise((resolve) => {
    if (!engine || !enginePort) return resolve();
    const req = http.request({ host: "127.0.0.1", port: enginePort, path: "/api/shutdown", method: "POST",
      headers: { Authorization: `Bearer ${token}` }, timeout: 4000 }, () => resolve());
    req.on("error", () => resolve());
    req.on("timeout", () => { req.destroy(); resolve(); });
    req.end();
    setTimeout(() => { if (engine) { try { engine.kill(); } catch {} } resolve(); }, 6000);
  });
}

// ------------------------------------------------------------------ IPC for the window
ipcMain.on("dva:config", (e) => { e.returnValue = { token, port: enginePort, dev: isDev, version: app.getVersion(), platform: process.platform }; });
ipcMain.handle("dva:pick-folder", async () => {
  const r = await dialog.showOpenDialog(win, { properties: ["openDirectory", "createDirectory"] });
  return r.canceled ? null : r.filePaths[0];
});
ipcMain.handle("dva:pick-file", async (_e, filters) => {
  const r = await dialog.showOpenDialog(win, { properties: ["openFile"], filters: filters || [] });
  return r.canceled ? null : r.filePaths[0];
});
ipcMain.handle("dva:open-path", (_e, p) => shell.openPath(p));
ipcMain.handle("dva:show-item", (_e, p) => shell.showItemInFolder(p));
ipcMain.handle("dva:open-external", (_e, url) => { if (/^https?:\/\//.test(url)) shell.openExternal(url); });
ipcMain.handle("dva:notify", (_e, { title, body }) => {
  if (!Notification.isSupported()) return;
  const n = new Notification({ title, body, icon: iconPath() });
  n.on("click", showWindow);
  n.show();
});
ipcMain.handle("dva:get-shell-prefs", () => ({ ...prefs(), openAtLogin: app.getLoginItemSettings().openAtLogin }));
ipcMain.handle("dva:set-shell-prefs", (_e, p) => {
  savePrefs(p);
  if ("openAtLogin" in p) app.setLoginItemSettings({ openAtLogin: !!p.openAtLogin, args: ["--hidden"] });
  return prefs();
});
ipcMain.handle("dva:logs-folder", () => shell.openPath(logsDir));

// ------------------------------------------------------------------ lifecycle
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", showWindow);
  app.setAppUserModelId("io.datavera.agent");
  app.whenReady().then(async () => {
    try {
      await startEngine();
    } catch (err) {
      return showFatal(err);
    }
    if (!isDev) {
      // Content Security Policy for the window (production only: Vite's dev server needs inline scripts).
      session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
        callback({ responseHeaders: { ...details.responseHeaders, "Content-Security-Policy": [
          "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; font-src 'self' data:; connect-src 'self'; script-src 'self'"] } });
      });
    }
    createTray();
    const hidden = process.argv.includes("--hidden") || prefs().startHidden;
    createWindow(!hidden);
  });
  app.on("window-all-closed", (e) => { if (!quitting) e.preventDefault(); });
  app.on("before-quit", async (e) => {
    quitting = true;
    if (engine) {
      e.preventDefault();
      await stopEngine();
      engine = null;
      app.quit();
    }
  });
}
