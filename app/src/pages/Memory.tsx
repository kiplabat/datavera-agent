import React, { useEffect, useState } from "react";
import { Brain, Pin, Trash2 } from "lucide-react";
import { api, fmtTime } from "../api";
import { go, useApp, useEvent } from "../store";
import { Badge, Button, PageHeader, Spinner, Tabs, Toggle } from "../components/ui";

const SCOPES = [
  { id: "global", label: "About you" }, { id: "project", label: "Projects" }, { id: "chat", label: "Chats" }, { id: "harness", label: "Task lessons" },
];

export default function Memory() {
  const { toast, boot, refreshBoot } = useApp();
  const [scope, setScope] = useState("global");
  const [data, setData] = useState<any>(null);
  const [text, setText] = useState("");
  const [editing, setEditing] = useState<string>("");
  const [draft, setDraft] = useState("");
  const load = () => api.get("/memory").then(setData);
  useEffect(() => { load(); }, []);
  useEvent((e) => { if (["memory_changed", "memory_learned"].includes(e.kind)) load(); });
  if (!data) return <div className="page"><Spinner /></div>;
  const rows = data.memories.filter((m: any) => m.scope === scope);
  const setSetting = async (key: string, v: any) => { await api.put("/settings", { [key]: v }); load(); refreshBoot(); };
  return (
    <div className="page">
      <PageHeader title="Memory" subtitle="What the agent has learned. It reads these in every relevant chat, so it gets better the more you use it. You can edit or delete anything." />
      <div className="card row gap-m wrap">
        <Toggle checked={data.enabled} onChange={(v: boolean) => setSetting("memory_enabled", v)} label="Use memory in chats" />
        <Toggle checked={data.learn} onChange={(v: boolean) => setSetting("learn_enabled", v)} label="Learn automatically after chats and tasks" />
        <label className="row gap-s small">Learning model
          <select className="input" value={boot.settings.learning_model || ""} onChange={(e) => setSetting("learning_model", e.target.value)}>
            <option value="">Same as the chat (no extra loading)</option>
            {boot.models.map((m: any) => <option key={m.id} value={m.id}>{m.name}</option>)}
          </select></label>
      </div>
      <p className="muted small">Learning happens when a chat has been quiet for a short while and the model is free. It keeps at most a few short notes per conversation, skips duplicates, and never stores keys, passwords or card numbers.</p>
      <Tabs value={scope} onChange={setScope} tabs={SCOPES.map((s) => ({ ...s, count: data.memories.filter((m: any) => m.scope === s.id).length }))} />
      {scope === "global" && (
        <div className="row gap-s mb">
          <input className="input" value={text} onChange={(e) => setText(e.target.value)} placeholder="Tell the agent something to always remember, e.g. 'I write in British English'" />
          <Button kind="primary" disabled={!text} onClick={async () => { try { await api.post("/memory", { scope: "global", text }); setText(""); load(); } catch (e: any) { toast(e.message, "error"); } }}>Remember</Button>
        </div>
      )}
      <div className="card">
        {rows.length === 0 && <div className="muted"><Brain size={16} /> Nothing here yet.</div>}
        {rows.map((m: any) => (
          <div key={m.id} className="memory-row">
            {editing === m.id ? (
              <input className="input" autoFocus value={draft} onChange={(e) => setDraft(e.target.value)}
                onBlur={async () => { try { await api.put(`/memory/${m.id}`, { text: draft }); } catch (e: any) { toast(e.message, "error"); } setEditing(""); load(); }}
                onKeyDown={(e) => e.key === "Enter" && (e.target as any).blur()} />
            ) : (
              <div className="grow" onClick={() => { setEditing(m.id); setDraft(m.text); }} title="Click to edit">
                {m.text}
                <div className="small muted">{m.scope_name && (m.scope === "chat" ? <a onClick={(e) => { e.stopPropagation(); go("chat", m.scope_id); }}>{m.scope_name}</a> : m.scope_name)}
                  {m.scope_name ? " · " : ""}{m.source === "learned" ? "learned" : m.source === "agent" ? "saved by the agent" : "added by you"} · {fmtTime(m.updated)}{m.uses ? ` · used ${m.uses}×` : ""}</div>
              </div>
            )}
            <div className="row gap-s">
              {m.kind !== "fact" && <Badge>{m.kind}</Badge>}
              <button className={`icon-btn${m.pinned ? " accent" : ""}`} title="Pin (always included first)" onClick={async () => { await api.put(`/memory/${m.id}`, { pinned: m.pinned ? 0 : 1 }); load(); }}><Pin size={14} /></button>
              <button className="icon-btn" title="Delete" onClick={async () => { await api.del(`/memory/${m.id}`); load(); }}><Trash2 size={14} /></button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
