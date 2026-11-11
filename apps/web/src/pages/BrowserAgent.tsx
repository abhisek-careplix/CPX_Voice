import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowLeft, CheckCircle2, Eye, Globe, Hand, Pause, Play, ShieldCheck, Square, XCircle } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../api";
import { useCan } from "../App";
import { Banner, Card, Empty, Pill, Toolbar, timeAgo, useToast } from "../ui";

type Risk = "read" | "enter_data" | "commit" | "person_only";

interface BrowserSettings {
  configured: boolean;
  default_model: "gemini" | "claude";
  models: { id: "gemini" | "claude"; label: string; model_id: string }[];
  presence_timeout_s: number;
  can_supervise: boolean;
  is_admin: boolean;
}

interface RunRow {
  id: string;
  task: string;
  start_url: string;
  allowed_hosts: string[];
  model: "gemini" | "claude";
  model_id: string;
  state: string;
  control: "agent" | "human";
  pause_reason: string | null;
  plan: string[];
  plan_approved: boolean;
  allow_for_run: Risk[];
  result: string | null;
  error: string | null;
  step_count: number;
  supervisor_idle_s: number | null;
  presence_timeout_s: number;
  created_at: string;
  ended_at: string | null;
}

interface Step {
  id: string;
  seq: number;
  kind: string;
  risk: Risk;
  status: string;
  description: string;
  why: string | null;
  target: { tag?: string; text?: string; name?: string } | null;
  x: number | null;
  y: number | null;
  text: string | null;
  url: string | null;
  page_url: string | null;
  page_title: string | null;
  needs_approval_reason: string | null;
  has_screenshot: boolean;
  feedback: string | null;
  outcome: string | null;
  created_at: string;
}

type RunDetail = RunRow & { steps: Step[]; pending: Step | null; is_supervisor: boolean };

const ENDED = ["done", "failed", "stopped"];
const W = 1280;
const H = 800;

export const RISK: Record<Risk, { label: string; tone: "blue" | "orange" | "red" | "purple"; help: string }> = {
  read: { label: "Read", tone: "blue", help: "Looks or moves around; changes nothing." },
  enter_data: { label: "Enter data", tone: "orange", help: "Types or picks values; nothing is saved yet." },
  commit: { label: "Commit", tone: "red", help: "Submits or changes data. Always asks, every time." },
  person_only: { label: "Person only", tone: "purple", help: "Paying, signing, attesting and passwords are done by a person." },
};

const STATE: Record<string, { label: string; tone: "blue" | "orange" | "red" | "green" | "gray" }> = {
  starting: { label: "Opening the browser", tone: "blue" },
  planning: { label: "Writing a plan", tone: "blue" },
  awaiting_plan: { label: "Plan needs approval", tone: "orange" },
  running: { label: "Working", tone: "blue" },
  awaiting_approval: { label: "Needs your approval", tone: "orange" },
  paused: { label: "Paused", tone: "gray" },
  done: { label: "Done", tone: "green" },
  failed: { label: "Failed", tone: "red" },
  stopped: { label: "Stopped", tone: "gray" },
};

const PAUSE: Record<string, string> = {
  supervisor_away: "Paused because no one is watching. Keep this page open and visible to continue.",
  human_control: "You have control. Click and type on the screen; hand back when done.",
  by_supervisor: "Paused by you.",
  left_sites: "The page moved to a site outside this run. Check the screen, then resume.",
};

export default function BrowserAgent() {
  const { id } = useParams();
  return id ? <RunView id={id} /> : <Runs />;
}

function Runs() {
  const settings = useQuery({ queryKey: ["browser-settings"], queryFn: () => api.get<BrowserSettings>("/browser/settings") });
  const runs = useQuery({ queryKey: ["browser-runs"], queryFn: () => api.get<RunRow[]>("/browser/runs"), refetchInterval: 5000 });
  const s = settings.data;
  return (
    <div className="content">
      <Toolbar eyebrow="Labs" title="Browser agent" description="A supervised agent that works in payer and portal websites. You approve its plan and any risky step, and can take over at any time." />
      {s && !s.configured && (
        <Banner tone="orange" title="The browser agent isn't enabled for this workspace">
          It's a preview add-on. Ask your CarePlix team to turn it on.
        </Banner>
      )}
      {s?.configured && s.can_supervise && <StartForm settings={s} />}
      <Card title="Runs" subtitle="Every action, approval and take-over is recorded with a screenshot.">
        {runs.data && runs.data.length === 0 ? (
          <Empty icon={Globe} title="No browser runs yet">Start one above. You'll approve the plan first.</Empty>
        ) : (
          <ul className="list">
            {(runs.data ?? []).map((r) => (
              <li key={r.id}>
                <Link className="list-row" to={`/browser/${r.id}`}>
                  <div className="row-main">
                    <div className="row-title">{r.task}</div>
                    <div className="row-sub">
                      {new URL(r.start_url).hostname} · {r.model === "claude" ? "Claude" : "Gemini"} · {r.step_count} steps · {timeAgo(r.created_at)}
                    </div>
                  </div>
                  <Pill tone={(STATE[r.state] ?? STATE.paused).tone}>{(STATE[r.state] ?? STATE.paused).label}</Pill>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

function StartForm({ settings }: { settings: BrowserSettings }) {
  const navigate = useNavigate();
  const toast = useToast();
  const [task, setTask] = useState("");
  const [url, setUrl] = useState("https://");
  const [model, setModel] = useState<"gemini" | "claude">(settings.default_model);
  const [supervised, setSupervised] = useState(false);
  const [allowReading, setAllowReading] = useState(false);
  const [ackHost, setAckHost] = useState<string | null>(null);
  const [terms, setTerms] = useState("");
  const start = useMutation({
    mutationFn: () => api.post<RunRow>("/browser/runs", { task, start_url: url, model, supervised, allow_reading: allowReading }),
    onSuccess: (r) => navigate(`/browser/${r.id}`),
    onError: (e) => {
      if (e instanceof ApiError && e.code === "site_terms_ack_needed") setAckHost(String(e.detail?.host ?? ""));
    },
  });
  const ack = useMutation({
    mutationFn: () => api.post("/browser/sites/acknowledge", { host: ackHost, terms_note: terms || null }),
    onSuccess: () => {
      toast("Site acknowledged. Starting the run.");
      setAckHost(null);
      start.mutate();
    },
  });
  return (
    <Card title="Start a supervised run" subtitle="The agent proposes; you approve; the agent does it.">
      <div className="card-pad stack" style={{ gap: 12 }}>
        <label className="field">
          <span className="field-label">Task, in plain words</span>
          <textarea className="textarea" rows={2} value={task} onChange={(e) => setTask(e.target.value)} placeholder="Check the status of claim 12345 for member W…6789 (date of service 10/14)" />
        </label>
        <div className="row" style={{ gap: 12, flexWrap: "wrap" }}>
          <label className="field" style={{ flex: 2, minWidth: 240 }}>
            <span className="field-label">Site</span>
            <input className="input" value={url} onChange={(e) => setUrl(e.target.value)} />
          </label>
          <label className="field" style={{ flex: 1, minWidth: 180 }}>
            <span className="field-label">Model</span>
            <select className="select" value={model} onChange={(e) => setModel(e.target.value as "gemini" | "claude")}>
              {settings.models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label}
                </option>
              ))}
            </select>
          </label>
        </div>
        <label className="checkbox-row">
          <input type="checkbox" checked={allowReading} onChange={(e) => setAllowReading(e.target.checked)} />
          <span>Let it read and move around without asking (typing and submitting still ask)</span>
        </label>
        <label className="checkbox-row" style={{ fontWeight: 600 }}>
          <input type="checkbox" checked={supervised} onChange={(e) => setSupervised(e.target.checked)} />
          <span>
            <ShieldCheck size={14} aria-hidden /> Under human supervision: I'll watch this run and approve its steps
          </span>
        </label>
        {start.error && !(start.error instanceof ApiError && start.error.code === "site_terms_ack_needed") && (
          <Banner tone="red" title="Couldn't start">{(start.error as Error).message}</Banner>
        )}
        {ackHost && (
          <Banner tone="orange" title={`First run on ${ackHost}`}>
            <div className="stack" style={{ gap: 8 }}>
              <span>
                Some portals' terms forbid automated access even with a person watching; the worst case is the practice's account being suspended. An admin acknowledges this once per site, and it's recorded.
              </span>
              {settings.is_admin ? (
                <>
                  <textarea className="textarea" rows={2} placeholder="Note on the site's terms (optional)" value={terms} onChange={(e) => setTerms(e.target.value)} />
                  <div>
                    <button className="btn btn-primary" disabled={ack.isPending} onClick={() => ack.mutate()}>
                      I've read the terms: acknowledge and start
                    </button>
                  </div>
                </>
              ) : (
                <span>Ask an admin to start the first run on this site.</span>
              )}
            </div>
          </Banner>
        )}
        <div>
          <button className="btn btn-primary" disabled={!supervised || task.trim().length < 3 || url.length < 10 || start.isPending} onClick={() => start.mutate()}>
            <Play size={15} aria-hidden /> {start.isPending ? "Starting…" : "Start run"}
          </button>
        </div>
      </div>
    </Card>
  );
}

function useVisible() {
  const [visible, setVisible] = useState(document.visibilityState === "visible");
  useEffect(() => {
    const on = () => setVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", on);
    return () => document.removeEventListener("visibilitychange", on);
  }, []);
  return visible;
}

function RunView({ id }: { id: string }) {
  const qc = useQueryClient();
  const canSupervise = useCan("browser_supervise");
  const detail = useQuery({ queryKey: ["browser-run", id], queryFn: () => api.get<RunDetail>(`/browser/runs/${id}`), refetchInterval: 1000 });
  const run = detail.data;
  const visible = useVisible();
  const [tick, setTick] = useState(0);
  const live = !!run && !ENDED.includes(run.state);
  // The live view: refreshing it while the page is visible is what counts as watching.
  useEffect(() => {
    if (!live || !visible) return;
    const t = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [live, visible]);
  const control = useMutation({
    mutationFn: (action: string) => api.post(`/browser/runs/${id}/control`, { action }),
    onSettled: () => qc.invalidateQueries({ queryKey: ["browser-run", id] }),
  });
  if (!run) return null;
  const st = STATE[run.state] ?? STATE.paused;
  const mine = run.is_supervisor && canSupervise;
  return (
    <div className="content">
      <Toolbar
        eyebrow={
          <Link to="/browser" className="row" style={{ gap: 4 }}>
            <ArrowLeft size={14} aria-hidden /> Browser runs
          </Link>
        }
        title={run.task}
        actions={
          mine &&
          live && (
            <div className="row" style={{ gap: 8 }}>
              {run.control === "human" ? (
                <button className="btn btn-primary" onClick={() => control.mutate("hand_back")}>
                  <Play size={15} aria-hidden /> Hand back to the agent
                </button>
              ) : (
                <button className="btn btn-secondary" onClick={() => control.mutate("take_over")}>
                  <Hand size={15} aria-hidden /> Take over
                </button>
              )}
              {run.state === "paused" && run.pause_reason === "by_supervisor" ? (
                <button className="btn btn-secondary" onClick={() => control.mutate("resume")}>
                  <Play size={15} aria-hidden /> Resume
                </button>
              ) : run.state === "paused" && run.pause_reason === "left_sites" ? (
                <button className="btn btn-secondary" onClick={() => control.mutate("resume")}>
                  <Play size={15} aria-hidden /> Resume
                </button>
              ) : (
                run.control === "agent" && (
                  <button className="btn btn-secondary" onClick={() => control.mutate("pause")}>
                    <Pause size={15} aria-hidden /> Pause
                  </button>
                )
              )}
              <button className="btn btn-danger" onClick={() => control.mutate("stop")}>
                <Square size={15} aria-hidden /> Stop
              </button>
            </div>
          )
        }
      />
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        <Pill tone={st.tone} live={live}>
          {st.label}
        </Pill>
        <Pill tone="gray" icon={ShieldCheck}>
          Under human supervision
        </Pill>
        <span className="small muted">
          {run.model === "claude" ? "Claude" : "Gemini"} ({run.model_id}) · sites: {run.allowed_hosts.join(", ")} · {run.step_count} steps
        </span>
      </div>
      {run.state === "paused" && run.pause_reason && <Banner tone="orange" title="Paused">{PAUSE[run.pause_reason] ?? run.pause_reason}</Banner>}
      {run.state === "done" && run.result && <Banner tone="green" title="Result">{run.result}</Banner>}
      {run.state === "failed" && <Banner tone="red" title="The run failed">{run.error}</Banner>}
      {!mine && live && <Banner tone="blue" title="Watching only">Only the supervisor who started this run can approve its steps.</Banner>}

      <div className="row" style={{ gap: 16, alignItems: "flex-start", flexWrap: "wrap" }}>
        <div style={{ flex: "3 1 560px", minWidth: 0 }}>
          <Card title="Live view" subtitle={live ? (visible ? "Watching" : "Not visible: the run will pause") : "The browser has closed"}>
            {live && mine ? <LiveScreen id={id} tick={tick} human={run.control === "human"} /> : <div className="card-pad small muted">No live screen.</div>}
          </Card>
        </div>
        <div className="stack" style={{ flex: "2 1 360px", gap: 16, minWidth: 0 }}>
          {run.state === "awaiting_plan" && <PlanCard run={run} mine={mine} />}
          {run.pending && <ApprovalCard run={run} step={run.pending} mine={mine} />}
          {run.plan.length > 0 && run.state !== "awaiting_plan" && (
            <Card title="Plan" subtitle={run.plan_approved ? "Approved" : "Not approved"}>
              <ol className="small" style={{ margin: 0, padding: "12px 12px 12px 30px" }}>
                {run.plan.map((s, i) => (
                  <li key={i}>{s}</li>
                ))}
              </ol>
            </Card>
          )}
          <History run={run} />
        </div>
      </div>
    </div>
  );
}

function LiveScreen({ id, tick, human }: { id: string; tick: number; human: boolean }) {
  const img = useRef<HTMLImageElement>(null);
  const [text, setText] = useState("");
  const [url, setUrl] = useState("");
  const input = useMutation({ mutationFn: (body: Record<string, unknown>) => api.post<{ ok: boolean; error?: string }>(`/browser/runs/${id}/input`, body) });
  const click = (e: React.MouseEvent<HTMLImageElement>) => {
    if (!human || !img.current) return;
    const r = img.current.getBoundingClientRect();
    input.mutate({ kind: "click", x: Math.round(((e.clientX - r.left) / r.width) * W), y: Math.round(((e.clientY - r.top) / r.height) * H) });
  };
  return (
    <div className="stack" style={{ gap: 8, padding: 8 }}>
      <img
        ref={img}
        src={`/api/browser/runs/${id}/screen?t=${tick}`}
        alt="Live browser screen"
        onClick={click}
        style={{ width: "100%", aspectRatio: `${W} / ${H}`, borderRadius: 8, border: human ? "3px solid var(--orange)" : "1px solid var(--separator-strong)", cursor: human ? "crosshair" : "default", background: "var(--surface-sunken)" }}
      />
      {human && (
        <div className="stack" style={{ gap: 6 }}>
          <div className="row" style={{ gap: 6 }}>
            <input className="input" placeholder="Type into the focused field (never stored)" value={text} onChange={(e) => setText(e.target.value)} type="password" autoComplete="off" />
            <button className="btn btn-secondary" disabled={!text} onClick={() => { input.mutate({ kind: "type", text }); setText(""); }}>
              Type
            </button>
          </div>
          <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
            {["Enter", "Tab", "Escape"].map((k) => (
              <button key={k} className="btn btn-plain" onClick={() => input.mutate({ kind: "key", text: k })}>
                {k}
              </button>
            ))}
            <button className="btn btn-plain" onClick={() => input.mutate({ kind: "scroll", direction: "up" })}>Scroll up</button>
            <button className="btn btn-plain" onClick={() => input.mutate({ kind: "scroll", direction: "down" })}>Scroll down</button>
            <button className="btn btn-plain" onClick={() => input.mutate({ kind: "back" })}>Back</button>
          </div>
          <div className="row" style={{ gap: 6 }}>
            <input className="input" placeholder="https://…" value={url} onChange={(e) => setUrl(e.target.value)} />
            <button className="btn btn-plain" disabled={!url.startsWith("http")} onClick={() => input.mutate({ kind: "navigate", url })}>
              Open
            </button>
          </div>
          <span className="small muted">You're driving. Sign in, enter codes, or fix a step, then hand back.</span>
        </div>
      )}
    </div>
  );
}

function PlanCard({ run, mine }: { run: RunDetail; mine: boolean }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(run.plan.join("\n"));
  const decide = useMutation({
    mutationFn: (decision: "approve" | "edit" | "cancel") => api.post(`/browser/runs/${run.id}/plan`, { decision, steps: draft.split("\n") }),
    onSettled: () => qc.invalidateQueries({ queryKey: ["browser-run", run.id] }),
  });
  return (
    <Card title="Approve the plan" subtitle="Nothing happens in the browser until you approve.">
      <div className="card-pad stack" style={{ gap: 10 }}>
        {editing ? (
          <textarea className="textarea" rows={Math.max(4, run.plan.length + 1)} value={draft} onChange={(e) => setDraft(e.target.value)} />
        ) : (
          <ol style={{ margin: 0, paddingLeft: 20 }}>
            {run.plan.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ol>
        )}
        {mine && (
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            <button className="btn btn-primary" onClick={() => decide.mutate(editing ? "edit" : "approve")}>
              <CheckCircle2 size={15} aria-hidden /> {editing ? "Save and approve" : "Approve plan"}
            </button>
            {!editing && (
              <button className="btn btn-secondary" onClick={() => setEditing(true)}>
                Edit
              </button>
            )}
            <button className="btn btn-plain" onClick={() => decide.mutate("cancel")}>
              Cancel run
            </button>
          </div>
        )}
      </div>
    </Card>
  );
}

function ApprovalCard({ run, step, mine }: { run: RunDetail; step: Step; mine: boolean }) {
  const qc = useQueryClient();
  const admin = useCan("admin");
  const [text, setText] = useState(step.text ?? "");
  const [editing, setEditing] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [denying, setDenying] = useState(false);
  const decide = useMutation({
    mutationFn: (decision: string) =>
      api.post(`/browser/runs/${run.id}/steps/${step.id}/decision`, {
        decision,
        text: decision === "edit" ? text : undefined,
        feedback: decision === "deny" ? feedback || undefined : undefined,
      }),
    onSettled: () => qc.invalidateQueries({ queryKey: ["browser-run", run.id] }),
  });
  const risk = RISK[step.risk];
  const personOnly = step.risk === "person_only";
  const bulkable = step.risk === "read" || step.risk === "enter_data";
  useEffect(() => {
    if (!mine) return;
    const onKey = (e: KeyboardEvent) => {
      const typing = (e.target as HTMLElement)?.tagName === "TEXTAREA" || (e.target as HTMLElement)?.tagName === "INPUT";
      if (e.key === "Enter" && !typing && !personOnly && !decide.isPending) decide.mutate("approve");
      if (e.key === "Escape" && !decide.isPending) setDenying(true);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [mine, personOnly, decide]);
  return (
    <Card title="Approve this step" subtitle={step.page_title || step.page_url || undefined}>
      <div className="card-pad stack" style={{ gap: 10 }}>
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          <Pill tone={risk.tone}>{risk.label}</Pill>
          <strong>{step.description}</strong>
        </div>
        <span className="small muted">{risk.help}</span>
        {step.needs_approval_reason && <span className="small row" style={{ gap: 4, color: "var(--orange)" }}><AlertTriangle size={13} aria-hidden /> {step.needs_approval_reason}</span>}
        {step.has_screenshot && (
          <div style={{ position: "relative" }}>
            <img src={`/api/browser/runs/${run.id}/steps/${step.id}/screenshot`} alt="Where the agent will act" style={{ width: "100%", borderRadius: 6, border: "1px solid var(--separator-strong)" }} />
            {step.x != null && step.y != null && (
              <span
                aria-hidden
                style={{
                  position: "absolute",
                  left: `${(step.x / W) * 100}%`,
                  top: `${(step.y / H) * 100}%`,
                  width: 28,
                  height: 28,
                  marginLeft: -14,
                  marginTop: -14,
                  borderRadius: "50%",
                  border: "3px solid var(--danger)",
                  boxShadow: "0 0 0 3px rgba(239,68,68,.25)",
                }}
              />
            )}
          </div>
        )}
        {step.kind === "type" && (
          <label className="field">
            <span className="field-label">Value it will type</span>
            {editing ? <input className="input" value={text} onChange={(e) => setText(e.target.value)} /> : <span className="mono">{step.text}</span>}
          </label>
        )}
        {step.url && <span className="small mono">{step.url}</span>}
        {step.why && <span className="small"><Eye size={12} aria-hidden /> Why: {step.why}</span>}
        {mine &&
          (denying ? (
            <div className="stack" style={{ gap: 6 }}>
              <textarea className="textarea" rows={2} autoFocus placeholder="Tell the agent what to do instead (optional)" value={feedback} onChange={(e) => setFeedback(e.target.value)} />
              <div className="row" style={{ gap: 8 }}>
                <button className="btn btn-danger" onClick={() => decide.mutate("deny")}>
                  <XCircle size={15} aria-hidden /> Deny
                </button>
                <button className="btn btn-plain" onClick={() => setDenying(false)}>
                  Back
                </button>
              </div>
            </div>
          ) : (
            <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
              {!personOnly && (
                <button className="btn btn-primary" disabled={decide.isPending} onClick={() => decide.mutate(editing ? "edit" : "approve")}>
                  <CheckCircle2 size={15} aria-hidden /> {editing ? "Save and approve" : "Approve"} <span className="small muted">Enter</span>
                </button>
              )}
              {bulkable && (
                <button className="btn btn-secondary" disabled={decide.isPending} onClick={() => decide.mutate("approve_similar")}>
                  Approve all {risk.label.toLowerCase()} steps in this run
                </button>
              )}
              {bulkable && admin && (
                <button className="btn btn-plain" disabled={decide.isPending} onClick={() => decide.mutate("always_allow")}>
                  Always allow on this site
                </button>
              )}
              {step.kind === "type" && !editing && !personOnly && (
                <button className="btn btn-plain" onClick={() => setEditing(true)}>
                  Edit value
                </button>
              )}
              <button className="btn btn-plain" onClick={() => setDenying(true)}>
                Deny <span className="small muted">Esc</span>
              </button>
              <button className="btn btn-plain" onClick={() => decide.mutate("take_over")}>
                <Hand size={14} aria-hidden /> Take over
              </button>
            </div>
          ))}
        {decide.error && <span className="small" style={{ color: "var(--red)" }}>{(decide.error as Error).message}</span>}
      </div>
    </Card>
  );
}

const STEP_STATUS: Record<string, string> = {
  executed: "done",
  failed: "failed",
  denied: "denied",
  by_person: "by you",
  proposed: "waiting",
  approved: "approved",
  auto_approved: "allowed",
  skipped: "skipped",
};

function History({ run }: { run: RunDetail }) {
  const steps = [...run.steps].reverse();
  return (
    <Card title="Steps" subtitle="Newest first. Screenshots open on demand and are audited.">
      {steps.length === 0 ? (
        <div className="card-pad small muted">No steps yet.</div>
      ) : (
        <ul className="list">
          {steps.map((s) => (
            <li key={s.id} className="list-row" style={{ alignItems: "flex-start" }}>
              <div className="row-main">
                <div className="row-title small">
                  {s.seq}. {s.description}
                </div>
                <div className="row-sub">
                  {RISK[s.risk]?.label ?? s.risk} · {STEP_STATUS[s.status] ?? s.status}
                  {s.feedback ? ` · “${s.feedback}”` : ""}
                  {s.outcome && s.outcome !== "done" ? ` · ${s.outcome}` : ""}
                  {s.has_screenshot && (
                    <>
                      {" · "}
                      <a href={`/api/browser/runs/${run.id}/steps/${s.id}/screenshot`} target="_blank" rel="noreferrer">
                        screenshot
                      </a>
                    </>
                  )}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
