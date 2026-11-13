import React, { useMemo, useState } from "react";
import { api } from "../api";
import { go, useApp } from "../store";
import { Composer, ConnectorPicker, FolderPicker, HarnessPicker, ModelPicker, ThinkingToggle, uploadAll } from "../components/Pickers";

const SUGGESTIONS = [
  { text: "Analyse the CSV files in my folder and summarise the key trends with charts", harness: "data-analyst" },
  { text: "Research the latest public data on solar capacity in Africa, with sources", harness: "researcher" },
  { text: "Find and publish one new dataset about glacier mass balance", harness: "datavera:dataset-publisher" },
  { text: "Every weekday at 8:30, summarise new files in my reports folder", harness: "agent" },
  { text: "Create a skill for writing my monthly project report", harness: "agent" },
];

export default function Home({ projectId }: { projectId?: string }) {
  const { boot, projects, toast } = useApp();
  const project = projects.find((p: any) => p.id === projectId);
  const [harness, setHarness] = useState<string>(project?.default_harness || boot.settings.default_harness || "agent");
  const [model, setModel] = useState<string>(project?.default_model || boot.default_model || "");
  const [connectors, setConnectors] = useState<string[]>(project?.connectors || []);
  const [folders, setFolders] = useState<string[]>([]);
  const [thinking, setThinking] = useState(false);
  const [prefill, setPrefill] = useState("");
  const hour = new Date().getHours();
  const greeting = useMemo(() => hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening", [hour]);

  const send = async (text: string, files: File[]) => {
    try {
      const chat = await api.post("/chats", { project_id: projectId || null, harness, model: model || null, connectors, folders, thinking });
      const attachments = await uploadAll(chat.id, files);
      await api.post(`/chats/${chat.id}/messages`, { text, attachments });
      go("chat", chat.id);
    } catch (e: any) { toast(e.message, "error"); throw e; }
  };

  return (
    <div className="home">
      <div className="home-inner">
        <h1 className="hero">{greeting}{project ? <> · <span className="accent">{project.name}</span></> : ""}</h1>
        <p className="subtitle center">Everything runs on this computer. Pick a harness for how the agent should work, then ask.</p>
        {boot.models.length === 0 && (
          <div className="notice">No model yet. <a onClick={() => go("models", "discover")}>Download the recommended model</a> to start (about 3 GB).</div>
        )}
        <Composer autoFocus onSend={send} initialText={prefill}
          options={<>
            <HarnessPicker value={harness} onChange={setHarness} />
            <ModelPicker value={model} onChange={setModel} />
            <ConnectorPicker value={connectors} onChange={setConnectors} />
            <FolderPicker value={folders} onChange={setFolders} inherited={project?.folders || []} />
            <ThinkingToggle value={thinking} onChange={setThinking} />
          </>} />
        <div className="suggestions">
          {SUGGESTIONS.map((s) => (
            <button key={s.text} className="suggestion" onClick={() => { setHarness(s.harness); setPrefill(s.text); }}>{s.text}</button>
          ))}
        </div>
      </div>
    </div>
  );
}
