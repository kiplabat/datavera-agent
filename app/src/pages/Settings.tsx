import React, { useEffect, useState } from "react";
import { FolderOpen } from "lucide-react";
import { api, desktop } from "../api";
import { useApp } from "../store";
import { Badge, Button, Field, FolderInput, PageHeader, Toggle } from "../components/ui";

export default function Settings() {
  const { boot, refreshBoot, toast } = useApp();
  const s = boot.settings;
  const [shell, setShell] = useState<any>(null);
  const [modelsDir, setModelsDir] = useState(s.models_dir || "");
  const [hf, setHf] = useState("");
  useEffect(() => { desktop?.getShellPrefs().then(setShell); }, []);
  const set = async (values: any) => { try { await api.put("/settings", values); refreshBoot(); } catch (e: any) { toast(e.message, "error"); } };
  const setShellPref = async (p: any) => setShell(await desktop.setShellPrefs(p));
  return (
    <div className="page narrow-page">
      <PageHeader title="Settings" />
      {shell && (
        <div className="card stack-s">
          <h3>Running in the background</h3>
          <Toggle checked={shell.openAtLogin} onChange={(v: boolean) => setShellPref({ openAtLogin: v })} label="Start Datavera Agent when I sign in to Windows (in the tray) — needed for schedules" />
          <Toggle checked={shell.keepInTray} onChange={(v: boolean) => setShellPref({ keepInTray: v })} label="Closing the window keeps the app running in the tray" />
        </div>
      )}
      <div className="card stack-s">
        <h3>Models</h3>
        <Field label="Models folder" help={`Where downloads go (models are large: pick a drive with space, e.g. D:\\models). Currently: ${boot.models_dir}. Existing models stay where they are.`}>
          <div className="row gap-s"><FolderInput value={modelsDir} onChange={setModelsDir} /><Button onClick={() => set({ models_dir: modelsDir })}>Save</Button></div>
        </Field>
        <div className="row gap-m wrap">
          <Field label="Default context (tokens)">
            <select className="input" value={s.default_ctx} onChange={(e) => set({ default_ctx: Number(e.target.value) })}>
              {[4096, 8192, 16384, 32768].map((c) => <option key={c} value={c}>{c.toLocaleString()}</option>)}
            </select>
          </Field>
          <Field label="Longest reply (tokens)">
            <select className="input" value={s.max_reply_tokens} onChange={(e) => set({ max_reply_tokens: Number(e.target.value) })}>
              {[1024, 2048, 4096, 8192].map((c) => <option key={c} value={c}>{c.toLocaleString()}</option>)}
            </select>
          </Field>
          <Field label="Free memory after (minutes idle)">
            <input className="input narrow" type="number" min={0} value={s.unload_after_minutes} onChange={(e) => set({ unload_after_minutes: Number(e.target.value) })} />
          </Field>
          <Field label="Models loaded at once" help="More lets agents use different models in parallel; each needs memory.">
            <input className="input narrow" type="number" min={1} max={6} value={s.max_loaded_models ?? 3} onChange={(e) => set({ max_loaded_models: Number(e.target.value) })} />
          </Field>
          <Field label="Agents at once (teams)">
            <input className="input narrow" type="number" min={1} max={8} value={s.max_parallel_agents ?? 4} onChange={(e) => set({ max_parallel_agents: Number(e.target.value) })} />
          </Field>
          <Field label="Time limit per reply (minutes)" help="Agents stop and report what they found.">
            <input className="input narrow" type="number" min={1} value={s.turn_time_limit_minutes ?? 30} onChange={(e) => set({ turn_time_limit_minutes: Number(e.target.value) })} />
          </Field>
        </div>
        <Toggle checked={s.browser_search ?? true} onChange={(v: boolean) => set({ browser_search: v })} label="When search engines return nothing useful, search again in a real browser window (Edge)" />
        <Field label="Hugging Face token (optional)" help="Only for models that require you to accept a licence on huggingface.co. Stored in Windows Credential Manager.">
          <div className="row gap-s"><input className="input" type="password" value={hf} placeholder={boot.has_hf_token ? "saved ••••" : "hf_…"} onChange={(e) => setHf(e.target.value)} />
            <Button onClick={async () => { await set({ hf_token: hf }); setHf(""); toast("Saved", "ok"); }}>Save</Button></div>
        </Field>
      </div>
      <div className="card stack-s">
        <h3>Safety</h3>
        <Toggle checked={s.allow_python} onChange={(v: boolean) => set({ allow_python: v })} label="Let the agent run Python code (always asks first unless auto-approve is on)" />
        <Field label="If nobody answers an approval, decline after (minutes)">
          <input className="input narrow" type="number" min={1} value={s.approval_timeout_minutes} onChange={(e) => set({ approval_timeout_minutes: Number(e.target.value) })} />
        </Field>
        <Field label="Most tools offered directly to the model" help="Above this, connector tools are offered through a search tool instead (keeps prompts small and fast on local models).">
          <input className="input narrow" type="number" min={4} max={128} value={s.max_native_tools} onChange={(e) => set({ max_native_tools: Number(e.target.value) })} />
        </Field>
        <div className="small">Keys and tokens are stored in: <Badge kind={boot.secrets_secure ? "ok" : "warn"}>{boot.secrets_secure ? "the system credential store" : "a private file (no credential store found)"}</Badge></div>
      </div>
      <div className="card stack-s">
        <h3>About</h3>
        <div className="small">Datavera Agent {boot.version} · llama.cpp {boot.runtime.tag}</div>
        <div className="small muted">Data folder: {boot.data_dir}</div>
        {desktop && <div className="row gap-s"><Button small icon={<FolderOpen size={13} />} onClick={() => desktop.openPath(boot.data_dir)}>Open data folder</Button>
          <Button small onClick={() => desktop.openLogs()}>Open logs</Button></div>}
      </div>
    </div>
  );
}
