import React, { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Loader2, X } from "lucide-react";
import { desktop } from "../api";

export function Button({ kind = "default", small, icon, children, busy, ...rest }: any) {
  return (
    <button className={`btn btn-${kind}${small ? " btn-sm" : ""}`} disabled={busy || rest.disabled} {...rest}>
      {busy ? <Loader2 className="spin" size={14} /> : icon}
      {children && <span>{children}</span>}
    </button>
  );
}

export function Toggle({ checked, onChange, label, disabled }: any) {
  return (
    <label className={`toggle${disabled ? " disabled" : ""}`}>
      <input type="checkbox" checked={!!checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span className="toggle-track"><span className="toggle-thumb" /></span>
      {label && <span className="toggle-label">{label}</span>}
    </label>
  );
}

export function Field({ label, help, children, inline }: any) {
  return (
    <div className={`field${inline ? " field-inline" : ""}`}>
      {label && <label className="field-label">{label}</label>}
      {children}
      {help && <div className="field-help">{help}</div>}
    </div>
  );
}

export function Modal({ title, onClose, children, wide, footer }: any) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal${wide ? " modal-wide" : ""}`}>
        <div className="modal-head"><h3>{title}</h3><button className="icon-btn" onClick={onClose}><X size={18} /></button></div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

export function Badge({ kind = "neutral", children, title }: any) {
  return <span className={`badge badge-${kind}`} title={title}>{children}</span>;
}

export function Spinner({ size = 16 }: any) {
  return <Loader2 className="spin" size={size} />;
}

export function Empty({ icon, title, children }: any) {
  return <div className="empty">{icon}<h3>{title}</h3><div>{children}</div></div>;
}

export function Tabs({ tabs, value, onChange }: any) {
  return (
    <div className="tabs">
      {tabs.map((t: any) => (
        <button key={t.id} className={`tab${value === t.id ? " active" : ""}`} onClick={() => onChange(t.id)}>
          {t.label}{t.count ? <span className="tab-count">{t.count}</span> : null}
        </button>
      ))}
    </div>
  );
}

export function PageHeader({ title, subtitle, actions }: any) {
  return (
    <div className="page-head">
      <div><h1>{title}</h1>{subtitle && <p className="subtitle">{subtitle}</p>}</div>
      <div className="page-actions">{actions}</div>
    </div>
  );
}

export function Progress({ value, total }: any) {
  const pct = total ? Math.min(100, (value / total) * 100) : 0;
  return <div className="progress"><div className="progress-bar" style={{ width: `${pct}%` }} /></div>;
}

export function Markdown({ children }: { children: string }) {
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }: any) => (
            <a href={href} onClick={(e) => { if (desktop && href?.startsWith("http")) { e.preventDefault(); desktop.openExternal(href); } }}
              target="_blank" rel="noreferrer">{children}</a>
          ),
        }}>
        {children || ""}
      </ReactMarkdown>
    </div>
  );
}

export function ConfirmButton({ onConfirm, children, ...rest }: any) {
  const [armed, setArmed] = useState(false);
  useEffect(() => { if (armed) { const t = setTimeout(() => setArmed(false), 3000); return () => clearTimeout(t); } }, [armed]);
  return (
    <Button {...rest} kind={armed ? "danger" : rest.kind || "ghost"} onClick={() => (armed ? (setArmed(false), onConfirm()) : setArmed(true))}>
      {armed ? "Click again to confirm" : children}
    </Button>
  );
}

export function FolderInput({ value, onChange, placeholder }: any) {
  return (
    <div className="row gap-s grow">
      <input className="input" value={value || ""} placeholder={placeholder || "C:\\Users\\you\\Documents\\folder"}
        onChange={(e) => onChange(e.target.value)} />
      {desktop && <Button onClick={async () => { const p = await desktop.pickFolder(); if (p) onChange(p); }}>Browse…</Button>}
    </div>
  );
}

export function fitBadge(fit: any, prefer: "cpu" | "gpu" = "cpu") {
  if (!fit) return null;
  const v = prefer === "gpu" && fit.gpu ? fit.gpu : fit.cpu;
  const map: any = {
    fits: ["ok", "Runs well"], tight: ["warn", "Tight fit"], too_big: ["bad", "Too big"], partial: ["warn", "Partly on GPU"],
  };
  const [kind, label] = map[v] || ["neutral", v];
  return <Badge kind={kind} title={`Needs about ${(fit.need_mb / 1024).toFixed(1)} GB of memory`}>{label}{prefer === "gpu" && fit.gpu ? " (GPU)" : ""}</Badge>;
}
