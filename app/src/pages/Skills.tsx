import React, { useEffect, useState } from "react";
import { BookOpen, Copy, Lightbulb, MessageSquarePlus, Plus, Trash2, Upload } from "lucide-react";
import { api, enc } from "../api";
import { go, useApp, useEvent } from "../store";
import { Badge, Button, ConfirmButton, Field, Markdown, Modal, PageHeader, Spinner, Toggle } from "../components/ui";

export async function startAgentChat(text: string, harness = "agent") {
  const chat = await api.post("/chats", { harness });
  await api.post(`/chats/${chat.id}/messages`, { text });
  go("chat", chat.id);
}

function NewSkill({ onClose, onDone }: any) {
  const { toast } = useApp();
  const [f, setF] = useState({ name: "", description: "", body: "## Steps\n1. \n\n## Rules\n- " });
  return (
    <Modal wide title="New skill" onClose={onClose} footer={<Button kind="primary" onClick={async () => {
      try { const s = await api.post("/skills", f); toast("Skill saved", "ok"); onDone(); onClose(); go("skills", s.id); } catch (e: any) { toast(e.message, "error"); }
    }}>Save skill</Button>}>
      <Field label="Name" help="lowercase-with-dashes"><input className="input" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="monthly-report" /></Field>
      <Field label="When to use it" help="One sentence. This is what the agent sees to decide when to load the skill."><input className="input" value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} placeholder="Use when writing the monthly project report" /></Field>
      <Field label="Instructions (Markdown)"><textarea className="input mono" rows={14} value={f.body} onChange={(e) => setF({ ...f, body: e.target.value })} /></Field>
    </Modal>
  );
}

export default function Skills({ selected }: { selected?: string }) {
  const { toast } = useApp();
  const [data, setData] = useState<any>(null);
  const [detail, setDetail] = useState<any>(null);
  const [raw, setRaw] = useState("");
  const [editing, setEditing] = useState(false);
  const [creating, setCreating] = useState(false);
  const [ask, setAsk] = useState("");
  const load = () => api.get("/skills").then(setData);
  useEffect(() => { load(); }, []);
  useEvent((e) => { if (["skills_changed", "plugins_changed", "skill_suggestion"].includes(e.kind)) load(); });
  useEffect(() => {
    if (selected) api.get(`/skills/${enc(selected)}/detail`).then((d) => { setDetail(d); setRaw(d.raw); setEditing(false); }).catch(() => setDetail(null));
    else setDetail(null);
  }, [selected, data]);
  if (!data) return <div className="page"><Spinner /></div>;
  const groups = [["user", "Your skills"], ["plugin", "From plugins"], ["builtin", "Built in"]];
  return (
    <div className="page">
      <PageHeader title="Skills" subtitle="Reusable instructions. Only each skill's one-line description is in the agent's prompt; it loads the full skill when a task matches."
        actions={<>
          <label className="btn btn-default"><Upload size={14} /><span>Upload</span>
            <input type="file" accept=".zip,.md" hidden onChange={async (e) => {
              const file = e.target.files?.[0]; e.target.value = "";
              if (file) try { const s = await api.upload("/skills-upload", file); toast("Skill added", "ok"); load(); go("skills", s.id); } catch (err: any) { toast(err.message, "error"); }
            }} /></label>
          <Button icon={<Plus size={14} />} onClick={() => setCreating(true)}>Write one</Button>
        </>} />
      <div className="card ask-card">
        <MessageSquarePlus size={18} className="accent" />
        <input className="input" value={ask} onChange={(e) => setAsk(e.target.value)} placeholder="Or describe a skill and let the agent write it, e.g. 'how I like my weekly status emails'" />
        <Button kind="primary" disabled={!ask} onClick={() => startAgentChat(`Help me create a skill: ${ask}. Load the skill-creator skill first, ask me anything you need, show me the draft, then save it.`)}>Ask the agent</Button>
      </div>
      {data.suggestions.length > 0 && (
        <div className="card">
          <h3><Lightbulb size={16} /> Suggested improvements</h3>
          <p className="muted small">Learned from your corrections in chats where a skill was used.</p>
          {data.suggestions.map((s: any) => (
            <div key={s.id} className="list-row"><span><b>{s.skill}</b>: {s.suggestion}</span>
              <span className="row gap-s"><Button small kind="primary" onClick={async () => { await api.post(`/skill-suggestions/${s.id}/apply`); toast("Skill updated", "ok"); load(); }}>Apply</Button>
                <Button small kind="ghost" onClick={async () => { await api.post(`/skill-suggestions/${s.id}/dismiss`); load(); }}>Dismiss</Button></span></div>
          ))}
        </div>
      )}
      <div className="split">
        <div className="list-col">
          {groups.map(([src, title]) => {
            const items = data.skills.filter((s: any) => s.source === src);
            if (!items.length) return null;
            return (
              <div key={src} className="mb">
                <div className="small muted">{title}</div>
                {items.map((s: any) => (
                  <div key={s.id} className={`pick-row${selected === s.id ? " active" : ""}`} onClick={() => go("skills", s.id)}>
                    <BookOpen size={14} /><div className="grow"><b>{s.name}</b><div className="small muted clamp">{s.description}</div></div>
                    <span onClick={(e) => e.stopPropagation()}><Toggle checked={s.enabled} onChange={async (v: boolean) => { await api.put(`/skills/${enc(s.id)}/enabled`, { enabled: v }); load(); }} /></span>
                  </div>
                ))}
              </div>
            );
          })}
        </div>
        <div className="detail-col">
          {!detail ? <div className="muted pad">Choose a skill to read or edit it.</div> : (
            <div className="card">
              <div className="row between"><div><h2>{detail.name}</h2><div className="muted small">{detail.description}</div></div>
                <Badge>{detail.source === "plugin" ? `plugin ${detail.plugin}` : detail.source}</Badge></div>
              {detail.files?.length > 0 && <div className="small muted mt">Files: {detail.files.join(", ")}</div>}
              {editing ? (
                <>
                  <textarea className="input mono mt" rows={22} value={raw} onChange={(e) => setRaw(e.target.value)} />
                  <div className="row gap-s mt">
                    <Button kind="primary" onClick={async () => { try { await api.put(`/skills/${enc(detail.id)}/raw`, { raw }); toast("Saved (previous version kept)", "ok"); setEditing(false); load(); } catch (e: any) { toast(e.message, "error"); } }}>Save</Button>
                    <Button kind="ghost" onClick={() => { setEditing(false); setRaw(detail.raw); }}>Cancel</Button>
                  </div>
                </>
              ) : <div className="mt"><Markdown>{detail.body}</Markdown></div>}
              {!editing && (
                <div className="row gap-s mt">
                  {detail.editable ? <Button onClick={() => setEditing(true)}>Edit</Button>
                    : <Button icon={<Copy size={13} />} onClick={async () => { const s = await api.post(`/skills/${enc(detail.id)}/copy`); toast("Copied to your skills", "ok"); load(); go("skills", s.id); }}>Copy to my skills to edit</Button>}
                  <Button onClick={() => startAgentChat(`Improve my skill "${detail.name}". Load it with load_skill, ask me what isn't working, then save the improved version with save_skill under the same name.`)}>Improve with the agent</Button>
                  {detail.editable && <ConfirmButton icon={<Trash2 size={13} />} onConfirm={async () => { await api.del(`/skills/${enc(detail.id)}/delete`); go("skills"); load(); }}>Delete</ConfirmButton>}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
      {creating && <NewSkill onClose={() => setCreating(false)} onDone={load} />}
    </div>
  );
}
