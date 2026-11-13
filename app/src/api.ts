// Talking to the local engine. Same origin in the app (and via Vite's proxy in development).
declare global {
  interface Window { dva?: any }
}

const params = new URLSearchParams(location.search);
export const TOKEN: string = window.dva?.token || params.get("token") || sessionStorage.getItem("dva-token") || "";
if (TOKEN) sessionStorage.setItem("dva-token", TOKEN);
export const desktop = window.dva || null;

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) { super(message); this.status = status; }
}

async function request(method: string, path: string, body?: any) {
  const init: RequestInit = { method, headers: { Authorization: `Bearer ${TOKEN}` } };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) {
    (init.headers as any)["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const r = await fetch(`/api${path}`, init);
  const text = await r.text();
  let data: any = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!r.ok) throw new ApiError((data && data.error) || `Request failed (${r.status})`, r.status);
  return data;
}

export const api = {
  get: (p: string) => request("GET", p),
  post: (p: string, b?: any) => request("POST", p, b ?? {}),
  put: (p: string, b?: any) => request("PUT", p, b ?? {}),
  del: (p: string) => request("DELETE", p),
  upload: (p: string, file: File) => { const f = new FormData(); f.append("file", file); return request("POST", p, f); },
};

export const fileUrl = (path: string) => `/api/file?path=${encodeURIComponent(path)}&token=${encodeURIComponent(TOKEN)}`;
export const enc = encodeURIComponent;

type Handler = (e: any) => void;
const handlers = new Set<Handler>();
let source: EventSource | null = null;
let lastSeq = 0;

export function connectEvents(onStatus?: (ok: boolean) => void) {
  if (source) return;
  const open = () => {
    source = new EventSource(`/api/events?token=${encodeURIComponent(TOKEN)}&since=${lastSeq}`);
    source.onopen = () => onStatus?.(true);
    source.onmessage = (m) => {
      try {
        const e = JSON.parse(m.data);
        lastSeq = Math.max(lastSeq, e.seq || 0);
        handlers.forEach((h) => { try { h(e); } catch (err) { console.error(err); } });
      } catch {}
    };
    source.onerror = () => {
      onStatus?.(false);
      source?.close();
      source = null;
      setTimeout(open, 1500);
    };
  };
  lastSeq = 0;
  open();
}

export function onEvent(h: Handler) {
  handlers.add(h);
  return () => { handlers.delete(h); };
}

export function fmtBytes(n: number) {
  if (!n && n !== 0) return "";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 10 || i === 0 ? 0 : 1)} ${u[i]}`;
}

export function fmtTime(iso?: string) {
  if (!iso) return "";
  const d = new Date(iso);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  return sameDay ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString([], { day: "numeric", month: "short" }) + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
