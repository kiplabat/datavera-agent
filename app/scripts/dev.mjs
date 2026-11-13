// Development: Vite (hot reload) + Electron. Electron starts the engine from ../engine with engine/.venv.
//   cd engine && py -3.12 -m venv .venv && .venv\Scripts\pip install -r requirements.txt
//   cd app && npm run dev
import { spawn } from "node:child_process";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

const app = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const env = { ...process.env, DVA_ENGINE_PORT: process.env.DVA_ENGINE_PORT || "8765" };
const npx = process.platform === "win32" ? "npx.cmd" : "npx";
const vite = spawn(npx, ["vite"], { cwd: app, env, stdio: "inherit", shell: process.platform === "win32" });

function waitFor(url, tries = 120) {
  return new Promise((resolve, reject) => {
    const attempt = (n) => http.get(url, () => resolve()).on("error", () => n > 0 ? setTimeout(() => attempt(n - 1), 500) : reject(new Error("Vite did not start")));
    attempt(tries);
  });
}

await waitFor("http://localhost:5173/");
const electron = spawn(npx, ["electron", "."], { cwd: app, env, stdio: "inherit", shell: process.platform === "win32" });
electron.on("exit", () => { vite.kill(); process.exit(0); });
