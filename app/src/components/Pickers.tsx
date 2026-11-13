import React, { useEffect, useRef, useState } from "react";
import { Bot, ChevronDown, Cpu, CornerDownRight, Eye, FolderOpen, Gauge, ListPlus, Paperclip, Plug, Plus, Send, Square, Users, X, Lightbulb } from "lucide-react";
import { api, desktop, fmtBytes } from "../api";
import { useApp } from "../store";
import { Button, Toggle } from "./ui";

function Popover({ label, icon, children, title }: any) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const close = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as any)) setOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);
  return (
    <div className="popover-wrap" ref={ref}>
      <button className="chip" onClick={() => setOpen(!open)} title={title}>{icon}<span className="ellipsis">{label}</span><ChevronDown size={12} /></button>
      {open && <div className="popover">{typeof children === "function" ? children(() => setOpen(false)) : children}</div>}
    </div>
  );
}

export function HarnessPicker({ value, onChange }: any) {
  const { boot } = useApp();
  const list = (boot?.harnesses || []).filter((h: any) => h.enabled && !h.error);
  const current = list.find((h: any) => h.id === value) || list.find((h: any) => h.id === (boot?.settings?.default_harness || "agent"));
  return (
    <Popover label={current?.name || "Harness"} icon={<Bot size={14} />} title="Harness: how the agent works on your request">
      {(close: any) => (
        <div className="menu">
          {list.map((h: any) => (
            <div key={h.id} className={`menu-item${current?.id === h.id ? " selected" : ""}`} onClick={() => { onChange(h.id); close(); }}>
              <div className="menu-title">{h.name} {h.type === "pipeline" && <span className="badge badge-neutral">pipeline</span>}</div>
              <div className="menu-sub">{h.description}</div>
            </div>
          ))}
        </div>
      )}
    </Popover>
  );
}

export const canSee = (m: any) => !!(m?.settings?.mmproj && m?.settings?.vision !== false);

export function ModelPicker({ value, onChange, allowDefault }: any) {
  const { boot } = useApp();
  const models = boot?.models || [];
  const current = models.find((m: any) => m.id === value) || (allowDefault ? null : models.find((m: any) => m.id === boot?.default_model));
  return (
    <Popover label={current ? current.name : (allowDefault ? "Same as the chat" : "No model yet")} icon={canSee(current) ? <Eye size={14} /> : <Cpu size={14} />}
      title="Model (runs on this computer). The eye marks models that can see images and videos.">
      {(close: any) => (
        <div className="menu">
          {models.length === 0 && <div className="menu-item" onClick={() => (location.hash = "/models/discover")}>Download a model…</div>}
          {allowDefault && <div className={`menu-item${!value ? " selected" : ""}`} onClick={() => { onChange(""); close(); }}><div className="menu-title">Same as the chat</div></div>}
          {models.map((m: any) => (
            <div key={m.id} className={`menu-item${current?.id === m.id ? " selected" : ""}`} onClick={() => { onChange(m.id); close(); }}>
              <div className="menu-title">{m.name} {canSee(m) && <Eye size={13} className="accent" />}</div>
              <div className="menu-sub">{m.quant} · {fmtBytes(m.size)} · {(m.settings?.compute || "auto").toUpperCase()}{canSee(m) ? " · sees images" : ""}{m.id === boot.default_model ? " · default" : ""}</div>
            </div>
          ))}
        </div>
      )}
    </Popover>
  );
}

// Per-chat limits: reply length, steps, time, temperature, searches. Empty = the harness's own setting.
const LIMITS = [
  { key: "max_reply_tokens", label: "Reply length (tokens)", help: "Longest single reply. About 750 words per 1,000 tokens.", ph: "2048" },
  { key: "max_steps", label: "Steps", help: "Model calls with tools before it must write up.", ph: "harness" },
  { key: "time_limit_minutes", label: "Time limit (minutes)", help: "The agent stops and reports what it found.", ph: "30" },
  { key: "max_searches", label: "Web searches per reply", help: "Stops search loops.", ph: "6" },
  { key: "max_tool_calls", label: "Tool calls per reply", help: "", ph: "40" },
  { key: "temperature", label: "Creativity (0–1.5)", help: "Low = precise, high = varied.", ph: "harness", step: 0.1 },
];

export function LimitsEditor({ value = {}, onChange, compact }: any) {
  return (
    <div className={compact ? "limits-grid compact" : "limits-grid"}>
      {LIMITS.map((l) => (
        <label key={l.key} className="limit" title={l.help}>
          <span>{l.label}</span>
          <input className="input narrow" type="number" min={0} step={l.step || 1} placeholder={l.ph} value={value[l.key] ?? ""}
            onChange={(e) => onChange({ ...value, [l.key]: e.target.value === "" ? undefined : Number(e.target.value) })} />
        </label>
      ))}
    </div>
  );
}

export function LimitsPicker({ value = {}, onChange }: any) {
  const [draft, setDraft] = useState<any>(value || {});
  useEffect(() => setDraft(value || {}), [JSON.stringify(value || {})]);
  const n = Object.keys(value || {}).length;
  return (
    <Popover label={n ? `${n} limit${n > 1 ? "s" : ""}` : "Limits"} icon={<Gauge size={14} />} title="Limits for this chat (reply length, steps, time…)">
      {(close: any) => (
        <div className="menu pad">
          <div className="menu-sub">Leave empty to use the harness's own settings.</div>
          <LimitsEditor value={draft} onChange={setDraft} />
          <div className="row gap-s"><Button small kind="primary" onClick={() => { onChange(draft); close(); }}>Save</Button>
            {n > 0 && <Button small kind="ghost" onClick={() => { onChange({}); close(); }}>Clear</Button>}</div>
        </div>
      )}
    </Popover>
  );
}

// A team for the next message: several agents working at the same time, each with its own model and limits.
export function TeamPicker({ team, onChange }: any) {
  const { boot } = useApp();
  const harnesses = (boot?.harnesses || []).filter((h: any) => h.enabled && !h.error && h.type === "loop" && h.mode !== "team");
  const on = team && team.length > 0;
  const update = (i: number, values: any) => onChange(team.map((a: any, j: number) => (j === i ? { ...a, ...values } : a)));
  const add = () => onChange([...(team || []), { name: `Agent ${(team?.length || 0) + 1}`, harness: "agent", model: "", task: "", limits: {} }]);
  return (
    <Popover label={on ? `Team of ${team.length}` : "Team"} icon={<Users size={14} />} title="Send the next message to a team of agents that work in parallel">
      <div className="menu pad team-builder">
        <div className="menu-sub">Agents work at the same time, each in its own chat, then a lead combines their results. Leave a task empty and the lead splits the work. Agents on different models run truly in parallel when memory allows.</div>
        {(team || []).map((a: any, i: number) => (
          <div key={i} className="team-agent">
            <div className="row gap-s">
              <input className="input" value={a.name} onChange={(e) => update(i, { name: e.target.value })} />
              <button className="icon-btn" title="Remove" onClick={() => onChange(team.filter((_: any, j: number) => j !== i))}><X size={13} /></button>
            </div>
            <div className="row gap-s wrap">
              <select className="input" value={a.harness} onChange={(e) => update(i, { harness: e.target.value })}>
                {harnesses.map((h: any) => <option key={h.id} value={h.id}>{h.name}</option>)}
              </select>
              <select className="input" value={a.model || ""} onChange={(e) => update(i, { model: e.target.value })} title="Model">
                <option value="">Same model as the chat</option>
                {(boot?.models || []).map((m: any) => <option key={m.id} value={m.id}>{m.name}{canSee(m) ? " (sees images)" : ""}</option>)}
              </select>
            </div>
            <textarea className="input" rows={2} placeholder="Task (optional — the lead decides)" value={a.task} onChange={(e) => update(i, { task: e.target.value })} />
            <details><summary className="small muted">Limits</summary><LimitsEditor compact value={a.limits || {}} onChange={(v: any) => update(i, { limits: v })} /></details>
          </div>
        ))}
        <div className="row gap-s">
          <Button small icon={<Plus size={13} />} onClick={add}>Add agent</Button>
          {on && <Button small kind="ghost" onClick={() => onChange([])}>No team</Button>}
        </div>
      </div>
    </Popover>
  );
}

export function ConnectorPicker({ value = [], onChange }: any) {
  const [list, setList] = useState<any[]>([]);
  useEffect(() => { api.get("/connectors").then((r) => setList(r.connectors.filter((c: any) => c.enabled))); }, []);
  const label = value.length ? `${value.length} connector${value.length > 1 ? "s" : ""}` : "No connectors";
  return (
    <Popover label={label} icon={<Plug size={14} />} title="Connectors (MCP) this chat may use">
      <div className="menu">
        {list.length === 0 && <div className="menu-sub pad">No connectors yet. Add them in Plugins & connectors.</div>}
        {list.map((c: any) => (
          <label key={c.id} className="menu-item row gap-s">
            <input type="checkbox" checked={value.includes(c.id)} disabled={!c.ready}
              onChange={(e) => onChange(e.target.checked ? [...value, c.id] : value.filter((x: string) => x !== c.id))} />
            <div><div className="menu-title">{c.name}</div><div className="menu-sub">{c.ready ? (c.description || c.source) : "Needs its key — set it up in Plugins"}</div></div>
          </label>
        ))}
      </div>
    </Popover>
  );
}

export function FolderPicker({ value = [], onChange, inherited = [] }: any) {
  const [typed, setTyped] = useState("");
  const add = (p: string) => p && !value.includes(p) && onChange([...value, p]);
  const label = value.length + inherited.length ? `${value.length + inherited.length} folder${value.length + inherited.length > 1 ? "s" : ""}` : "No folders";
  return (
    <Popover label={label} icon={<FolderOpen size={14} />} title="Folders the agent may read and write">
      <div className="menu pad">
        {inherited.map((f: string) => <div key={f} className="folder-row muted" title="From the project">{f}</div>)}
        {value.map((f: string) => (
          <div key={f} className="folder-row"><span className="ellipsis">{f}</span>
            <button className="icon-btn" onClick={() => onChange(value.filter((x: string) => x !== f))}><X size={12} /></button></div>
        ))}
        {desktop ? <Button small onClick={async () => add(await desktop.pickFolder())}>Add a folder…</Button> :
          <div className="row gap-s"><input className="input" value={typed} onChange={(e) => setTyped(e.target.value)} placeholder="Folder path" />
            <Button small onClick={() => { add(typed); setTyped(""); }}>Add</Button></div>}
        <div className="menu-sub">The agent can only touch files inside these folders (and the chat's own workspace). Changes ask you first.</div>
      </div>
    </Popover>
  );
}

export function Composer({ onSend, running, onStop, status, placeholder, autoFocus, options, initialText, followUps }: any) {
  const [text, setText] = useState(initialText || "");
  const [files, setFiles] = useState<File[]>([]);
  const ref = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  useEffect(() => { if (autoFocus) ref.current?.focus(); }, []);
  useEffect(() => { if (initialText) setText(initialText); }, [initialText]);
  useEffect(() => {
    const el = ref.current;
    if (el) { el.style.height = "auto"; el.style.height = Math.min(el.scrollHeight, 260) + "px"; }
  }, [text]);
  const send = async (mode?: string) => {
    if ((running && !followUps) || (!text.trim() && !files.length)) return;
    const t = text, f = files;
    setText(""); setFiles([]);
    try { await onSend(t, f, mode); } catch (e) { setText(t); setFiles(f); }
  };
  const hasText = !!text.trim() || files.length > 0;
  return (
    <div className="composer" onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => { e.preventDefault(); setFiles([...files, ...Array.from(e.dataTransfer.files)]); }}>
      {files.length > 0 && (
        <div className="attach-row">
          {files.map((f, i) => (
            <span key={i} className="attach"><Paperclip size={12} />{f.name} <span className="muted">{fmtBytes(f.size)}</span>
              <button className="icon-btn" onClick={() => setFiles(files.filter((_, j) => j !== i))}><X size={12} /></button></span>
          ))}
        </div>
      )}
      <textarea ref={ref} value={text} rows={1}
        placeholder={running && followUps ? "Add a follow-up… Enter queues it for when this reply ends; Steer hands it to the agent now"
          : placeholder || "Message the agent… (Shift+Enter for a new line)"}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(running ? "queue" : undefined); } }} />
      <div className="composer-bar">
        <div className="row gap-s wrap">
          <button className="icon-btn" title="Attach files" onClick={() => fileRef.current?.click()}><Paperclip size={16} /></button>
          <input ref={fileRef} type="file" multiple hidden onChange={(e) => { setFiles([...files, ...Array.from(e.target.files || [])]); e.target.value = ""; }} />
          {options}
        </div>
        <div className="row gap-s">
          {status && <span className="status-text">{status}</span>}
          {running && followUps && hasText && <>
            <Button small icon={<CornerDownRight size={13} />} onClick={() => send("steer")} title="Give this to the agent at its next step, without waiting for the reply to end">Steer</Button>
            <Button small kind="primary" icon={<ListPlus size={13} />} onClick={() => send("queue")} title="Send this as soon as the current reply ends">Queue</Button>
          </>}
          {running ? <Button kind="danger" small icon={<Square size={13} />} onClick={onStop}>Stop</Button>
            : <Button kind="primary" small icon={<Send size={14} />} onClick={() => send()} disabled={!hasText}>Send</Button>}
        </div>
      </div>
    </div>
  );
}

export function ThinkingToggle({ value, onChange }: any) {
  return (
    <span className="chip chip-toggle" title="Let the model think before answering (slower, sometimes better). Works with models that support it.">
      <Lightbulb size={14} /><Toggle checked={value} onChange={onChange} label="Think" />
    </span>
  );
}

export async function uploadAll(chatId: string, files: File[]) {
  const out = [];
  for (const f of files) out.push(await api.upload(`/chats/${chatId}/upload`, f));
  return out;
}
