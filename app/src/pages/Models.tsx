import React, { useEffect, useState } from "react";
import { Cpu, Download, Eye, Gauge, HardDrive, MemoryStick, Play, RotateCcw, Search, Star, Trash2, X, Zap } from "lucide-react";
import { api, desktop, enc, fmtBytes } from "../api";
import { go, useApp, useEvent } from "../store";
import { Badge, Button, ConfirmButton, Field, FolderInput, PageHeader, Progress, Spinner, Tabs, Toggle, fitBadge } from "../components/ui";

const CTX = [2048, 4096, 8192, 16384, 32768, 65536];

function fmtEta(s?: number | null) {
  if (!s) return "";
  if (s < 90) return `${s} s left`;
  if (s < 5400) return `${Math.round(s / 60)} min left`;
  return `${(s / 3600).toFixed(1)} h left`;
}

const DL_ACTIVE = ["queued", "connecting", "downloading", "retrying", "verifying"];

function DownloadsBar() {
  const { downloads, toast } = useApp();
  const shown = Object.values(downloads).filter((d: any) => [...DL_ACTIVE, "paused", "failed"].includes(d.state));
  if (!shown.length) return null;
  const act = async (fn: () => Promise<any>) => { try { await fn(); } catch (e: any) { toast(e.message, "error"); } };
  return (
    <div className="card">
      <h3>Downloads</h3>
      {shown.map((d: any) => {
        const active = DL_ACTIVE.includes(d.state);
        const pct = d.total ? Math.floor((d.done / d.total) * 100) : 0;
        return (
          <div key={d.id} className="download-row">
            <div className="row between wrap"><b>{d.name}{d.vision ? <> <Eye size={13} className="accent" /></> : null}</b>
              <span className="muted small">{fmtBytes(d.done)} / {fmtBytes(d.total)} · {pct}%{d.rate ? ` · ${fmtBytes(d.rate)}/s` : ""}{d.eta ? ` · ${fmtEta(d.eta)}` : ""}</span></div>
            <div className="row gap-s"><Progress value={d.done} total={d.total} />
              {(d.state === "failed" || d.state === "paused") && <Button small icon={<RotateCcw size={13} />} onClick={() => act(() => api.post(`/downloads/${d.id}/resume`))}>{d.state === "paused" ? "Resume" : "Try again"}</Button>}
              <button className="icon-btn" title={active ? "Cancel" : "Remove"} onClick={() => act(() => api.del(`/downloads/${d.id}`))}><X size={14} /></button></div>
            {active && <div className="dl-phase">{d.state === "queued" ? "Waiting to start…" : d.state === "verifying" ? "Checking the file…" : d.phase || (d.state === "connecting" ? "Connecting…" : "Downloading…")}
              {d.state === "downloading" && !d.rate && d.done === 0 ? " — waiting for the first data…" : ""}</div>}
            {!active && d.error && <div className="dl-error">{d.error}</div>}
          </div>
        );
      })}
      <p className="muted small">Downloads continue if you close the window, retry by themselves when the connection drops, and resume where they stopped after a restart.</p>
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
      {(data.loaded?.length > 0 || modelStatus.state === "loading" || modelStatus.state === "failed") && (
        <div className="card">
          <h3>Loaded now</h3>
          {modelStatus.state === "loading" && <div className="row gap-s small"><Spinner size={13} /> Loading {modelStatus.model_id}…</div>}
          {modelStatus.state === "failed" && <div className="error-line">{modelStatus.error}</div>}
          {(data.loaded || []).map((l: any) => (
            <div key={l.model_id} className="loaded-row">
              <div><b>{l.model_id}</b> {l.vision && <Eye size={13} className="accent" />} <span className="muted small">· {l.compute?.toUpperCase()} ({l.backend}) · context {l.ctx?.toLocaleString()}{l.parallel > 1 ? ` · ${l.parallel} replies at once` : ""} · about {(l.memory_mb / 1024).toFixed(1)} GB{l.users ? ` · in use (${l.users})` : ""}</span></div>
              <Button small disabled={l.users > 0} onClick={async () => { try { await api.post("/runtime/unload", { model_id: l.model_id }); load(); } catch (e: any) { toast(e.message, "error"); } }}>Unload</Button>
            </div>
          ))}
          <p className="muted small">Several models can be loaded at once (up to {boot.settings.max_loaded_models || 3}, if memory allows) so agents can work in parallel. Idle models unload after {boot.settings.unload_after_minutes || 20} minutes.</p>
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
                <h3>{m.name} {m.id === data.default && <Badge kind="accent"><Star size={11} /> default</Badge>}
                  {s.mmproj && s.vision !== false && <Badge kind="ok" title={s.mmproj}><Eye size={11} /> sees images</Badge>}</h3>
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
              <Field label="Simultaneous replies" help="For agents in parallel; uses more memory.">
                <select className="input narrow" value={s.parallel || 1} onChange={(e) => set(m.id, { parallel: Number(e.target.value) })}>
                  {[1, 2, 3, 4].map((n) => <option key={n} value={n}>{n}</option>)}
                </select>
              </Field>
              <Field label="Vision (images & video)">
                {s.mmproj ? <><Toggle checked={s.vision !== false} onChange={(v: boolean) => set(m.id, { vision: v })} label={s.vision !== false ? "On" : "Off"} />
                  {s.vision === false && s.vision_error && <div className="field-help">Turned off: {s.vision_error}</div>}</>
                  : <Button small icon={<Eye size={13} />} busy={busy === `vision-${m.id}`} onClick={async () => {
                      setBusy(`vision-${m.id}`);
                      try { const r = await api.post(`/models/${m.id}/vision`); toast(`Downloading the vision part from ${r.repo}`, "ok"); } catch (e: any) { toast(e.message, "error"); }
                      setBusy(""); }}>Add vision</Button>}
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
      <LocalModels onAdded={load} />
      <div className="card">
        <h3>Add a model file you already have</h3>
        <p className="muted small">Any GGUF file (from LM Studio, Ollama, Hugging Face…). It stays where it is.</p>
        <ImportFile onDone={load} />
      </div>
    </div>
  );
}

// Models other local-AI apps already downloaded (Ollama, LM Studio, Hugging Face cache, GPT4All, Jan, your folders).
export function LocalModels({ onAdded, compact }: any) {
  const { toast, boot, refreshBoot } = useApp();
  const [data, setData] = useState<any>(null);
  const [busy, setBusy] = useState("");
  const [folder, setFolder] = useState("");
  const scan = () => { setData(null); api.get("/models/local").then(setData).catch((e) => toast(e.message, "error")); };
  useEffect(() => { scan(); }, []);
  const add = async (f: any) => {
    setBusy(f.path);
    try { await api.post("/models/import", { path: f.path, name: f.name, mmproj: f.mmproj }); toast(`Added ${f.name}`, "ok"); await refreshBoot(); onAdded?.(); scan(); }
    catch (e: any) { toast(e.message, "error"); }
    setBusy("");
  };
  const fresh = (data?.found || []).filter((f: any) => !f.added_as);
  if (compact && data && !fresh.length) return null;
  return (
    <div className="card">
      <div className="row between"><h3>Already on this computer</h3>
        <button className="link" onClick={scan}>Scan again</button></div>
      {!data ? <div className="row gap-s muted small"><Spinner size={13} /> Looking for models from Ollama, LM Studio, Hugging Face, GPT4All and Jan…</div> : (
        <>
          <p className="muted small">{fresh.length ? `Found ${fresh.length} model${fresh.length > 1 ? "s" : ""} you can use without downloading. ` : "No other models found. "}
            They are used where they are (nothing is copied), and removing one here never deletes the other app's file.</p>
          {fresh.map((f: any) => (
            <div key={f.path} className="list-row" title={f.path}>
              <span className="ellipsis"><b>{f.name}</b> {f.vision && <Eye size={12} className="accent" />} <span className="muted small">{f.source} · {f.quant} · {fmtBytes(f.size)}{f.architecture ? ` · ${f.architecture}` : ""}</span></span>
              <span className="row gap-s">{fitBadge(f.fit)}<Button small kind="primary" busy={busy === f.path} onClick={() => add(f)}>Use</Button></span>
            </div>
          ))}
          {fresh.length > 1 && <div className="mt"><Button small onClick={async () => { for (const f of fresh) await add(f); }}>Use all {fresh.length}</Button></div>}
          {fresh.some((f: any) => f.source === "Ollama") && <p className="muted small">Most Ollama models load fine in llama.cpp; a few Ollama-only formats may not — the app will say so when loading.</p>}
          {!compact && (
            <Field label="Also look in a folder" help={`Searched: ${data.scanned.map((s: any) => s.folder).join(" · ") || "—"}`}>
              <div className="row gap-s"><FolderInput value={folder} onChange={setFolder} />
                <Button disabled={!folder} onClick={async () => {
                  await api.put("/settings", { model_scan_folders: [...(boot.settings.model_scan_folders || []), folder] });
                  setFolder(""); await refreshBoot(); scan();
                }}>Add folder</Button></div>
            </Field>
          )}
        </>
      )}
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

const CATEGORIES = [
  { id: "all", label: "All" }, { id: "chat", label: "Chat & agents" }, { id: "vision", label: "Sees images" },
  { id: "coding", label: "Coding" }, { id: "small", label: "Small & fast" },
];
const SORTS = [
  { id: "fit", label: "Best for this computer" }, { id: "downloads", label: "Most downloaded" },
  { id: "trending", label: "Trending" }, { id: "recent", label: "Recently updated" },
];

function RepoFiles({ repo, onBack, hasGpu, onDownload }: any) {
  const [vision, setVision] = useState(true);
  return (
    <div className="mt">
      <div className="row between"><b>{repo.repo}</b><button className="link" onClick={onBack}>Back</button></div>
      {repo.vision && (
        <div className="row gap-s small mt"><Toggle checked={vision} onChange={setVision} />
          <span><Eye size={13} className="accent" /> Also download the vision part ({fmtBytes(repo.mmproj_file?.size)}) so this model can look at images and videos</span></div>
      )}
      {repo.files.length === 0 && <p className="muted">This repository has no GGUF model files.</p>}
      {repo.files.map((f: any) => (
        <div key={f.file} className="list-row">
          <span className="ellipsis">{f.file}{f.parts.length > 1 ? ` (${f.parts.length} parts)` : ""} {f.recommended && <Badge kind="accent" title="Best quality that runs comfortably on this computer">recommended</Badge>}</span>
          <span className="row gap-s"><span className="muted small">{f.quant} · {fmtBytes(f.size)}</span>{fitBadge(f.fit, hasGpu ? "gpu" : "cpu")}
            <Button small kind={f.recommended ? "primary" : "default"} icon={<Download size={13} />} onClick={() => onDownload({ repo: repo.repo, file: f.file, vision })}>Download</Button></span>
        </div>
      ))}
      <p className="muted small">Q4_K_M is usually the best balance; bigger files (Q5, Q6, Q8) are a little better and slower, smaller ones (Q3, IQ2) are faster and noticeably worse.</p>
    </div>
  );
}

function Browse({ hasGpu, onOpen, busy }: any) {
  const { toast } = useApp();
  const [category, setCategory] = useState("all");
  const [sort, setSort] = useState("fit");
  const [trusted, setTrusted] = useState(true);
  const [fitsOnly, setFitsOnly] = useState(true);
  const [data, setData] = useState<any>(null);
  const [shown, setShown] = useState(25);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    setLoading(true); setShown(25);
    api.get(`/models/browse?category=${category}&sort=${sort}&trusted=${trusted}`).then(setData)
      .catch((e) => toast(`Could not reach Hugging Face: ${e.message}`, "error")).finally(() => setLoading(false));
  }, [category, sort, trusted]);
  const fits = (m: any) => m.fit.cpu !== "too_big" || (hasGpu && ["fits", "partial"].includes(m.fit.gpu));
  const rows = (data?.models || []).filter((m: any) => !fitsOnly || fits(m));
  return (
    <div className="card">
      <h3>Browse models for this computer</h3>
      <p className="muted small">Popular models on Hugging Face, with an estimate of size, whether they fit this computer's memory and how quick they feel. Click one to choose a file.</p>
      <div className="browse-filters">
        <div className="seg">{CATEGORIES.map((c) => <button key={c.id} className={`seg-btn${category === c.id ? " on" : ""}`} onClick={() => setCategory(c.id)}>{c.label}</button>)}</div>
        <select className="input narrow-auto" value={sort} onChange={(e) => setSort(e.target.value)}>{SORTS.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}</select>
        <Toggle checked={fitsOnly} onChange={setFitsOnly} label="Only what fits" />
        <Toggle checked={trusted} onChange={setTrusted} label="Well-known publishers" />
      </div>
      {loading && <div className="row gap-s muted small mt"><Spinner size={13} /> Asking Hugging Face…</div>}
      {!loading && data && (
        <div className="browse-list">
          {rows.length === 0 && <p className="muted">Nothing here fits this computer. Try another category, or turn off "Only what fits".</p>}
          {rows.slice(0, shown).map((m: any) => (
            <div key={m.repo} className={`browse-row${fits(m) ? "" : " dim"}`} onClick={() => onOpen(m.repo)}>
              <div className="stack-xs" style={{ minWidth: 0 }}>
                <div className="ellipsis"><b>{m.name}</b> <span className="muted small">by {m.publisher}</span></div>
                <div className="browse-meta">
                  <span>{m.params_b} B parameters{m.moe ? ` (${m.active_b} B active)` : ""}</span>
                  <span>· about {fmtBytes(m.est_size)} at Q4</span>
                  {m.context ? <span>· {Math.round(m.context / 1024)}K context</span> : null}
                  <span>· {m.downloads.toLocaleString()} downloads</span>
                  {m.vision && <Badge kind="accent"><Eye size={11} /> images</Badge>}
                  {m.coder && <Badge>code</Badge>}
                  {m.gated && <Badge kind="warn" title="Accept the licence on huggingface.co and add a token in Settings">needs access</Badge>}
                </div>
              </div>
              <div className="row gap-s">
                {m.installed ? <Badge kind="ok">installed</Badge> : null}
                <Badge kind={m.speed === "fast" ? "ok" : m.speed === "ok" ? "neutral" : "warn"} title="Rough reply speed on this computer">{({ fast: "fast", ok: "medium speed", slow: "slow", "too big": "too big" } as any)[m.speed] || m.speed}</Badge>
                {fitBadge(m.fit, hasGpu ? "gpu" : "cpu")}
                {busy === m.repo && <Spinner size={13} />}
              </div>
            </div>
          ))}
          {rows.length > shown && <Button small onClick={() => setShown(shown + 25)}>Show more ({rows.length - shown})</Button>}
        </div>
      )}
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
    try { setRepo(await api.get(`/models/files?repo=${enc(r)}`)); window.scrollTo({ top: 0 }); } catch (e: any) { toast(e.message, "error"); }
    setBusy("");
  };
  const dl = async (body: any) => {
    try { await api.post("/models/download", body); toast("Download started", "ok"); go("models"); } catch (e: any) { toast(e.message, "error"); }
  };
  return (
    <div className="stack">
      <DownloadsBar />
      {repo && <div className="card"><RepoFiles repo={repo} hasGpu={hasGpu} onBack={() => setRepo(null)} onDownload={dl} /></div>}
      <div className="card">
        <h3>Recommended</h3>
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
      <Browse hasGpu={hasGpu} onOpen={openRepo} busy={busy} />
      <div className="card">
        <h3>Search by name</h3>
        <p className="muted small">If you know a model's name. Any GGUF repository on Hugging Face.</p>
        <div className="row gap-s"><input className="input" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && search()} placeholder="e.g. qwen3.5, gemma, llama, mistral" />
          <Button icon={<Search size={14} />} busy={busy === "search"} onClick={search}>Search</Button></div>
        {results && (
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
