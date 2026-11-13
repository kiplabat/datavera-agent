import React, { useEffect, useRef, useState } from "react";
import {
  AlertTriangle, Brain, Check, CheckCircle2, ChevronDown, ChevronRight, Circle, Copy, FileText, FolderOpen, Loader2,
  MoreHorizontal, Paperclip, Pin, RotateCcw, ShieldCheck, Sparkles, Trash2, XCircle,
} from "lucide-react";
import { api, desktop, fileUrl, fmtBytes, fmtTime } from "../api";
import { go, useApp, useEvent } from "../store";
import { Badge, Button, Markdown, Spinner, Toggle } from "../components/ui";
import { Composer, ConnectorPicker, FolderPicker, HarnessPicker, ModelPicker, ThinkingToggle, uploadAll } from "../components/Pickers";

function ToolCard({ step, approval, onDecide }: any) {
  const [open, setOpen] = useState(false);
  const state = step.state === "running" ? "running" : step.declined ? "declined" : step.ok === false ? "failed" : "ok";
  const icon = state === "running" ? <Loader2 size={14} className="spin" /> : state === "ok" ? <CheckCircle2 size={14} className="ok" />
    : state === "declined" ? <ShieldCheck size={14} className="muted" /> : <XCircle size={14} className="bad" />;
  return (
    <div className={`tool-card tool-${state}`}>
      <div className="tool-head" onClick={() => setOpen(!open)}>
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}{icon}
        <span className="tool-label">{step.label || step.name}</span>
        <span className="tool-desc ellipsis">{step.description || summarize(step.arguments)}</span>
        {state === "declined" && <Badge>declined</Badge>}
      </div>
      {approval && (
        <div className="approval">
          <div className="approval-title"><ShieldCheck size={15} /> Allow this?</div>
          <pre className="approval-desc">{approval.description}</pre>
          <div className="row gap-s">
            <Button kind="primary" small onClick={() => onDecide(approval.approval_id, true, false)}>Allow</Button>
            <Button small onClick={() => onDecide(approval.approval_id, true, true)}>Always allow in this chat</Button>
            <Button kind="ghost" small onClick={() => onDecide(approval.approval_id, false, false)}>Decline</Button>
          </div>
        </div>
      )}
      {open && (
        <div className="tool-body">
          <div className="small muted">Arguments</div>
          <pre>{JSON.stringify(step.arguments, null, 2)}</pre>
          {step.result !== undefined && <><div className="small muted">Result</div><pre>{step.result}</pre></>}
        </div>
      )}
    </div>
  );
}

function summarize(args: any) {
  if (!args) return "";
  const v = Object.values(args)[0];
  return typeof v === "string" ? v.slice(0, 120) : "";
}

function PlanView({ plan, current }: any) {
  if (!plan?.length) return null;
  return (
    <div className="plan">
      <div className="small muted">Plan</div>
      {plan.map((s: any, i: number) => (
        <div key={i} className={`plan-step plan-${s.state}`}>
          {s.state === "done" ? <Check size={14} className="ok" /> : s.state === "doing" || i === current ? <Loader2 size={14} className="spin accent" /> : <Circle size={12} className="muted" />}
          <span>{s.title}</span>
        </div>
      ))}
    </div>
  );
}

function PipelineResult({ step }: any) {
  const r = step.result || {};
  return (
    <div className="pipeline-result">
      {r.cover && <img src={fileUrl(r.cover)} alt="cover" />}
      <div className="row gap-s wrap">
        {r.dataset?.edit_url && <Button small kind="primary" onClick={() => desktop ? desktop.openExternal(r.dataset.edit_url) : window.open(r.dataset.edit_url)}>Open on Datavera</Button>}
        {r.run_folder && desktop && <Button small onClick={() => desktop.openPath(r.run_folder)}>Open run folder</Button>}
        <Badge kind={step.code === 0 ? "ok" : "warn"}>exit {step.code}</Badge>
      </div>
    </div>
  );
}

function LogView({ lines }: any) {
  const [open, setOpen] = useState(true);
  const ref = useRef<HTMLPreElement>(null);
  useEffect(() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight; }, [lines?.length]);
  if (!lines?.length) return null;
  return (
    <div className="logview">
      <div className="tool-head" onClick={() => setOpen(!open)}>{open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}<span className="tool-label">Progress log</span><span className="muted small">{lines.length} lines</span></div>
      {open && <pre ref={ref}>{lines.join("\n")}</pre>}
    </div>
  );
}

function Steps({ steps, approvals, onDecide, log }: any) {
  return (
    <>
      {steps.map((s: any, i: number) => {
        if (s.type === "text") return <Markdown key={i}>{s.text}</Markdown>;
        if (s.type === "tool") return <ToolCard key={s.id || i} step={s} approval={approvals?.find((a: any) => a.call_id === s.id)} onDecide={onDecide} />;
        if (s.type === "plan_step") return <div key={i} className="plan-divider">Step {s.index}: {s.title}</div>;
        if (s.type === "pipeline") return <div key={i} className="note"><Sparkles size={14} /> Running <b>{s.harness}</b>{Object.keys(s.inputs || {}).length ? <> with {Object.entries(s.inputs).filter(([, v]) => v).map(([k, v]) => `${k}: ${v}`).join(", ")}</> : null}</div>;
        if (s.type === "pipeline_end") return <div key={i}>{!log && s.log_tail?.length ? <LogView lines={s.log_tail} /> : null}<PipelineResult step={s} /></div>;
        if (s.type === "error") return <div key={i} className="error-line"><AlertTriangle size={14} /> {s.text}</div>;
        if (s.type === "note") return <div key={i} className="note">{s.text}</div>;
        return null;
      })}
    </>
  );
}

function Reasoning({ text, live }: any) {
  const [open, setOpen] = useState(false);
  if (!text) return null;
  return (
    <div className="reasoning">
      <div className="tool-head" onClick={() => setOpen(!open)}>{open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}<Brain size={14} /><span className="muted">{live ? "Thinking…" : "Thoughts"}</span></div>
      {open && <div className="reasoning-body">{text}</div>}
    </div>
  );
}

function AssistantMessage({ msg, isLast, onRetry }: any) {
  const meta = msg.meta || {};
  const steps = meta.steps?.length ? meta.steps : [{ type: "text", text: msg.content }];
  const stats = meta.stats || {};
  return (
    <div className="msg msg-assistant">
      <Reasoning text={meta.reasoning} />
      <PlanView plan={meta.plan} />
      <Steps steps={steps} />
      {meta.stopped && <div className="note">Stopped.</div>}
      <div className="msg-foot">
        <span>{meta.harness_name || meta.harness}</span>
        {meta.model && <span>· {meta.model}</span>}
        {stats.seconds ? <span>· {stats.seconds}s</span> : null}
        {stats.tokens_per_second ? <span>· {stats.tokens_per_second} tok/s</span> : null}
        <button className="icon-btn" title="Copy" onClick={() => navigator.clipboard.writeText(msg.content || "")}><Copy size={13} /></button>
        {isLast && <button className="icon-btn" title="Retry" onClick={onRetry}><RotateCcw size={13} /></button>}
      </div>
    </div>
  );
}

function LiveTurn({ live, onDecide }: any) {
  return (
    <div className="msg msg-assistant live">
      <Reasoning text={live.reasoning} live />
      <PlanView plan={live.plan} current={live.current} />
      <Steps steps={live.steps} approvals={live.approvals} onDecide={onDecide} log={live.log} />
      <LogView lines={live.log} />
      {live.approvals.filter((a: any) => !a.call_id).map((a: any) => (
        <ToolCard key={a.approval_id} step={{ label: a.label, description: a.description, state: "running", arguments: {} }} approval={a} onDecide={onDecide} />
      ))}
      {live.error && <div className="error-line"><AlertTriangle size={14} /> {live.error}</div>}
      <div className="typing"><Spinner size={13} /> {live.status || "Working…"}</div>
    </div>
  );
}

function FilesPanel({ chatId, onClose }: any) {
  const [data, setData] = useState<any>(null);
  const load = () => api.get(`/chats/${chatId}/files`).then(setData);
  useEffect(() => { load(); }, [chatId]);
  useEvent((e) => { if (e.kind === "chat" && e.chat_id === chatId && e.type === "file_changed") load(); }, [chatId]);
  return (
    <div className="side-panel">
      <div className="panel-head"><b>Chat files</b><button className="link" onClick={onClose}>Close</button></div>
      {data && <div className="small muted pad-x">{data.root}</div>}
      {desktop && data && <div className="pad-x"><Button small icon={<FolderOpen size={13} />} onClick={() => desktop.openPath(data.root)}>Open folder</Button></div>}
      <div className="panel-list">
        {data?.files.length === 0 && <div className="muted pad">Uploads, downloads and files the agent makes without a folder land here.</div>}
        {data?.files.map((f: any) => (
          <div key={f.path} className="file-row" onClick={() => desktop ? desktop.openPath(f.path) : window.open(fileUrl(f.path))}>
            <FileText size={14} /><span className="ellipsis">{f.name}</span><span className="muted small">{fmtBytes(f.size)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function ChatView({ chatId }: { chatId: string }) {
  const { toast, projects, refreshChats } = useApp();
  const [chat, setChat] = useState<any>(null);
  const [live, setLive] = useState<any>(null);
  const [learned, setLearned] = useState<any[]>([]);
  const [showFiles, setShowFiles] = useState(false);
  const [menu, setMenu] = useState(false);
  const [editingTitle, setEditingTitle] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  const load = async () => {
    try {
      const c = await api.get(`/chats/${chatId}`);
      setChat(c);
      if (c.running_job && !live) setLive((l: any) => l || { message_id: null, steps: [], approvals: c.pending_approvals.map((a: any) => ({ ...a })), log: [], status: "Working…", reasoning: "" });
      if (!c.running_job) setLive(null);
    } catch (e: any) { toast(e.message, "error"); go("home"); }
  };
  useEffect(() => { load(); }, [chatId]);

  useEffect(() => {
    if (stick.current) bottom.current?.scrollIntoView({ block: "end" });
  });

  const update = (fn: (l: any) => any) => setLive((l: any) => fn(l || { steps: [], approvals: [], log: [], reasoning: "" }));

  useEvent((e) => {
    if (e.kind === "memory_learned" && e.chat_id === chatId) setLearned((x) => [...x, ...e.items]);
    if (e.kind === "job" && e.chat_id === chatId && ["failed", "cancelled"].includes(e.status)) { setTimeout(load, 300); }
    if (e.kind !== "chat" || e.chat_id !== chatId) return;
    switch (e.type) {
      case "user_message":
        setChat((c: any) => c && !c.messages.find((m: any) => m.id === e.message_id)
          ? { ...c, messages: [...c.messages, { id: e.message_id, role: "user", content: e.content, meta: e.meta, created: new Date().toISOString() }] } : c);
        update(() => ({ message_id: null, steps: [], approvals: [], log: [], reasoning: "", status: "Starting…" }));
        break;
      case "assistant_start": update((l) => ({ ...l, message_id: e.message_id, status: "Starting…" })); break;
      case "status": update((l) => ({ ...l, status: e.text })); break;
      case "reasoning": update((l) => ({ ...l, reasoning: (l.reasoning || "") + e.text })); break;
      case "token": update((l) => {
        const steps = [...l.steps];
        const last = steps[steps.length - 1];
        if (last && last.type === "text" && !last.closed) steps[steps.length - 1] = { ...last, text: last.text + e.text };
        else steps.push({ type: "text", text: e.text });
        return { ...l, steps, status: "Writing…" };
      }); break;
      case "text_replace": update((l) => {
        const steps = [...l.steps];
        for (let i = steps.length - 1; i >= 0; i--) if (steps[i].type === "text") { steps[i] = { ...steps[i], text: e.text }; break; }
        return { ...l, steps };
      }); break;
      case "tool_call": update((l) => ({ ...l, steps: [...l.steps.map((s: any) => s.type === "text" ? { ...s, closed: true } : s), { ...e.call, type: "tool", state: "running" }], status: e.call.label || "Working…" })); break;
      case "tool_result": update((l) => ({ ...l, steps: l.steps.map((s: any) => s.id === e.call_id ? { ...s, state: "done", ok: e.ok, result: e.result, declined: e.declined } : s) })); break;
      case "approval": update((l) => {
        const lastTool = [...l.steps].reverse().find((s: any) => s.type === "tool" && s.state === "running");
        return { ...l, approvals: [...l.approvals, { ...e, call_id: lastTool?.id }], status: "Waiting for your approval" };
      }); break;
      case "approval_done": update((l) => ({ ...l, approvals: l.approvals.filter((a: any) => a.approval_id !== e.approval_id) })); break;
      case "plan": update((l) => ({ ...l, plan: e.steps, current: e.current })); break;
      case "log": update((l) => ({ ...l, log: [...(l.log || []), e.text].slice(-400) })); break;
      case "pipeline_start": update((l) => ({ ...l, steps: [...l.steps, { type: "pipeline", harness: e.harness, inputs: e.inputs }] })); break;
      case "error": update((l) => ({ ...l, error: e.text })); break;
      case "assistant_end":
        setChat((c: any) => c ? { ...c, messages: [...c.messages, { id: e.message_id, role: "assistant", content: e.content, meta: e.meta, created: new Date().toISOString() }] } : c);
        setLive(null);
        refreshChats();
        break;
    }
  }, [chatId]);

  const patch = async (values: any) => {
    try { setChat({ ...(await api.put(`/chats/${chatId}`, values)) }); } catch (e: any) { toast(e.message, "error"); }
  };

  const send = async (text: string, files: File[]) => {
    try {
      const attachments = await uploadAll(chatId, files);
      stick.current = true;
      await api.post(`/chats/${chatId}/messages`, { text, attachments });
    } catch (e: any) { toast(e.message, "error"); throw e; }
  };

  const decide = async (approvalId: string, allow: boolean, always: boolean) => {
    try { await api.post(`/approvals/${approvalId}`, { allow, always }); } catch (e: any) { toast(e.message, "error"); }
  };

  if (!chat) return <div className="center-screen"><Spinner /></div>;
  const project = projects.find((p: any) => p.id === chat.project_id);
  const running = !!live || !!chat.running_job;
  const lastAssistant = [...chat.messages].reverse().find((m: any) => m.role === "assistant");

  return (
    <div className="chat-layout">
      <div className="chat-col">
        <div className="chat-head">
          <div className="chat-title">
            {project && <span className="crumb" onClick={() => go("project", project.id)}>{project.name} /</span>}
            {editingTitle ? <input className="input title-input" autoFocus defaultValue={chat.title}
              onBlur={(e) => { setEditingTitle(false); patch({ title: e.target.value }); }}
              onKeyDown={(e) => e.key === "Enter" && (e.target as any).blur()} />
              : <h2 onClick={() => setEditingTitle(true)} title="Rename">{chat.title || "New chat"}</h2>}
            {chat.origin === "schedule" && <Badge>scheduled</Badge>}
          </div>
          <div className="row gap-s">
            <span className="chip chip-toggle" title="Approve changes (file writes, Python, connector actions) automatically in this chat">
              <ShieldCheck size={14} /><Toggle checked={chat.auto_approve} onChange={(v: boolean) => patch({ auto_approve: v })} label="Auto-approve" />
            </span>
            <button className="icon-btn" title="Chat files" onClick={() => setShowFiles(!showFiles)}><Paperclip size={16} /></button>
            <div className="popover-wrap">
              <button className="icon-btn" onClick={() => setMenu(!menu)}><MoreHorizontal size={18} /></button>
              {menu && (
                <div className="popover right" onMouseLeave={() => setMenu(false)}>
                  <div className="menu">
                    <div className="menu-item" onClick={() => { patch({ pinned: !chat.pinned }); setMenu(false); }}><Pin size={13} /> {chat.pinned ? "Unpin" : "Pin"}</div>
                    <div className="menu-sub pad">Move to project</div>
                    <div className="menu-item" onClick={() => { patch({ project_id: null }); setMenu(false); }}>— No project —</div>
                    {projects.map((p: any) => <div key={p.id} className="menu-item" onClick={() => { patch({ project_id: p.id }); setMenu(false); }}>{p.name}</div>)}
                    <div className="menu-item danger" onClick={async () => { await api.del(`/chats/${chatId}`); refreshChats(); go("home"); }}><Trash2 size={13} /> Delete chat</div>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
        <div className="messages" ref={scroller}
          onScroll={(e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80; }}>
          <div className="messages-inner">
            {chat.messages.map((m: any) => m.role === "user" ? (
              <div key={m.id} className="msg msg-user">
                <div className="bubble">{m.content}
                  {(m.meta?.attachments || []).map((a: any) => <div key={a.path} className="attach"><Paperclip size={12} />{a.name}</div>)}
                </div>
                <div className="msg-time">{fmtTime(m.created)}</div>
              </div>
            ) : <AssistantMessage key={m.id} msg={m} isLast={m.id === lastAssistant?.id && !running}
                  onRetry={async () => { try { await api.post(`/chats/${chatId}/retry`); load(); } catch (e: any) { toast(e.message, "error"); } }} />)}
            {live && <LiveTurn live={live} onDecide={decide} />}
            {learned.length > 0 && (
              <div className="learned" onClick={() => go("memory")}>
                <Brain size={14} /> Learned: {learned.map((l) => l.text).join(" · ")}
              </div>
            )}
            <div ref={bottom} />
          </div>
        </div>
        <div className="composer-wrap">
          <Composer onSend={send} running={running} status={live?.status}
            onStop={() => api.post(`/chats/${chatId}/stop`)}
            options={<>
              <HarnessPicker value={chat.harness} onChange={(v: string) => patch({ harness: v })} />
              <ModelPicker value={chat.model} onChange={(v: string) => patch({ model: v })} />
              <ConnectorPicker value={chat.connectors || []} onChange={(v: string[]) => patch({ connectors: v })} />
              <FolderPicker value={chat.folders || []} onChange={(v: string[]) => patch({ folders: v })} inherited={project?.folders || []} />
              <ThinkingToggle value={!!chat.thinking} onChange={(v: boolean) => patch({ thinking: v })} />
            </>} />
        </div>
      </div>
      {showFiles && <FilesPanel chatId={chatId} onClose={() => setShowFiles(false)} />}
    </div>
  );
}
