import React, { useEffect, useState } from "react";
import { Activity as ActivityIcon, CheckCircle2, Clock, Loader2, ShieldCheck, Square, XCircle } from "lucide-react";
import { api, fmtTime } from "../api";
import { go, useEvent } from "../store";
import { Badge, Button, Empty, Modal, PageHeader, Spinner } from "../components/ui";

const ICON: any = {
  running: <Loader2 size={15} className="spin accent" />, queued: <Clock size={15} className="muted" />,
  waiting: <ShieldCheck size={15} className="warn" />, done: <CheckCircle2 size={15} className="ok" />,
  failed: <XCircle size={15} className="bad" />, cancelled: <Square size={13} className="muted" />, interrupted: <XCircle size={15} className="muted" />,
};

export default function Activity() {
  const [jobs, setJobs] = useState<any[] | null>(null);
  const [progress, setProgress] = useState<Record<string, string>>({});
  const [log, setLog] = useState<any>(null);
  const load = () => api.get("/jobs").then((r) => setJobs(r.jobs));
  useEffect(() => { load(); }, []);
  useEvent((e) => {
    if (e.kind === "job") load();
    if (e.kind === "job_progress") setProgress((p) => ({ ...p, [e.id]: e.text }));
  });
  if (!jobs) return <div className="page"><Spinner /></div>;
  return (
    <div className="page">
      <PageHeader title="Activity" subtitle="Everything the agent is doing or has done: chat replies, pipelines, scheduled runs and plugin actions. Tasks keep going when the window is closed." />
      {jobs.length === 0 && <Empty icon={<ActivityIcon size={32} />} title="Nothing yet">Start a chat or a scheduled task.</Empty>}
      <div className="card">
        {jobs.map((j) => (
          <div key={j.id} className="job-row">
            {ICON[j.status] || ICON.queued}
            <div className="grow">
              <div className="row gap-s"><b className="ellipsis">{j.title}</b><Badge>{j.kind}</Badge>{j.status === "waiting" && <Badge kind="warn">needs your approval</Badge>}</div>
              <div className="small muted">{fmtTime(j.created)}{j.harness ? ` · ${j.harness}` : ""}{j.model ? ` · ${j.model}` : ""}
                {["running", "waiting"].includes(j.status) && (progress[j.id] || j.progress) ? ` · ${progress[j.id] || j.progress}` : ""}
                {j.error ? <span className="bad"> · {j.error}</span> : null}</div>
            </div>
            <div className="row gap-s">
              {j.chat_id && <Button small onClick={() => go("chat", j.chat_id)}>Open</Button>}
              {!j.chat_id && <Button small onClick={async () => setLog(await api.get(`/jobs/${j.id}`))}>Log</Button>}
              {["running", "waiting", "queued"].includes(j.status) && <Button small kind="danger" onClick={() => api.post(`/jobs/${j.id}/cancel`)}>Stop</Button>}
            </div>
          </div>
        ))}
      </div>
      {log && <Modal wide title={log.title} onClose={() => setLog(null)}><pre className="log">{log.log || JSON.stringify(log.result, null, 2)}</pre></Modal>}
    </div>
  );
}
