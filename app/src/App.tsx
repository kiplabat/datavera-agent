import React, { useState } from "react";
import {
  Activity as ActivityIcon, BookOpen, Brain, CalendarClock, Cpu, FolderKanban, MessageSquarePlus, Plug, Search,
  Settings as SettingsIcon, Workflow, Loader2, Pin, WifiOff,
} from "lucide-react";
import { go, useApp } from "./store";
import { Badge, Spinner } from "./components/ui";
import Home from "./pages/Home";
import ChatView from "./pages/ChatView";
import ProjectView from "./pages/ProjectView";
import Models from "./pages/Models";
import Plugins from "./pages/Plugins";
import Skills from "./pages/Skills";
import Harnesses from "./pages/Harnesses";
import Schedules from "./pages/Schedules";
import ActivityPage from "./pages/Activity";
import Memory from "./pages/Memory";
import Settings from "./pages/Settings";
import Onboarding from "./pages/Onboarding";

const NAV = [
  { page: "activity", label: "Activity", icon: ActivityIcon },
  { page: "schedules", label: "Schedules", icon: CalendarClock },
  { page: "models", label: "Models", icon: Cpu },
  { page: "plugins", label: "Plugins & connectors", icon: Plug },
  { page: "skills", label: "Skills", icon: BookOpen },
  { page: "harnesses", label: "Harnesses", icon: Workflow },
  { page: "memory", label: "Memory", icon: Brain },
  { page: "settings", label: "Settings", icon: SettingsIcon },
];

function Sidebar() {
  const { route, projects, chats, jobs, modelStatus, connected } = useApp();
  const [q, setQ] = useState("");
  const looseChats = chats.filter((c: any) => !c.project_id);
  const filtered = q ? chats.filter((c: any) => (c.title || "").toLowerCase().includes(q.toLowerCase())) : looseChats;
  return (
    <aside className="sidebar">
      <div className="brand" onClick={() => go("home")}>
        <img src="./icon.png" alt="" /> <span>Datavera Agent</span>
      </div>
      <button className="new-chat" onClick={() => go("home")}><MessageSquarePlus size={16} /> New chat</button>
      <div className="search"><Search size={14} /><input placeholder="Search chats" value={q} onChange={(e) => setQ(e.target.value)} /></div>
      <div className="side-scroll">
        {!q && (
          <div className="side-section">
            <div className="side-title">
              <span>Projects</span>
              <button className="link" onClick={() => go("project", "new")}>+ New</button>
            </div>
            {projects.length === 0 && <div className="side-empty">Group chats, folders, instructions and memory.</div>}
            {projects.map((p: any) => (
              <div key={p.id} className={`side-item${route.page === "project" && route.id === p.id ? " active" : ""}`}
                onClick={() => go("project", p.id)}>
                <FolderKanban size={14} /><span className="ellipsis">{p.name}</span><span className="muted small">{p.chat_count || ""}</span>
              </div>
            ))}
          </div>
        )}
        <div className="side-section">
          <div className="side-title"><span>{q ? "Results" : "Chats"}</span></div>
          {filtered.slice(0, 80).map((c: any) => (
            <div key={c.id} className={`side-item${route.page === "chat" && route.id === c.id ? " active" : ""}`}
              onClick={() => go("chat", c.id)} title={c.title}>
              {c.running ? <Loader2 size={13} className="spin accent" /> : c.pinned ? <Pin size={12} /> : <span className="dot" />}
              <span className="ellipsis">{c.title || "New chat"}</span>
              {c.origin === "schedule" && <CalendarClock size={12} className="muted" />}
            </div>
          ))}
          {!q && looseChats.length === 0 && <div className="side-empty">Your chats appear here.</div>}
        </div>
      </div>
      <nav className="side-nav">
        {NAV.map(({ page, label, icon: Icon }) => (
          <div key={page} className={`side-item${route.page === page ? " active" : ""}`} onClick={() => go(page)}>
            <Icon size={15} /><span>{label}</span>
            {page === "activity" && jobs.length > 0 && <Badge kind="accent">{jobs.length}</Badge>}
          </div>
        ))}
      </nav>
      <div className="model-chip" onClick={() => go("models")}
        title={modelStatus.error || "Model status (models unload after a while to free memory)"}>
        {!connected ? <><WifiOff size={13} /> Reconnecting to the engine…</> :
          modelStatus.state === "loading" ? <><Spinner size={13} /> Loading {modelStatus.model_id}…</> :
          modelStatus.state === "ready" ? <><span className="live-dot" /> {modelStatus.model_id} · {String(modelStatus.compute || "").toUpperCase()}</> :
          modelStatus.state === "failed" ? <><span className="err-dot" /> Model failed to load</> :
          <><span className="idle-dot" /> No model loaded</>}
      </div>
    </aside>
  );
}

export default function App() {
  const { route, boot, bootError } = useApp();
  if (bootError && !boot) {
    return <div className="center-screen"><h2>Can't reach the engine</h2><p>{bootError}</p><p className="muted">If this persists, restart Datavera Agent. Logs are in the app's data folder.</p></div>;
  }
  if (!boot) return <div className="center-screen"><Spinner size={28} /><p className="muted">Starting…</p></div>;
  const needsOnboarding = !boot.settings?.onboarding_done && boot.models.length === 0;
  let page: React.ReactNode;
  if (needsOnboarding && !["models", "plugins", "settings"].includes(route.page)) page = <Onboarding />;
  else switch (route.page) {
    case "chat": page = <ChatView key={route.id} chatId={route.id} />; break;
    case "project": page = <ProjectView key={route.id} projectId={route.id} />; break;
    case "models": page = <Models tab={route.id} />; break;
    case "plugins": page = <Plugins tab={route.id} />; break;
    case "skills": page = <Skills selected={route.id} />; break;
    case "harnesses": page = <Harnesses selected={route.id} />; break;
    case "schedules": page = <Schedules />; break;
    case "activity": page = <ActivityPage />; break;
    case "memory": page = <Memory />; break;
    case "settings": page = <Settings />; break;
    default: page = <Home projectId={route.page === "home" ? route.id : undefined} />;
  }
  return (
    <div className="shell">
      <Sidebar />
      <main className="main">{page}</main>
    </div>
  );
}
