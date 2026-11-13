import React, { useEffect, useState } from "react";
import { Brain, FolderOpen, MessageSquarePlus, Trash2, X } from "lucide-react";
import { api, desktop, fmtTime } from "../api";
import { go, useApp } from "../store";
import { Button, ConfirmButton, Field, FolderInput, PageHeader, Spinner, Toggle } from "../components/ui";
import { ConnectorPicker, HarnessPicker, ModelPicker } from "../components/Pickers";

export default function ProjectView({ projectId }: { projectId?: string }) {
  const { toast, refreshProjects } = useApp();
  const isNew = !projectId || projectId === "new";
  const [p, setP] = useState<any>(isNew ? { name: "", instructions: "", folders: [], connectors: [], memory_enabled: 1 } : null);
  const [saving, setSaving] = useState(false);
  const [newFolder, setNewFolder] = useState("");
  const [newMemory, setNewMemory] = useState("");

  const load = async () => { if (!isNew) setP(await api.get(`/projects/${projectId}`)); };
  useEffect(() => { load(); }, [projectId]);

  const save = async () => {
    setSaving(true);
    try {
      const body = { name: p.name || "New project", instructions: p.instructions, folders: p.folders, default_model: p.default_model,
        default_harness: p.default_harness, connectors: p.connectors, memory_enabled: !!p.memory_enabled };
      const out = isNew ? await api.post("/projects", body) : await api.put(`/projects/${projectId}`, body);
      await refreshProjects();
      toast("Project saved", "ok");
      if (isNew) go("project", out.id); else load();
    } catch (e: any) { toast(e.message, "error"); } finally { setSaving(false); }
  };

  if (!p) return <div className="center-screen"><Spinner /></div>;
  return (
    <div className="page">
      <PageHeader title={isNew ? "New project" : p.name}
        subtitle="A project keeps related chats together with shared instructions, folders, defaults and memory."
        actions={!isNew && <Button kind="primary" icon={<MessageSquarePlus size={15} />} onClick={() => go("home", projectId)}>New chat in project</Button>} />
      <div className="grid-2">
        <div className="card">
          <Field label="Name"><input className="input" value={p.name} onChange={(e) => setP({ ...p, name: e.target.value })} placeholder="e.g. Monthly energy report" /></Field>
          <Field label="Instructions" help="Always given to the agent in this project: goals, tone, conventions, what to avoid.">
            <textarea className="input" rows={7} value={p.instructions} onChange={(e) => setP({ ...p, instructions: e.target.value })}
              placeholder="e.g. Reports are for the board: plain English, numbers rounded, always cite the source file." />
          </Field>
          <Field label="Folders" help="Every chat in this project can read and write these folders (changes still ask first).">
            {(p.folders || []).map((f: string) => (
              <div key={f} className="folder-row"><FolderOpen size={14} /><span className="ellipsis">{f}</span>
                <button className="icon-btn" onClick={() => setP({ ...p, folders: p.folders.filter((x: string) => x !== f) })}><X size={13} /></button></div>
            ))}
            <div className="row gap-s"><FolderInput value={newFolder} onChange={setNewFolder} />
              <Button onClick={() => { if (newFolder) { setP({ ...p, folders: [...p.folders, newFolder] }); setNewFolder(""); } }}>Add</Button></div>
          </Field>
          <div className="row gap-m wrap">
            <Field label="Default harness"><HarnessPicker value={p.default_harness} onChange={(v: string) => setP({ ...p, default_harness: v })} /></Field>
            <Field label="Default model"><ModelPicker value={p.default_model} onChange={(v: string) => setP({ ...p, default_model: v })} /></Field>
            <Field label="Connectors"><ConnectorPicker value={p.connectors || []} onChange={(v: string[]) => setP({ ...p, connectors: v })} /></Field>
          </div>
          <Toggle checked={!!p.memory_enabled} onChange={(v: boolean) => setP({ ...p, memory_enabled: v })} label="Use and learn project memory" />
          <div className="row gap-s mt">
            <Button kind="primary" busy={saving} onClick={save}>{isNew ? "Create project" : "Save changes"}</Button>
            {!isNew && <ConfirmButton icon={<Trash2 size={14} />} onConfirm={async () => { await api.del(`/projects/${projectId}`); await refreshProjects(); go("home"); }}>Delete project</ConfirmButton>}
          </div>
        </div>
        {!isNew && (
          <div className="stack">
            <div className="card">
              <h3>Chats</h3>
              {p.chats.length === 0 && <p className="muted">No chats yet.</p>}
              {p.chats.map((c: any) => (
                <div key={c.id} className="list-row" onClick={() => go("chat", c.id)}>
                  <span className="ellipsis">{c.title || "New chat"}</span><span className="muted small">{fmtTime(c.updated)}</span>
                </div>
              ))}
            </div>
            <div className="card">
              <h3><Brain size={16} /> Project memory</h3>
              <p className="muted small">Learned from chats in this project, or added by you. Used in every chat here.</p>
              {p.memories.map((m: any) => (
                <div key={m.id} className="memory-row"><span>{m.text}</span>
                  <button className="icon-btn" onClick={async () => { await api.del(`/memory/${m.id}`); load(); }}><X size={13} /></button></div>
              ))}
              <div className="row gap-s mt"><input className="input" value={newMemory} placeholder="Add something the agent should know"
                onChange={(e) => setNewMemory(e.target.value)} />
                <Button onClick={async () => { try { await api.post("/memory", { scope: "project", scope_id: projectId, text: newMemory }); setNewMemory(""); load(); } catch (e: any) { toast(e.message, "error"); } }}>Add</Button></div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
