import React, { useEffect, useState } from "react";
import { Copy, MessageSquarePlus, Plus, Trash2, Upload, Workflow, X } from "lucide-react";
import { api, enc } from "../api";
import { go, useApp, useEvent } from "../store";
import { Badge, Button, ConfirmButton, Field, PageHeader, Spinner, Toggle } from "../components/ui";
import { startAgentChat } from "./Skills";

const GROUPS = [
  ["files", "Files in the chat's folders"], ["web", "Web search & pages"], ["python", "Run Python"],
  ["memory", "Memory"], ["skills", "Skills"], ["automation", "Schedules & harnesses"],
  ["vision", "Look at images & videos (vision models)"], ["agents", "Start helper agents in parallel"],
];
const MODES: any = { chat: "Chat — no tools, fastest", agent: "Agent — tools, step by step", planner: "Planner — plan first, then steps",
  research: "Research — fixed stages: search, read, download, cited report (cannot loop)", team: "Team — splits the work between agents in parallel" };

function Editor({ h, onSaved, onCancel }: any) {
  const { toast } = useApp();
  const [s, setS] = useState<any>({ name: h?.name || "", description: h?.description || "", type: "loop", mode: h?.mode || "agent",
    tools: h?.tools || ["files", "web", "memory", "skills"], max_steps: h?.max_steps || 10, temperature: h?.temperature ?? 0.3,
    thinking: !!h?.thinking, connectors: h?.connectors ?? true, memory: h?.memory ?? true, learn: h?.learn ?? true,
    plan_max_steps: h?.plan_max_steps || 5, tool_mode: h?.tool_mode || "native",
    ...(h?.max_searches ? { max_searches: h.max_searches } : {}), ...(h?.time_limit_minutes ? { time_limit_minutes: h.time_limit_minutes } : {}),
    ...(h?.max_reply_tokens ? { max_reply_tokens: h.max_reply_tokens } : {}), ...(h?.queries ? { queries: h.queries } : {}),
    ...(h?.pages ? { pages: h.pages } : {}), ...(h?.max_downloads !== undefined ? { max_downloads: h.max_downloads } : {}) });
  const num = (k: string, v: string) => { const n = { ...s }; if (v === "") delete n[k]; else n[k] = Number(v); setS(n); };
  const [prompt, setPrompt] = useState(h?.system_prompt || "");
  const save = async () => {
    try {
      const out = h?.editable ? await api.put(`/harnesses/${enc(h.id)}/spec`, { spec: s, prompt }) : await api.post("/harnesses", { spec: s, prompt });
      toast("Harness saved", "ok"); onSaved(out);
    } catch (e: any) { toast(e.message, "error"); }
  };
  return (
    <div className="card">
      <h3>{h?.editable ? `Edit ${h.name}` : "New harness"}</h3>
      <Field label="Name"><input className="input" value={s.name} onChange={(e) => setS({ ...s, name: e.target.value })} /></Field>
      <Field label="Description" help="Shown in the harness menu."><input className="input" value={s.description} onChange={(e) => setS({ ...s, description: e.target.value })} /></Field>
      <Field label="How it works">
        <select className="input" value={s.mode} onChange={(e) => setS({ ...s, mode: e.target.value })}>{Object.entries(MODES).map(([k, v]: any) => <option key={k} value={k}>{v}</option>)}</select>
      </Field>
      {s.mode === "research" && <div className="row gap-m wrap">
        <Field label="Searches"><input className="input narrow" type="number" min={1} max={8} placeholder="4" value={s.queries ?? ""} onChange={(e) => num("queries", e.target.value)} /></Field>
        <Field label="Pages to read"><input className="input narrow" type="number" min={1} max={15} placeholder="6" value={s.pages ?? ""} onChange={(e) => num("pages", e.target.value)} /></Field>
        <Field label="Files to download"><input className="input narrow" type="number" min={0} max={20} placeholder="6" value={s.max_downloads ?? ""} onChange={(e) => num("max_downloads", e.target.value)} /></Field>
      </div>}
      {!["chat", "research", "team"].includes(s.mode) && <>
        <Field label="Tools">{GROUPS.map(([k, label]) => (
          <label key={k} className="check"><input type="checkbox" checked={s.tools.includes(k)} onChange={(e) => setS({ ...s, tools: e.target.checked ? [...s.tools, k] : s.tools.filter((x: string) => x !== k) })} /> {label}</label>
        ))}<Toggle checked={s.connectors} onChange={(v: boolean) => setS({ ...s, connectors: v })} label="Can use the chat's connectors" /></Field>
        <div className="row gap-m wrap">
          <Field label="Step limit"><input className="input narrow" type="number" min={1} max={60} value={s.max_steps} onChange={(e) => setS({ ...s, max_steps: Number(e.target.value) })} /></Field>
          {s.mode === "planner" && <Field label="Max plan steps"><input className="input narrow" type="number" min={1} max={12} value={s.plan_max_steps} onChange={(e) => setS({ ...s, plan_max_steps: Number(e.target.value) })} /></Field>}
          <Field label="Web searches per reply" help="Stops search loops."><input className="input narrow" type="number" min={1} max={30} placeholder="6" value={s.max_searches ?? ""} onChange={(e) => num("max_searches", e.target.value)} /></Field>
          <Field label="Time limit (min)"><input className="input narrow" type="number" min={1} placeholder="30" value={s.time_limit_minutes ?? ""} onChange={(e) => num("time_limit_minutes", e.target.value)} /></Field>
          <Field label="Tool calling" help="JSON mode helps small models that fumble native tool calls.">
            <select className="input" value={s.tool_mode} onChange={(e) => setS({ ...s, tool_mode: e.target.value })}><option value="native">Native</option><option value="json">JSON (small models)</option></select>
          </Field>
        </div>
      </>}
      <div className="row gap-m wrap">
        <Field label={`Creativity (temperature ${s.temperature})`}><input type="range" min={0} max={1.2} step={0.1} value={s.temperature} onChange={(e) => setS({ ...s, temperature: Number(e.target.value) })} /></Field>
        <Toggle checked={s.thinking} onChange={(v: boolean) => setS({ ...s, thinking: v })} label="Think before answering" />
        <Toggle checked={s.memory} onChange={(v: boolean) => setS({ ...s, memory: v })} label="Use memory" />
        <Toggle checked={s.learn} onChange={(v: boolean) => setS({ ...s, learn: v })} label="Learn from chats" />
      </div>
      <Field label="Instructions (Markdown)" help="Role, method, quality rules, output format. Short is better for local models.">
        <textarea className="input mono" rows={12} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
      </Field>
      <div className="row gap-s"><Button kind="primary" disabled={!s.name} onClick={save}>Save</Button>{onCancel && <Button kind="ghost" onClick={onCancel}>Cancel</Button>}</div>
    </div>
  );
}

export default function Harnesses({ selected }: { selected?: string }) {
  const { toast, refreshBoot, boot } = useApp();
  const [list, setList] = useState<any[] | null>(null);
  const [detail, setDetail] = useState<any>(null);
  const [mode, setMode] = useState<"view" | "edit" | "new">("view");
  const [ask, setAsk] = useState("");
  const load = () => api.get("/harnesses").then((r) => setList(r.harnesses));
  useEffect(() => { load(); }, []);
  useEvent((e) => { if (["harnesses_changed", "plugins_changed", "memory_learned"].includes(e.kind)) load(); });
  useEffect(() => {
    setMode(selected === "new" ? "new" : "view");
    if (selected && selected !== "new") api.get(`/harnesses/${enc(selected)}/detail`).then(setDetail).catch(() => setDetail(null));
    else setDetail(null);
  }, [selected, list]);
  if (!list) return <div className="page"><Spinner /></div>;
  const after = (h: any) => { load(); refreshBoot(); setMode("view"); go("harnesses", h.id); };
  return (
    <div className="page">
      <PageHeader title="Harnesses" subtitle="A harness is how the agent works: its instructions, tools, step limit and style — or a fixed pipeline. Pick one per chat or scheduled task."
        actions={<>
          <label className="btn btn-default"><Upload size={14} /><span>Upload</span>
            <input type="file" accept=".zip,.json" hidden onChange={async (e) => {
              const file = e.target.files?.[0]; e.target.value = "";
              if (file) try { const h = await api.upload("/harnesses-upload", file); toast("Harness added", "ok"); after(h); } catch (err: any) { toast(err.message, "error"); }
            }} /></label>
          <Button icon={<Plus size={14} />} onClick={() => go("harnesses", "new")}>New harness</Button>
        </>} />
      <div className="card ask-card">
        <MessageSquarePlus size={18} className="accent" />
        <input className="input" value={ask} onChange={(e) => setAsk(e.target.value)} placeholder="Or describe the assistant you want, e.g. 'a proofreader for my reports'" />
        <Button kind="primary" disabled={!ask} onClick={() => startAgentChat(`Create a harness for me: ${ask}. Load the harness-creator skill first, propose the design, then save it with save_harness.`)}>Ask the agent</Button>
      </div>
      <div className="split">
        <div className="list-col">
          {list.map((h) => (
            <div key={h.id} className={`pick-row${selected === h.id ? " active" : ""}`} onClick={() => go("harnesses", h.id)}>
              <Workflow size={14} />
              <div className="grow"><b>{h.name}</b> {h.type === "pipeline" && <Badge>pipeline</Badge>} {h.id === boot.settings.default_harness && <Badge kind="accent">default</Badge>}
                <div className="small muted clamp">{h.error ? `Problem: ${h.error}` : h.description}</div></div>
              <span onClick={(e) => e.stopPropagation()}><Toggle checked={h.enabled} onChange={async (v: boolean) => { await api.put(`/harnesses/${enc(h.id)}/enabled`, { enabled: v }); load(); refreshBoot(); }} /></span>
            </div>
          ))}
        </div>
        <div className="detail-col">
          {mode === "new" && <Editor onSaved={after} onCancel={() => go("harnesses")} />}
          {mode === "edit" && detail && <Editor h={detail} onSaved={after} onCancel={() => setMode("view")} />}
          {mode === "view" && !detail && <div className="muted pad">Choose a harness to see how it works.</div>}
          {mode === "view" && detail && (
            <div className="stack">
              <div className="card">
                <div className="row between"><h2>{detail.name}</h2><Badge>{detail.source === "plugin" ? `plugin ${detail.plugin}` : detail.source}</Badge></div>
                <p>{detail.description}</p>
                <div className="kv">
                  <div>Type</div><div>{detail.type === "pipeline" ? "Pipeline (fixed steps in code)" : MODES[detail.mode || "agent"]}</div>
                  {detail.type !== "pipeline" && <><div>Tools</div><div>{(detail.tools || []).join(", ") || "none"}{detail.connectors ? " + chat connectors" : ""}</div>
                    <div>Step limit</div><div>{detail.max_steps || "—"}</div></>}
                  {detail.type === "pipeline" && <><div>Needs</div><div>{(detail.needs || []).join(", ") || "—"}</div><div>Time limit</div><div>{detail.timeout_minutes || 60} minutes</div></>}
                </div>
                {detail.system_prompt && <><div className="small muted mt">Instructions</div><pre className="prompt">{detail.system_prompt}</pre></>}
                <div className="row gap-s mt">
                  {detail.editable && detail.type !== "pipeline" && <Button onClick={() => setMode("edit")}>Edit</Button>}
                  {!detail.editable && detail.type !== "pipeline" && <Button icon={<Copy size={13} />} onClick={async () => { const h = await api.post(`/harnesses/${enc(detail.id)}/copy`); after(h); }}>Copy to customise</Button>}
                  {detail.id !== boot.settings.default_harness && detail.type !== "pipeline" && <Button onClick={async () => { await api.put("/settings", { default_harness: detail.id }); refreshBoot(); toast("Default harness set", "ok"); }}>Make default</Button>}
                  {detail.editable && <ConfirmButton icon={<Trash2 size={13} />} onConfirm={async () => { await api.del(`/harnesses/${enc(detail.id)}/delete`); go("harnesses"); load(); refreshBoot(); }}>Delete</ConfirmButton>}
                </div>
              </div>
              <div className="card">
                <h3>Lessons learned</h3>
                <p className="muted small">After tasks run with this harness, the agent notes what to do differently next time. Tool-using harnesses read these before every task; for pipelines they are a running record.</p>
                {detail.lessons.length === 0 && <div className="muted small">None yet.</div>}
                {detail.lessons.map((m: any) => (
                  <div key={m.id} className="memory-row"><span>{m.text}</span><button className="icon-btn" onClick={async () => { await api.del(`/memory/${m.id}`); load(); }}><X size={13} /></button></div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
