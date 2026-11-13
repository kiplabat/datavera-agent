import React, { useEffect, useState } from "react";
import { Check, Cpu, Download, Plug, Sparkles } from "lucide-react";
import { api } from "../api";
import { go, useApp } from "../store";
import { Button, Progress, Spinner, fitBadge } from "../components/ui";

export default function Onboarding() {
  const { downloads, refreshBoot, toast } = useApp();
  const [catalog, setCatalog] = useState<any>(null);
  const [started, setStarted] = useState<string>("");
  useEffect(() => { api.get("/models/catalog").then(setCatalog).catch((e) => toast(`Couldn't reach Hugging Face: ${e.message}`, "error")); }, []);
  const hw = catalog?.hardware;
  const pick = catalog?.models.find((m: any) => m.default && m.fit.cpu !== "too_big") || catalog?.models.find((m: any) => m.fit.cpu === "fits");
  const dl = Object.values(downloads).find((d: any) => d.state !== "cancelled") as any;
  const finish = async () => { await api.put("/settings", { onboarding_done: true }); await refreshBoot(); go("home"); };
  return (
    <div className="page onboarding">
      <h1 className="hero">Welcome to Datavera Agent</h1>
      <p className="subtitle center">Your own AI agents, running entirely on this computer. Three quick steps.</p>
      <div className="steps">
        <div className="card step-card">
          <div className="step-num"><Cpu size={18} /></div>
          <div className="grow">
            <h3>1. Your computer</h3>
            {!hw ? <Spinner /> : <p>{hw.cpu} · {(hw.ram_mb / 1024).toFixed(0)} GB memory{hw.gpus.length ? ` · ${hw.gpus.map((g: any) => g.name).join(", ")}` : ""}. Suggested processing: <b>{hw.recommended_backend === "cpu" ? "CPU" : hw.recommended_backend.toUpperCase()}</b>.</p>}
          </div>
        </div>
        <div className="card step-card">
          <div className="step-num"><Download size={18} /></div>
          <div className="grow">
            <h3>2. Download a model</h3>
            {!pick ? <Spinner /> : (
              <>
                <p><b>{pick.name}</b> {fitBadge(pick.fit)} — {pick.about} About {pick.approx_gb} GB.</p>
                {dl ? (
                  <div>{dl.state === "done" ? <span className="ok-line"><Check size={14} /> Ready</span> : <>
                    <Progress value={dl.done} total={dl.total} /><div className="small muted">{dl.state}… {dl.total ? Math.round(100 * dl.done / dl.total) : 0}%</div></>}
                    {dl.error && <div className="error-line">{dl.error}</div>}</div>
                ) : <div className="row gap-s"><Button kind="primary" busy={started === pick.key} onClick={async () => {
                  setStarted(pick.key);
                  try { await api.post("/models/download", { catalog_key: pick.key }); } catch (e: any) { toast(e.message, "error"); setStarted(""); }
                }}>Download {pick.name}</Button><Button kind="ghost" onClick={() => go("models", "discover")}>Choose another</Button></div>}
              </>
            )}
          </div>
        </div>
        <div className="card step-card">
          <div className="step-num"><Plug size={18} /></div>
          <div className="grow">
            <h3>3. Connect Datavera (optional)</h3>
            <p>To publish datasets or check your analytics, paste your Datavera API key in Plugins → Datavera. You can do this any time.</p>
            <Button onClick={() => go("plugins")}>Open Plugins</Button>
          </div>
        </div>
      </div>
      <div className="center mt"><Button kind="primary" icon={<Sparkles size={15} />} onClick={finish}>{dl?.state === "done" ? "Start" : "Continue (the download keeps going)"}</Button></div>
    </div>
  );
}
