import React, { useEffect, useState } from "react";
import { CalendarPlus, CheckCircle2, Download, Package, Play, Plug, Plus, Trash2, Upload, XCircle } from "lucide-react";
import { api, desktop, enc, TOKEN } from "../api";
import { go, useApp, useEvent } from "../store";
import { Badge, Button, ConfirmButton, Field, FolderInput, Modal, PageHeader, Spinner, Tabs, Toggle } from "../components/ui";

function SettingField({ field, value, options, onChange }: any) {
  if (field.type === "secret") {
    return (
      <Field label={field.label} help={field.help}>
        {value === true && <div className="row gap-s"><Badge kind="ok">Saved securely</Badge><span className="muted small">Paste a new one below to replace it.</span></div>}
        <input className="input" type="password" autoComplete="off" placeholder={value === true ? "••••••••••••" : "Paste here"} onChange={(e) => onChange(e.target.value)} />
      </Field>
    );
  }
  if (field.type === "boolean") return <Field help={field.help}><Toggle checked={value ?? field.default} onChange={onChange} label={field.label} /></Field>;
  if (field.type === "textarea") return <Field label={field.label} help={field.help}><textarea className="input" rows={3} value={value || ""} onChange={(e) => onChange(e.target.value)} /></Field>;
  if (field.type === "folder") return <Field label={field.label} help={field.help}><FolderInput value={value} onChange={onChange} /></Field>;
  if (field.type === "select") {
    const opts = options?.length ? options : field.options || [];
    return (
      <Field label={field.label} help={field.help}>
        <select className="input" value={value || ""} onChange={(e) => onChange(e.target.value)} disabled={!opts.length}>
          {!opts.length && <option value="">(press Check and connect first)</option>}
          {opts.map((o: string) => <option key={o} value={o}>{o}</option>)}
        </select>
      </Field>
    );
  }
  return <Field label={field.label} help={field.help}><input className="input" value={value || ""} onChange={(e) => onChange(e.target.value)} /></Field>;
}

function PluginCard({ p, onChange }: any) {
  const { toast, refreshBoot } = useApp();
  const [open, setOpen] = useState(!p.configured && p.enabled && p.id === "datavera");
  const [values, setValues] = useState<any>({});
  const [busy, setBusy] = useState("");
  const [setup, setSetup] = useState<any>(p.setup_state?.message ? p.setup_state : null);
  const save = async () => {
    setBusy("save");
    try {
      await api.put(`/plugins/${p.id}/settings`, values);
      setValues({});
      if (p.setup) {
        setBusy("setup");
        const r = await api.post(`/plugins/${p.id}/setup`);
        setSetup(r);
        toast(r.ok ? "Connected" : r.message, r.ok ? "ok" : "error");
      } else toast("Saved", "ok");
      onChange();
    } catch (e: any) { toast(e.message, "error"); }
    setBusy("");
  };
  const runAction = async (a: any) => {
    try { await api.post(`/plugins/${p.id}/actions/${a.id}`); toast(`${a.label} started — see Activity`, "info"); } catch (e: any) { toast(e.message, "error"); }
  };
  const useTemplate = async (t: any) => {
    try {
      await api.post("/schedules", { name: t.name, when: t.when, prompt: t.prompt, harness: t.harness, connectors: t.connectors || [],
        only_on_ac: t.only_on_ac, max_runs_per_day: t.max_runs_per_day, auto_approve: t.auto_approve, enabled: false });
      toast("Scheduled task added (switched off). Review and switch it on in Schedules.", "ok");
      go("schedules");
    } catch (e: any) { toast(e.message, "error"); }
  };
  if (p.error) return <div className="card"><b>{p.name}</b> <Badge kind="bad">broken</Badge><div className="error-line">{p.error}</div></div>;
  const options = p.setup_state?.options || setup?.options || {};
  return (
    <div className="card plugin-card">
      <div className="row between">
        <div className="row gap-s"><Package size={18} className="accent" /><div><h3>{p.name} <span className="muted small">v{p.version}</span></h3>
          <div className="muted small">{p.source === "builtin" ? "Built in" : "Installed by you"}{p.author ? ` · ${p.author}` : ""}</div></div></div>
        <div className="row gap-s">
          {p.enabled && (p.configured ? <Badge kind="ok">Ready</Badge> : <Badge kind="warn">Needs setup</Badge>)}
          <Toggle checked={p.enabled} onChange={async (v: boolean) => { await api.put(`/plugins/${p.id}/enabled`, { enabled: v }); onChange(); refreshBoot(); }} />
        </div>
      </div>
      <p>{p.description}</p>
      <div className="row gap-s wrap small">
        {p.connectors?.length > 0 && <Badge>{p.connectors.length} connector{p.connectors.length > 1 ? "s" : ""}</Badge>}
        {p.skills?.length > 0 && <Badge>{p.skills.length} skills</Badge>}
        {p.harness_list?.length > 0 && <Badge>{p.harness_list.length} harness{p.harness_list.length > 1 ? "es" : ""}</Badge>}
        <button className="link" onClick={() => setOpen(!open)}>{open ? "Hide details" : "Settings & details"}</button>
      </div>
      {open && p.enabled && (
        <div className="plugin-body">
          {p.settings?.length > 0 && (
            <div className="stack-s">
              {p.settings.map((f: any) => (
                <SettingField key={f.key} field={f} options={options[f.key]} value={f.key in values ? values[f.key] : p.values[f.key]}
                  onChange={(v: any) => setValues({ ...values, [f.key]: v })} />
              ))}
              <div className="row gap-s">
                <Button kind="primary" busy={!!busy} onClick={save}>{p.setup ? "Save, check and connect" : "Save"}</Button>
                {busy === "setup" && <span className="muted small">Checking with the service… the first time it also builds your catalogue (about a minute).</span>}
              </div>
              {setup?.message && <div className={setup.ok ? "ok-line" : "error-line"}>{setup.ok ? <CheckCircle2 size={14} /> : <XCircle size={14} />} {setup.message}</div>}
            </div>
          )}
          {p.actions?.length > 0 && (
            <div className="mt"><div className="small muted">Actions</div>
              <div className="row gap-s wrap">{p.actions.map((a: any) => <Button key={a.id} small icon={<Play size={13} />} title={a.help} onClick={() => runAction(a)}>{a.label}</Button>)}</div></div>
          )}
          {p.harness_list?.length > 0 && (
            <div className="mt"><div className="small muted">Harnesses</div>
              {p.harness_list.map((h: any) => <div key={h.id} className="list-row" onClick={() => go("harnesses", h.id)}><b>{h.name}</b><span className="muted small ellipsis">{h.description}</span></div>)}</div>
          )}
          {p.skills?.length > 0 && (
            <div className="mt"><div className="small muted">Skills</div>
              {p.skills.map((s: any) => <div key={s.id} className="list-row" onClick={() => go("skills", s.id)}><b>{s.name}</b><span className="muted small ellipsis">{s.description}</span></div>)}</div>
          )}
          {p.schedule_templates?.length > 0 && (
            <div className="mt"><div className="small muted">Ready-made scheduled tasks</div>
              {p.schedule_templates.map((t: any) => (
                <div key={t.name} className="list-row"><span><b>{t.name}</b> <span className="muted small">{t.when}</span></span>
                  <Button small icon={<CalendarPlus size={13} />} onClick={() => useTemplate(t)}>Add</Button></div>
              ))}</div>
          )}
          <div className="row gap-s mt">
            <Button small icon={<Download size={13} />} onClick={() => window.open(`/api/plugins/${p.id}/export?token=${enc(TOKEN)}`)}>Export as zip</Button>
            {p.source === "user" && <ConfirmButton small icon={<Trash2 size={13} />} onConfirm={async () => { await api.del(`/plugins/${p.id}`); onChange(); }}>Remove</ConfirmButton>}
          </div>
        </div>
      )}
    </div>
  );
}

function CreatePlugin({ onClose, onDone }: any) {
  const { toast } = useApp();
  const [form, setForm] = useState<any>({ name: "", description: "", skills: [], harnesses: [], connectors: [] });
  const [items, setItems] = useState<any>(null);
  useEffect(() => {
    Promise.all([api.get("/skills"), api.get("/harnesses"), api.get("/connectors")]).then(([s, h, c]) =>
      setItems({ skills: s.skills.filter((x: any) => x.source === "user"), harnesses: h.harnesses.filter((x: any) => x.source === "user"), connectors: c.connectors.filter((x: any) => x.source === "user") }));
  }, []);
  const toggle = (key: string, id: string) => setForm({ ...form, [key]: form[key].includes(id) ? form[key].filter((x: string) => x !== id) : [...form[key], id] });
  return (
    <Modal title="Create a plugin" onClose={onClose} footer={<Button kind="primary" disabled={!form.name} onClick={async () => {
      try { await api.post("/plugins-create", form); toast("Plugin created", "ok"); onDone(); onClose(); } catch (e: any) { toast(e.message, "error"); }
    }}>Create</Button>}>
      <p className="muted small">Bundle your own skills, harnesses and connectors into one plugin you can export and share. Keys are not included; whoever installs it enters their own.</p>
      <Field label="Name"><input className="input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
      <Field label="Description"><input className="input" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></Field>
      {!items ? <Spinner /> : ["skills", "harnesses", "connectors"].map((k) => (
        <Field key={k} label={k[0].toUpperCase() + k.slice(1)}>
          {items[k].length === 0 && <div className="muted small">None of your own yet.</div>}
          {items[k].map((x: any) => <label key={x.id} className="check"><input type="checkbox" checked={form[k].includes(x.id)} onChange={() => toggle(k, x.id)} /> {x.name}</label>)}
        </Field>
      ))}
    </Modal>
  );
}

function Plugins_({ }: any) {
  const { toast } = useApp();
  const [data, setData] = useState<any>(null);
  const [creating, setCreating] = useState(false);
  const load = () => api.get("/plugins").then(setData);
  useEffect(() => { load(); }, []);
  useEvent((e) => { if (e.kind === "plugins_changed") load(); });
  return (
    <div className="stack">
      <div className="row gap-s">
        <label className="btn btn-default"><Upload size={14} /><span>Install from zip</span>
          <input type="file" accept=".zip" hidden onChange={async (e) => {
            const f = e.target.files?.[0]; e.target.value = "";
            if (f) try { await api.upload("/plugins-upload", f); toast("Plugin installed", "ok"); load(); } catch (err: any) { toast(err.message, "error"); }
          }} /></label>
        <Button icon={<Plus size={14} />} onClick={() => setCreating(true)}>Create a plugin</Button>
      </div>
      {!data ? <Spinner /> : data.plugins.map((p: any) => <PluginCard key={p.id} p={p} onChange={load} />)}
      {creating && <CreatePlugin onClose={() => setCreating(false)} onDone={load} />}
    </div>
  );
}

function ConnectorForm({ preset, onClose, onDone }: any) {
  const { toast } = useApp();
  const [f, setF] = useState<any>({ name: preset.name.replace(/ \(.*\)/, ""), transport: preset.transport, url: preset.url || "",
    command: preset.command || "", args: (preset.args || []).join(" "), auth: preset.auth || "none", token: "", description: preset.description || "" });
  const save = async () => {
    try {
      const body = { ...f, args: f.args ? f.args.match(/(?:[^\s"]+|"[^"]*")+/g).map((a: string) => a.replace(/^"|"$/g, "")) : [] };
      const c = await api.post("/connectors", body);
      const t = await api.post(`/connectors/${enc(c.id)}/test`);
      toast(t.ok ? `Connected: ${t.tools.length} tools` : `Saved, but the test failed: ${t.error}`, t.ok ? "ok" : "error");
      onDone(); onClose();
    } catch (e: any) { toast(e.message, "error"); }
  };
  return (
    <Modal title={`Add connector: ${preset.name}`} onClose={onClose} footer={<Button kind="primary" onClick={save}>Save and test</Button>}>
      <p className="muted small">{preset.description}</p>
      <Field label="Name"><input className="input" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
      {f.transport === "http" ? <>
        <Field label="Server URL" help="Must start with https://"><input className="input" value={f.url} onChange={(e) => setF({ ...f, url: e.target.value })} placeholder="https://example.com/mcp" /></Field>
        <Field label="Authentication">
          <select className="input" value={f.auth} onChange={(e) => setF({ ...f, auth: e.target.value })}><option value="none">None</option><option value="bearer">API key / bearer token</option></select>
        </Field>
        {f.auth === "bearer" && <Field label="Key or token" help="Stored in Windows Credential Manager."><input className="input" type="password" value={f.token} onChange={(e) => setF({ ...f, token: e.target.value })} /></Field>}
      </> : <>
        <Field label="Command" help="{node} = the app's built-in Node, {python} = the app's Python. npx/uvx need Node.js/uv installed."><input className="input" value={f.command} onChange={(e) => setF({ ...f, command: e.target.value })} placeholder="npx" /></Field>
        <Field label="Arguments"><input className="input" value={f.args} onChange={(e) => setF({ ...f, args: e.target.value })} placeholder="-y @some/mcp-server" /></Field>
      </>}
    </Modal>
  );
}

function Connectors() {
  const { toast } = useApp();
  const [data, setData] = useState<any>(null);
  const [preset, setPreset] = useState<any>(null);
  const [tools, setTools] = useState<Record<string, any>>({});
  const load = () => api.get("/connectors").then(setData);
  useEffect(() => { load(); }, []);
  useEvent((e) => { if (["connectors_changed", "plugins_changed"].includes(e.kind)) load(); });
  const test = async (c: any) => {
    setTools({ ...tools, [c.id]: "loading" });
    const r = await api.post(`/connectors/${enc(c.id)}/test`);
    setTools((t) => ({ ...t, [c.id]: r }));
  };
  const update = async (c: any, values: any) => { try { await api.put(`/connectors/${enc(c.id)}/config`, values); load(); } catch (e: any) { toast(e.message, "error"); } };
  if (!data) return <Spinner />;
  return (
    <div className="stack">
      <div className="card">
        <h3>Add a connector</h3>
        <p className="muted small">Connectors are MCP servers: they give the agent tools for other apps and services. Choose them per chat (the plug menu) or per project.</p>
        <div className="row gap-s wrap">{data.presets.map((p: any) => <Button key={p.key} icon={<Plug size={13} />} onClick={() => setPreset(p)}>{p.name}</Button>)}</div>
      </div>
      {data.connectors.map((c: any) => {
        const t = tools[c.id];
        return (
          <div key={c.id} className="card">
            <div className="row between">
              <div><h3>{c.name}</h3><div className="muted small">{c.transport === "http" ? c.url : `${c.command} ${(c.args || []).join(" ")}`} · {c.source === "user" ? "yours" : `from plugin ${c.plugin}`}</div></div>
              <div className="row gap-s">{!c.ready && <Badge kind="warn">needs key</Badge>}<Toggle checked={c.enabled} onChange={(v: boolean) => update(c, { enabled: v })} /></div>
            </div>
            <div className="row gap-m wrap mt">
              <Field label="Ask before">
                <select className="input" value={c.approval} onChange={(e) => update(c, { approval: e.target.value })}>
                  <option value="changes">Actions that change things</option><option value="always">Every action</option><option value="never">Never ask</option>
                </select>
              </Field>
              {c.source === "user" && c.auth === "bearer" && <Field label="Replace key"><input className="input" type="password" placeholder={c.has_token ? "saved ••••" : "paste key"} onBlur={(e) => e.target.value && update(c, { token: e.target.value })} /></Field>}
            </div>
            <div className="row gap-s">
              <Button small busy={t === "loading"} onClick={() => test(c)}>Test & list tools</Button>
              {c.source === "user" && <ConfirmButton small icon={<Trash2 size={13} />} onConfirm={async () => { await api.del(`/connectors/${enc(c.id)}/delete`); load(); }}>Remove</ConfirmButton>}
              {c.source !== "user" && <button className="link" onClick={() => go("plugins")}>Settings are in its plugin</button>}
            </div>
            {t && t !== "loading" && (t.ok ? (
              <div className="tool-list">{t.tools.map((x: any) => <div key={x.name} className="small"><b>{x.name}</b> {x.readonly ? <Badge kind="ok">read-only</Badge> : <Badge kind="warn">changes</Badge>} <span className="muted">{x.description.slice(0, 140)}</span></div>)}</div>
            ) : <div className="error-line">{t.error}</div>)}
          </div>
        );
      })}
      {preset && <ConnectorForm preset={preset} onClose={() => setPreset(null)} onDone={load} />}
    </div>
  );
}

export default function Plugins({ tab }: { tab?: string }) {
  const t = tab || "plugins";
  return (
    <div className="page">
      <PageHeader title="Plugins & connectors" subtitle="Plugins bundle connectors, skills, harnesses and ready-made tasks. Datavera ships built in." />
      <Tabs value={t} onChange={(v: string) => go("plugins", v)} tabs={[{ id: "plugins", label: "Plugins" }, { id: "connectors", label: "Connectors (MCP)" }]} />
      {t === "plugins" ? <Plugins_ /> : <Connectors />}
    </div>
  );
}
