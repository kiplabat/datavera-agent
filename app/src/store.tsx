import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { api, connectEvents, desktop, onEvent } from "./api";

export type Route = { page: string; id?: string; tab?: string };

function parseHash(): Route {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  return { page: parts[0] || "home", id: parts[1], tab: parts[2] };
}

export function go(page: string, id?: string, tab?: string) {
  location.hash = "/" + [page, id, tab].filter(Boolean).map((x) => encodeURIComponent(x as string)).join("/");
}

type Toast = { id: number; text: string; kind?: "ok" | "error" | "info" };

const Ctx = createContext<any>(null);
export const useApp = () => useContext(Ctx);

export function useEvent(handler: (e: any) => void, deps: any[] = []) {
  const ref = useRef(handler);
  ref.current = handler;
  useEffect(() => onEvent((e) => ref.current(e)), deps);
}

export function AppProvider({ children }: { children: React.ReactNode }) {
  const [route, setRoute] = useState<Route>(parseHash());
  const [boot, setBoot] = useState<any>(null);
  const [bootError, setBootError] = useState<string>("");
  const [projects, setProjects] = useState<any[]>([]);
  const [chats, setChats] = useState<any[]>([]);
  const [jobs, setJobs] = useState<any[]>([]);
  const [connected, setConnected] = useState(true);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [modelStatus, setModelStatus] = useState<any>({ state: "idle" });
  const [downloads, setDownloads] = useState<Record<string, any>>({});

  const toast = useCallback((text: string, kind: Toast["kind"] = "info") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, text, kind }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), kind === "error" ? 7000 : 4000);
  }, []);

  const refreshBoot = useCallback(async () => {
    try {
      const b = await api.get("/bootstrap");
      setBoot(b);
      setModelStatus(b.runtime.status);
      setBootError("");
    } catch (e: any) { setBootError(e.message); }
  }, []);
  const refreshProjects = useCallback(async () => setProjects((await api.get("/projects")).projects), []);
  const refreshChats = useCallback(async () => setChats((await api.get("/chats?limit=300")).chats), []);
  const refreshJobs = useCallback(async () => setJobs((await api.get("/jobs?status=active")).jobs), []);

  useEffect(() => {
    const onHash = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHash);
    refreshBoot(); refreshProjects(); refreshChats(); refreshJobs();
    connectEvents(setConnected);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEvent((e) => {
    if (e.kind === "chats_changed") refreshChats();
    if (e.kind === "projects_changed") refreshProjects();
    if (e.kind === "job") { refreshJobs(); if (["done", "failed", "cancelled"].includes(e.status)) refreshChats(); }
    if (e.kind === "model_status") setModelStatus(e);
    if (e.kind === "models_changed" || e.kind === "settings_changed" || e.kind === "harnesses_changed") refreshBoot();
    if (e.kind === "download") {
      setDownloads((d) => ({ ...d, [e.id]: e }));
      if (e.state === "done") { toast(`Downloaded ${e.name}`, "ok"); refreshBoot(); }
      if (e.state === "failed") toast(`Download failed: ${e.error}`, "error");
    }
    if (e.kind === "notify") {
      if (desktop && (document.hidden || !document.hasFocus())) desktop.notify(e.title, e.body || "");
      else toast(e.title + (e.body ? ` — ${e.body}` : ""), "info");
    }
    if (e.kind === "runtime" && e.action === "installed") { toast(`Installed the ${e.backend} runtime`, "ok"); refreshBoot(); }
    if (e.kind === "runtime" && e.action === "install_failed") toast(`Runtime install failed: ${e.error}`, "error");
  });

  const value = {
    route, boot, bootError, refreshBoot, projects, refreshProjects, chats, refreshChats, jobs, refreshJobs,
    connected, toast, modelStatus, downloads, setDownloads,
  };
  return (
    <Ctx.Provider value={value}>
      {children}
      <div className="toasts">
        {toasts.map((t) => <div key={t.id} className={`toast toast-${t.kind}`}>{t.text}</div>)}
      </div>
    </Ctx.Provider>
  );
}
