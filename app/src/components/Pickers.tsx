import React, { useEffect, useRef, useState } from "react";
import { Bot, ChevronDown, Cpu, FolderOpen, Paperclip, Plug, Send, Square, X, Lightbulb } from "lucide-react";
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

export function ModelPicker({ value, onChange }: any) {
  const { boot } = useApp();
  const models = boot?.models || [];
  const current = models.find((m: any) => m.id === value) || models.find((m: any) => m.id === boot?.default_model);
  return (
    <Popover label={current ? current.name : "No model yet"} icon={<Cpu size={14} />} title="Model (runs on this computer)">
      {(close: any) => (
        <div className="menu">
          {models.length === 0 && <div className="menu-item" onClick={() => (location.hash = "/models/discover")}>Download a model…</div>}
          {models.map((m: any) => (
            <div key={m.id} className={`menu-item${current?.id === m.id ? " selected" : ""}`} onClick={() => { onChange(m.id); close(); }}>
              <div className="menu-title">{m.name}</div>
              <div className="menu-sub">{m.quant} · {fmtBytes(m.size)} · {(m.settings?.compute || "auto").toUpperCase()}{m.id === boot.default_model ? " · default" : ""}</div>
            </div>
          ))}
        </div>
      )}
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

export function Composer({ onSend, running, onStop, status, placeholder, autoFocus, options, initialText }: any) {
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
  const send = async () => {
    if (running || (!text.trim() && !files.length)) return;
    const t = text, f = files;
    setText(""); setFiles([]);
    try { await onSend(t, f); } catch (e) { setText(t); setFiles(f); throw e; }
  };
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
      <textarea ref={ref} value={text} rows={1} placeholder={placeholder || "Message the agent… (Shift+Enter for a new line)"}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
      <div className="composer-bar">
        <div className="row gap-s wrap">
          <button className="icon-btn" title="Attach files" onClick={() => fileRef.current?.click()}><Paperclip size={16} /></button>
          <input ref={fileRef} type="file" multiple hidden onChange={(e) => { setFiles([...files, ...Array.from(e.target.files || [])]); e.target.value = ""; }} />
          {options}
        </div>
        <div className="row gap-s">
          {status && <span className="status-text">{status}</span>}
          {running ? <Button kind="danger" small icon={<Square size={13} />} onClick={onStop}>Stop</Button>
            : <Button kind="primary" small icon={<Send size={14} />} onClick={send} disabled={!text.trim() && !files.length}>Send</Button>}
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
