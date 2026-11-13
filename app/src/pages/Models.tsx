import React, { useEffect, useState } from "react";
import { Cpu, Download, Gauge, HardDrive, MemoryStick, Play, Search, Star, Trash2, X, Zap } from "lucide-react";
import { api, desktop, enc, fmtBytes } from "../api";
import { go, useApp, useEvent } from "../store";
import { Badge, Button, ConfirmButton, Field, PageHeader, Progress, Spinner, Tabs, fitBadge } from "../components/ui";

const CTX = [2048, 4096, 8192, 16384, 32768, 65536];

function DownloadsBar() {
  const { downloads } = useApp();
  const active = Object.values(downloads).filter((d: any) => ["queued", "downloading", "verifying"].includes(d.state));
  if (!active.length) return null;
  return (
    <div className="card">
      <h3>Downloading</h3>
      {active.map((d: any) => (
        <div key={d.id} className="download-row">
          <div className="row between"><b>{d.name}</b>
            <span className="muted small">{fmtBytes(d.done)} / {fmtBytes(d.total)} {d.rate ? `· ${fmtBytes(d.rate)}/s` : ""} {d.state === "verifying" ? "· checking…" : ""}</span></div>
          <div className="row gap-s"><Progress value={d.done} total={d.total} />
            <button className="icon-btn" title="Cancel" onClick={() => api.del(`/downloads/${d.id}`)}><X size={14} /></button></div>
        </div>
      ))}
      <p className="muted small">Downloads continue if you close the window. An interrupted download resumes where it stopped.</p>
    </div>
  );
}

function Installed() {
  const { toast, modelStatus, boot } = useApp();
  const [data, setData] = useState<any>(null);
  const [busy, setBusy] = useState<string>("");
  const load = () => api.get("/models").then(setData);
  useEffect(() => { load(); }, []);
  useEvent((e) => { if (["models_changed", "model_status"].includes(e.kind)) load(); });
  const set = async (id: string, values: any) => { try { await api.put(`/models/${id}`, values); load(); } catch (e: any) { toast(e.message, "error"); } };
  if (!data) return <Spinner />;
  const gpuInstalled = (boot.runtime.installed || []).some((b: string) => b !== "cpu");
  return (
    <div className="stack">
      <DownloadsBar />
      {modelStatus.state !== "idle" && (
        <div className="card row between">
          <div><b>Loaded now:</b> {modelStatus.model_id || "—"} <span className="muted">· {modelStatus.state}{modelStatus.backend ? ` · ${modelStatus.backend}` : ""}{modelStatus.ctx ? ` · context ${modelStatus.ctx}` : ""}</span>
            {modelStatus.error && <div className="error-line">{modelStatus.error}</div>}</div>
          {modelStatus.state === "ready" && <Button small onClick={async () => { try { await api.post("/runtime/unload"); } catch (e: any) { toast(e.message, "error"); } }}>Unload (free memory)</Button>}
        </div>
      )}
      {data.models.length === 0 && (
        <div className="card center"><p>No models yet.</p><Button kind="primary" onClick={() => go("models", "discover")}>Find a model</Button></div>
      )}
      {data.models.map((m: any) => {
        const s = m.settings || {};
        const b = m.bench || {};
        return (
          <div key={m.id} className="card model-card">
            <div className="row between wrap">
              <div>
                <h3>{m.name} {m.id === data.default && <Badge kind="accent"><Star size={11} /> default</Badge>}</h3>
                <div className="muted small">{m.repo || m.path} · {m.quant} · {fmtBytes(m.size)}{m.meta?.architecture ? ` · ${m.meta.architecture}` : ""}{m.meta?.context_length ? ` · trained for ${m.meta.context_length.toLocaleString()} tokens` : ""}</div>
                {!m.exists && <div className="error-line">The model file is missing ({m.path}).</div>}
              </div>
              <div className="row gap-s">{fitBadge(m.fit, s.compute === "gpu" ? "gpu" : "cpu")}</div>
            </div>
            <div className="row gap-m wrap mt">
              <Field label="Run on">
                <div className="seg">
                  {["auto", "cpu", "gpu"].map((c) => (
                    <button key={c} className={`seg-btn${(s.compute || "auto") === c ? " on" : ""}`} disabled={c === "gpu" && !gpuInstalled}
                      title={c === "gpu" && !gpuInstalled ? "Install a GPU runtime in the Hardware tab first" : ""}
                      onClick={() => set(m.id, { compute: c })}>{c === "auto" ? "Auto" : c.toUpperCase()}</button>
                  ))}
                </div>
              </Field>
              <Field label="Context (memory of the conversation)">
                <select className="input" value={s.ctx || boot.settings.default_ctx || 8192} onChange={(e) => set(m.id, { ctx: Number(e.target.value) })}>
                  {CTX.map((c) => <option key={c} value={c}>{c.toLocaleString()} tokens</option>)}
                </select>
              </Field>
              {s.compute === "gpu" && (
                <Field label="Layers on the GPU" help="auto = as many as fit; the rest run on the CPU (CPU + GPU together).">
                  <input className="input narrow" placeholder="auto" value={s.gpu_layers && s.gpu_layers !== "auto" ? s.gpu_layers : ""}
                    onChange={(e) => set(m.id, { gpu_layers: e.target.value ? (e.target.value === "all" ? "all" : Number(e.target.value) || "auto") : "auto" })} />
                </Field>
              )}
              <Field label="CPU threads">
                <input className="input narrow" type="number" min={1} max={64} placeholder="auto" value={s.threads || ""}
                  onChange={(e) => set(m.id, { threads: e.target.value ? Number(e.target.value) : null })} />
              </Field>
            </div>
            {b.at && (
              <div className="bench">
                <Gauge size={14} /> Last test ({b.compute?.toUpperCase()}): reply in {b.reply_seconds}s · reads {Math.round(b.prompt_per_second || 0)} tok/s · writes {(b.predicted_per_second || 0).toFixed(1)} tok/s · structured answers {b.json_valid ? "OK" : "failed"}
              </div>
            )}
            <div className="row gap-s mt wrap">
              <Button small icon={<Play size={13} />} busy={busy === `load-${m.id}`} onClick={async () => {
                setBusy(`load-${m.id}`); try { await api.post(`/models/${m.id}/load`, {}); } catch (e: any) { toast(e.message, "error"); } setBusy(""); }}>Load now</Button>
              <Button small icon={<Gauge size={13} />} busy={busy === `bench-${m.id}`} onClick={async () => {
                setBusy(`bench-${m.id}`); try { await api.post(`/models/${m.id}/benchmark`); load(); toast("Speed test done", "ok"); } catch (e: any) { toast(e.message, "error"); } setBusy(""); }}>Test speed</Button>
              {m.id !== data.default && <Button small icon={<Star size={13} />} onClick={() => set(m.id, { make_default: true })}>Make default</Button>}
              <ConfirmButton small icon={<Trash2 size={13} />} onConfirm={async () => { try { await api.del(`/models/${m.id}`); load(); } catch (e: any) { toast(e.message, "error"); } }}>Delete</ConfirmButton>
            </div>
          </div>
        );
      })}
      <div className="card">
        <h3>Add a model file you already have</h3>
        <p className="muted small">Any GGUF file (from LM Studio, Ollama exports, Hugging Face…). It stays where it is.</p>
        <ImportFile onDone={load} />
      </div>
    </div>
  );
}

function ImportFile({ onDone }: any) {
  const { toast } = useApp();
  const [path, setPath] = useState("");
  const go_ = async (p: string) => { try { await api.post("/models/import", { path: p }); toast("Model added", "ok"); setPath(""); onDone(); } catch (e: any) { toast(e.message, "error"); } };
  return (
    <div className="row gap-s">
      <input className="input" value={path} onChange={(e) => setPath(e.target.value)} placeholder="C:\\models\\model.gguf" />
      {desktop && <Button onClick={async () => { const p = await desktop.pickFile([{ name: "GGUF model", extensions: ["gguf"] }]); if (p) go_(p); }}>Browse…</Button>}
      <Button kind="primary" disabled={!path} onClick={() => go_(path)}>Add</Button>
    </div>
  );
}

function Discover() {
  const { toast } = useApp();
  const [catalog, setCatalog] = useState<any>(null);
  const [q, setQ] = useState("");
  const [results, setResults] = useState<any[] | null>(null);
  const [repo, setRepo] = useState<any>(null);
  const [busy, setBusy] = useState("");
  useEffect(() => { api.get("/models/catalog").then(setCatalog).catch((e) => toast(e.message, "error")); }, []);
  const hasGpu = catalog?.hardware?.gpus?.some((g: any) => !g.integrated && g.vram_mb > 3000);
  const search = async () => {
    if (!q.trim()) return;
    setBusy("search");
    try { setResults((await api.get(`/models/search?q=${enc(q)}`)).results); setRepo(null); } catch (e: any) { toast(`Search failed: ${e.message}`, "error"); }
    setBusy("");
  };
  const openRepo = async (r: string) => {
    setBusy(r);
    try { setRepo(await api.get(`/models/files?repo=${enc(r)}`)); } catch (e: any) { toast(e.message, "error"); }
    setBusy("");
  };
  const dl = async (body: any) => {
    try { await api.post("/models/download", body); toast("Download started", "ok"); go("models"); } catch (e: any) { toast(e.message, "error"); }
  };
  return (
    <div className="stack">
      <DownloadsBar />
      <div className="card">
        <h3>Recommended for this computer</h3>
        {!catalog ? <Spinner /> : (
          <>
            <p className="muted small">{catalog.hardware.cpu} · {(catalog.hardware.ram_mb / 1024).toFixed(0)} GB memory{catalog.hardware.gpus.map((g: any) => ` · ${g.name}${g.vram_mb ? ` (${(g.vram_mb / 1024).toFixed(0)} GB)` : ""}`).join("")}</p>
            <div className="catalog">
              {catalog.models.map((m: any) => (
                <div key={m.key} className={`catalog-item${m.fit.cpu === "too_big" && !(hasGpu && m.fit.gpu === "fits") ? " dim" : ""}`}>
                  <div className="row between"><b>{m.name}</b>{fitBadge(m.fit, hasGpu ? "gpu" : "cpu")}</div>
                  <div className="small muted">{m.quant} · about {m.approx_gb} GB{m.tools ? " · good with tools" : ""}{m.default ? " · recommended" : ""}</div>
                  <p className="small">{m.about}</p>
                  {m.installed ? <Badge kind="ok">Installed</Badge> :
                    <Button small kind={m.default ? "primary" : "default"} icon={<Download size={13} />} onClick={() => dl({ catalog_key: m.key })}>Download</Button>}
                </div>
              ))}
            </div>
          </>
        )}
      </div>
      <div className="card">
        <h3>Search Hugging Face</h3>
        <p className="muted small">Any GGUF model. Smaller "Q4_K_M" files are the usual best balance of size and quality.</p>
        <div className="row gap-s"><input className="input" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && search()} placeholder="e.g. qwen3.5, gemma, llama, mistral" />
          <Button icon={<Search size={14} />} busy={busy === "search"} onClick={search}>Search</Button></div>
        {results && !repo && (
          <div className="mt">
            {results.length === 0 && <p className="muted">No GGUF models found.</p>}
            {results.map((r) => (
              <div key={r.repo} className="list-row" onClick={() => openRepo(r.repo)}>
                <span className="ellipsis">{r.repo}</span>
                <span className="muted small">{busy === r.repo ? <Spinner size={12} /> : `${(r.downloads || 0).toLocaleString()} downloads`}{r.gated ? " · needs access" : ""}</span>
              </div>
            ))}
          </div>
        )}
        {repo && (
          <div className="mt">
            <div className="row between"><b>{repo.repo}</b><button className="link" onClick={() => setRepo(null)}>Back to results</button></div>
            {repo.files.map((f: any) => (
              <div key={f.file} className="list-row">
                <span className="ellipsis">{f.file}{f.parts.length > 1 ? ` (${f.parts.length} parts)` : ""}</span>
                <span className="row gap-s"><span className="muted small">{f.quant} · {fmtBytes(f.size)}</span>{fitBadge(f.fit, hasGpu ? "gpu" : "cpu")}
                  <Button small icon={<Download size={13} />} onClick={() => dl({ repo: repo.repo, file: f.file })}>Download</Button></span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function Hardware() {
  const { toast, boot, refreshBoot } = useApp();
  const [hw, setHw] = useState<any>(null);
  const [rt, setRt] = useState<any>(null);
  const [progress, setProgress] = useState<any>({});
  const load = () => { api.get("/hardware").then(setHw); api.get("/runtime").then(setRt); };
  useEffect(() => { load(); }, []);
  useEvent((e) => {
    if (e.kind === "runtime") {
      if (e.action === "install_progress") setProgress((p: any) => ({ ...p, [e.backend]: e }));
      else { setProgress((p: any) => ({ ...p, [e.backend]: null })); load(); }
    }
  });
  const setDefault = async (v: string) => { await api.put("/settings", { default_compute: v }); refreshBoot(); };
  if (!hw || !rt) return <Spinner />;
  const info: any = {
    cpu: "Works on every computer. Speed depends on the processor and memory.",
    vulkan: "Uses the graphics card through Vulkan (NVIDIA, AMD, Intel Arc). Integrated graphics are often slower than the CPU.",
    cuda: "Fastest on NVIDIA graphics cards (needs a recent NVIDIA driver). Large download (~0.5–1 GB).",
    metal: "Apple Silicon graphics.",
  };
  return (
    <div className="stack">
      <div className="card">
        <h3>This computer</h3>
        <div className="hw-grid">
          <div><Cpu size={16} /> <b>{hw.cpu}</b><div className="muted small">{hw.cores} cores / {hw.threads} threads</div></div>
          <div><MemoryStick size={16} /> <b>{(hw.ram_mb / 1024).toFixed(1)} GB memory</b><div className="muted small">{(hw.ram_free_mb / 1024).toFixed(1)} GB free now</div></div>
          <div><HardDrive size={16} /> <b>{hw.disk_free_mb ? `${(hw.disk_free_mb / 1024).toFixed(0)} GB free` : "—"}</b><div className="muted small">on the home drive</div></div>
          {hw.gpus.map((g: any) => (
            <div key={g.name}><Zap size={16} /> <b>{g.name}</b><div className="muted small">{g.vram_mb ? `${(g.vram_mb / 1024).toFixed(1)} GB` : "shared memory"}{g.integrated ? " · integrated" : ""}</div></div>
          ))}
        </div>
        <p className="small">Suggested: <b>{hw.recommended_backend === "cpu" ? "CPU" : hw.recommended_backend.toUpperCase()}</b>{hw.recommended_backend === "cpu" && hw.gpus.some((g: any) => g.integrated) ? " — integrated graphics share the computer's memory and are usually not faster." : ""}</p>
      </div>
      <div className="card">
        <h3>Processing</h3>
        <Field label="Default for models set to Auto" help="Each model can override this on its card. Auto uses the GPU only when a suitable graphics card and GPU runtime are present.">
          <div className="seg">
            {["auto", "cpu", "gpu"].map((c) => <button key={c} className={`seg-btn${(boot.settings.default_compute || "auto") === c ? " on" : ""}`} onClick={() => setDefault(c)}>{c === "auto" ? "Auto" : c.toUpperCase()}</button>)}
          </div>
        </Field>
      </div>
      <div className="card">
        <h3>Runtimes (llama.cpp {rt.tag})</h3>
        {["cpu", ...rt.available.filter((b: string) => b !== "cpu")].map((b: string) => {
          const installed = rt.installed[b];
          const p = progress[b] || rt.installing[b];
          return (
            <div key={b} className="runtime-row">
              <div><b>{rt.labels[b] || b}</b> {installed && <Badge kind="ok">installed</Badge>}
                <div className="muted small">{info[b]}</div>
                {rt.devices[b]?.length > 0 && <div className="small">Devices: {rt.devices[b].map((d: any) => d.error ? d.error : `${d.name} (${(d.total_mb / 1024).toFixed(1)} GB)`).join(", ")}</div>}
                {p && <div className="row gap-s"><Progress value={p.done || 0} total={p.total || 1} /><span className="small muted">{p.state || "downloading"}</span></div>}
              </div>
              <div>
                {!installed && <Button small icon={<Download size={13} />} busy={!!p} onClick={async () => { try { await api.post("/runtime/install", { backend: b }); } catch (e: any) { toast(e.message, "error"); } }}>Install</Button>}
                {installed && b !== "cpu" && b !== "custom" && <ConfirmButton small onConfirm={async () => { try { await api.del(`/runtime/${b}`); load(); } catch (e: any) { toast(e.message, "error"); } }}>Remove</ConfirmButton>}
              </div>
            </div>
          );
        })}
        {rt.installed.custom && <div className="runtime-row"><div><b>Custom llama-server</b><div className="muted small">{rt.installed.custom}</div></div></div>}
      </div>
    </div>
  );
}

export default function Models({ tab }: { tab?: string }) {
  const t = tab || "installed";
  return (
    <div className="page">
      <PageHeader title="Models" subtitle="Models run on this computer only. Download the ones it can handle, choose CPU or GPU, and pick a model per chat, project or scheduled task." />
      <Tabs value={t} onChange={(v: string) => go("models", v)} tabs={[{ id: "installed", label: "Installed" }, { id: "discover", label: "Discover" }, { id: "hardware", label: "Hardware & GPU" }]} />
      {t === "installed" && <Installed />}
      {t === "discover" && <Discover />}
      {t === "hardware" && <Hardware />}
    </div>
  );
}
