// Gathers everything the installer ships besides the Electron app itself, into app/resources/:
//   engine/  the Python engine + built-in harnesses/skills/plugins (+ the Datavera pipeline code)
//   python/  an embedded Python 3.12 for Windows with the engine's packages pre-installed
//   llama/cpu/  the llama.cpp CPU build (GPU builds are downloaded from inside the app when wanted)
// Usage: node scripts/prepare-resources.mjs [--platform win] [--skip-python] [--skip-llama]
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import extract from "extract-zip";

const here = path.dirname(fileURLToPath(import.meta.url));
const app = path.resolve(here, "..");
const repo = path.resolve(app, "..");
const engineSrc = path.join(repo, "engine");
const out = path.join(app, "resources");
const args = process.argv.slice(2);
const platform = args.includes("--platform") ? args[args.indexOf("--platform") + 1] : (process.platform === "win32" ? "win" : process.platform);
const versions = JSON.parse(fs.readFileSync(path.join(engineSrc, "dva", "versions.json"), "utf8"));
const cache = path.join(app, ".cache");
fs.mkdirSync(cache, { recursive: true });

const PIPELINE_FILES = ["agent.py", "connection.py", "doctor.py", "dv_acquire.py", "dv_analysis.py", "dv_browser.py",
  "dv_config.py", "dv_discovery.py", "dv_llm.py", "dv_progress.py", "dv_publisher.py", "inventory_relevance.py",
  "inventory_store.py", "setup_account.py", "INVENTORY_RULES.md", "agent_config.default.json", "LICENSE"];

function step(msg) { console.log(`\n== ${msg}`); }

function copyDir(src, dest, skip = (p) => false) {
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const s = path.join(src, entry.name);
    if (skip(s, entry)) continue;
    const d = path.join(dest, entry.name);
    if (entry.isDirectory()) copyDir(s, d, skip);
    else fs.copyFileSync(s, d);
  }
}

async function download(url, dest) {
  if (fs.existsSync(dest)) { console.log(`   cached ${path.basename(dest)}`); return dest; }
  console.log(`   downloading ${url}`);
  const r = await fetch(url, { headers: { "User-Agent": "datavera-agent-build" } });
  if (!r.ok) throw new Error(`HTTP ${r.status} for ${url}`);
  const buf = Buffer.from(await r.arrayBuffer());
  fs.writeFileSync(dest + ".part", buf);
  fs.renameSync(dest + ".part", dest);
  return dest;
}

function findPython() {
  const tries = process.platform === "win32" ? [["py", ["-3.12"]], ["python", []]] : [["python3.12", []], ["python3", []]];
  for (const [cmd, pre] of tries) {
    try {
      const v = execFileSync(cmd, [...pre, "-c", "import sys;print('%d.%d'%sys.version_info[:2])"]).toString().trim();
      if (v === "3.12") return [cmd, pre];
    } catch {}
  }
  throw new Error("Python 3.12 is needed to prepare the installer (it installs the engine's packages). Install it from python.org or with: winget install Python.Python.3.12");
}

async function engine() {
  step("Engine");
  const dest = path.join(out, "engine");
  fs.rmSync(dest, { recursive: true, force: true });
  const skip = (p, e) => e.name === "__pycache__" || e.name === "tests" || e.name === ".venv" || e.name.endsWith(".pyc") || e.name === "pipeline";
  copyDir(path.join(engineSrc, "dva"), path.join(dest, "dva"), skip);
  copyDir(path.join(engineSrc, "builtin"), path.join(dest, "builtin"), skip);
  fs.copyFileSync(path.join(engineSrc, "requirements.txt"), path.join(dest, "requirements.txt"));
  const pipe = path.join(dest, "builtin", "plugins", "datavera", "pipeline");
  fs.mkdirSync(pipe, { recursive: true });
  for (const f of PIPELINE_FILES) {
    if (fs.existsSync(path.join(repo, f))) fs.copyFileSync(path.join(repo, f), path.join(pipe, f));
    else console.warn(`   (missing pipeline file ${f})`);
  }
  console.log(`   engine + Datavera pipeline copied to ${dest}`);
}

async function python() {
  step("Embedded Python");
  const dest = path.join(out, "python");
  if (platform !== "win") { console.log("   skipped (only the Windows installer embeds Python; dev uses engine/.venv)"); return; }
  const v = versions.python_embed;
  const zip = await download(`https://www.python.org/ftp/python/${v}/python-${v}-embed-amd64.zip`, path.join(cache, `python-${v}-embed-amd64.zip`));
  fs.rmSync(dest, { recursive: true, force: true });
  await extract(zip, { dir: dest });
  const pth = fs.readdirSync(dest).find((f) => f.endsWith("._pth"));
  const zipName = fs.readdirSync(dest).find((f) => /^python\d+\.zip$/.test(f));
  fs.writeFileSync(path.join(dest, pth), [zipName, ".", "Lib\\site-packages", "..\\engine", "import site", ""].join("\r\n"));
  const site = path.join(dest, "Lib", "site-packages");
  fs.mkdirSync(site, { recursive: true });
  const [cmd, pre] = findPython();
  console.log("   installing engine packages for Windows / Python 3.12 (a few minutes)…");
  execFileSync(cmd, [...pre, "-m", "pip", "install", "--disable-pip-version-check", "--no-compile",
    "--target", site, "--platform", "win_amd64", "--python-version", "3.12", "--implementation", "cp",
    "--only-binary=:all:", "-r", path.join(engineSrc, "requirements.txt")], { stdio: "inherit" });
  console.log(`   Python ready in ${dest}`);
}

async function llama() {
  step(`llama.cpp ${versions.llama_cpp_tag} (CPU build)`);
  const key = platform === "win" ? "windows" : (platform === "darwin" ? "darwin" : "linux");
  const backend = key === "darwin" ? "metal" : "cpu";
  const pats = versions.backend_assets[key][backend];
  const rel = await (await fetch(`https://api.github.com/repos/${versions.llama_cpp_repo}/releases/tags/${versions.llama_cpp_tag}`,
    { headers: { "User-Agent": "datavera-agent-build", Accept: "application/vnd.github+json" } })).json();
  const asset = (rel.assets || []).find((a) => pats.every((p) => a.name.includes(p)) && a.name.endsWith(".zip") && !a.name.startsWith("cudart"));
  if (!asset) throw new Error(`No ${backend} asset in llama.cpp ${versions.llama_cpp_tag} matching ${pats.join(" + ")}`);
  const zip = await download(asset.browser_download_url, path.join(cache, asset.name));
  const dest = path.join(out, "llama", backend);
  fs.rmSync(dest, { recursive: true, force: true });
  const tmp = dest + ".tmp";
  fs.rmSync(tmp, { recursive: true, force: true });
  await extract(zip, { dir: tmp });
  const exe = process.platform === "win32" || platform === "win" ? "llama-server.exe" : "llama-server";
  const find = (dir) => {
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, e.name);
      if (e.isFile() && e.name === exe) return dir;
      if (e.isDirectory()) { const f = find(p); if (f) return f; }
    }
    return null;
  };
  const folder = find(tmp);
  if (!folder) throw new Error("llama-server was not found in the download");
  fs.renameSync(folder, dest);
  fs.rmSync(tmp, { recursive: true, force: true });
  console.log(`   ${asset.name} → ${dest}`);
}

fs.mkdirSync(out, { recursive: true });
await engine();
if (!args.includes("--skip-python")) await python();
if (!args.includes("--skip-llama")) await llama();
for (const d of ["python", "llama"]) fs.mkdirSync(path.join(out, d), { recursive: true });
console.log("\nResources ready in app/resources");
