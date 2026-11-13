import React, { useEffect, useState } from "react";
import { CalendarClock, Pencil, Play, Plus, Trash2 } from "lucide-react";
import { api, fmtTime } from "../api";
import { go, useApp, useEvent } from "../store";
import { Badge, Button, ConfirmButton, Empty, Field, Modal, PageHeader, Spinner, Toggle } from "../components/ui";
import { ConnectorPicker, HarnessPicker, ModelPicker } from "../components/Pickers";

function Editor({ s, onClose, onDone }: any) {
  const { toast, projects } = useApp();
  const [f, setF] = useState<any>(s ? { ...s, when: s.when } : { name: "", prompt: "", when: "every day at 09:00", harness: "planner", model: "", project_id: null,
    chat_mode: "new", connectors: [], auto_approve: false, catch_up: true, only_on_ac: false, max_runs_per_day: 0, enabled: true });
  const [preview, setPreview] = useState<any>(null);
  useEffect(() => {
    const t = setTimeout(() => api.post("/schedules/parse", { when: f.when }).then(setPreview).catch((e) => setPreview({ error: e.message })), 300);
    return () => clearTimeout(t);
  }, [f.when]);
  const save = async () => {
    try {
      const body = { ...f, cron: preview?.cron };
      if (s) await api.put(`/schedules/${s.id}`, body); else await api.post("/schedules", body);
      toast("Scheduled task saved", "ok"); onDone(); onClose();
    } catch (e: any) { toast(e.message, "error"); }
  };
  return (
    <Modal wide title={s ? "Edit scheduled task" : "New scheduled task"} onClose={onClose} footer={<Button kind="primary" disabled={!f.name || !preview?.cron} onClick={save}>Save</Button>}>
      <Field label="Name"><input className="input" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="Morning data check" /></Field>
      <Field label="What should it do?" help="Write it for a run with nobody watching: the goal, which folders or sources, what to produce, what to do if there is nothing new.">
        <textarea className="input" rows={5} value={f.prompt} onChange={(e) => setF({ ...f, prompt: e.target.value })} />
      </Field>
      <Field label="When" help={preview?.error ? <span className="bad">{preview.error}</span> : preview ? `${preview.when} · next: ${new Date(preview.next).toLocaleString()}` : ""}>
        <input className="input" value={f.when} onChange={(e) => setF({ ...f, when: e.target.value })} placeholder="every day at 08:30 · weekdays at 9 · every 3 hours · every monday at 10:00" />
      </Field>
      <div className="row gap-m wrap">
        <Field label="Harness"><HarnessPicker value={f.harness} onChange={(v: string) => setF({ ...f, harness: v })} /></Field>
        <Field label="Model"><ModelPicker value={f.model} onChange={(v: string) => setF({ ...f, model: v })} /></Field>
        <Field label="Connectors"><ConnectorPicker value={f.connectors || []} onChange={(v: string[]) => setF({ ...f, connectors: v })} /></Field>
        <Field label="Project">
          <select className="input" value={f.project_id || ""} onChange={(e) => setF({ ...f, project_id: e.target.value || null })}>
            <option value="">— none —</option>{projects.map((p: any) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </Field>
        <Field label="Results">
          <select className="input" value={f.chat_mode} onChange={(e) => setF({ ...f, chat_mode: e.target.value })}>
            <option value="new">A new chat each run</option><option value="same">Keep adding to one chat</option>
          </select>
        </Field>
      </div>
      <div className="stack-s">
        <Toggle checked={f.auto_approve} onChange={(v: boolean) => setF({ ...f, auto_approve: v })} label="Approve changes automatically (file writes, Python, connector actions) — otherwise the run waits for you" />
        <Toggle checked={f.catch_up} onChange={(v: boolean) => setF({ ...f, catch_up: v })} label="If the computer was off at the time, run once when the app starts" />
        <Toggle checked={f.only_on_ac} onChange={(v: boolean) => setF({ ...f, only_on_ac: v })} label="Only when plugged in" />
        <Field label="Maximum runs per day (0 = no limit)"><input className="input narrow" type="number" min={0} value={f.max_runs_per_day} onChange={(e) => setF({ ...f, max_runs_per_day: Number(e.target.value) })} /></Field>
      </div>
    </Modal>
  );
}

export default function Schedules() {
  const { toast } = useApp();
  const [list, setList] = useState<any[] | null>(null);
  const [edit, setEdit] = useState<any>(null);
  const load = () => api.get("/schedules").then((r) => setList(r.schedules));
  useEffect(() => { load(); }, []);
  useEvent((e) => { if (["schedules_changed", "schedules_tick", "job"].includes(e.kind)) load(); });
  if (!list) return <div className="page"><Spinner /></div>;
  return (
    <div className="page">
      <PageHeader title="Schedules" subtitle="Tasks that run on their own, each in its own chat, with the harness and model you choose. They run while Datavera Agent is open (it can start with Windows and stay in the tray — see Settings)."
        actions={<Button kind="primary" icon={<Plus size={14} />} onClick={() => setEdit({})}>New scheduled task</Button>} />
      {list.length === 0 && <Empty icon={<CalendarClock size={32} />} title="No scheduled tasks yet">Create one here, ask the agent in any chat ("every morning, …"), or add a ready-made one from a plugin.</Empty>}
      {list.map((s) => (
        <div key={s.id} className="card schedule-row">
          <div className="grow">
            <div className="row gap-s"><h3>{s.name}</h3>{s.running && <Badge kind="accent">running</Badge>}{s.auto_approve ? <Badge>auto-approve</Badge> : null}</div>
            <div className="muted small">{s.when} · next {s.enabled ? fmtTime(s.next_run) : "—"}{s.last_run ? ` · last ${fmtTime(s.last_run)}` : ""} · {s.harness || "default harness"}{s.model ? ` · ${s.model}` : ""}</div>
            <div className="small clamp">{s.prompt}</div>
          </div>
          <div className="row gap-s">
            <Toggle checked={!!s.enabled} onChange={async (v: boolean) => { await api.put(`/schedules/${s.id}`, { enabled: v }); load(); }} />
            <Button small icon={<Play size={13} />} disabled={s.running} onClick={async () => { try { const r = await api.post(`/schedules/${s.id}/run`); go("chat", r.chat_id); } catch (e: any) { toast(e.message, "error"); } }}>Run now</Button>
            <Button small icon={<Pencil size={13} />} onClick={() => setEdit(s)}>Edit</Button>
            <ConfirmButton small icon={<Trash2 size={13} />} onConfirm={async () => { await api.del(`/schedules/${s.id}`); load(); }}>Delete</ConfirmButton>
          </div>
        </div>
      ))}
      {edit && <Editor s={edit.id ? edit : null} onClose={() => setEdit(null)} onDone={load} />}
    </div>
  );
}
