import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AudioLines,
  Check,
  CircleDashed,
  Cpu,
  ExternalLink,
  OctagonX,
  PhoneIncoming,
  PhoneOutgoing,
  Phone,
  Plus,
  Radio,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  X,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type AgentSummary, type Attempt, type Check as ReadinessCheck, type ConnectionsOverview, type ProbeStatus, type SandboxLine, type Readiness, type Route } from "../api";
import { useCan, useMe } from "../App";
import { CallBlocked } from "../CallBlocked";
import { AttemptPill, Banner, Card, CheckPill, Empty, Pill, Sheet, Switch, Toolbar, timeAgo, useToast } from "../ui";

const REGIONS = ["us-central1", "us-east1", "us-east4", "us-west1", "europe-west1", "europe-west4", "asia-northeast1"];

export default function Connections() {
  const me = useMe();
  const isAdmin = useCan("admin");
  const qc = useQueryClient();
  const toast = useToast();
  const overview = useQuery({ queryKey: ["connections"], queryFn: () => api.get<ConnectionsOverview>("/connections") });
  const readiness = useQuery({ queryKey: ["readiness"], queryFn: () => api.get<Readiness>("/readiness") });
  const routes = useQuery({ queryKey: ["routes"], queryFn: () => api.get<Route[]>("/routes") });
  const agents = useQuery({ queryKey: ["agents"], queryFn: () => api.get<AgentSummary[]>("/agents") });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["connections"] });
    qc.invalidateQueries({ queryKey: ["readiness"] });
    qc.invalidateQueries({ queryKey: ["routes"] });
  };
  const stop = useMutation({
    mutationFn: (enabled: boolean) => api.post<{ calls_ended: number }>("/workspace/emergency-stop", { enabled }),
    onSuccess: (r, enabled) => {
      qc.invalidateQueries();
      toast(enabled ? `Calling stopped · ${r.calls_ended} call(s) ended` : "Calling resumed");
    },
  });
  const checks = Object.fromEntries((readiness.data?.checks ?? []).map((c) => [c.name, c])) as Record<string, ReadinessCheck>;
  const o = overview.data;

  return (
    <>
      <Toolbar
        eyebrow="Settings"
        title="Connections"
        description="Everything a call depends on, checked one by one: the voice platform, the agent runtime, the voice model and your phone lines. Live calls need every step ready."
        actions={
          <button className="btn btn-secondary" onClick={refresh} disabled={readiness.isFetching}>
            <RefreshCw size={15} className={readiness.isFetching ? "spin" : ""} aria-hidden /> Re-check
          </button>
        }
      />
      <div className="content">
        <SetupSteps checks={checks} />
        {!isAdmin && <Banner tone="blue" title="Only administrators can change connections." />}

        {o?.sandbox.available && <SandboxCard line={o.sandbox} agents={agents.data ?? []} isAdmin={isAdmin} onChange={refresh} />}

        <LiveKitCard o={o} check={checks.livekit_api} worker={checks.agent_worker} isAdmin={isAdmin} onChange={refresh} />
        <GeminiCard o={o} check={checks.voice_model} isAdmin={isAdmin} onChange={refresh} />
        <InboundCard o={o} check={checks.inbound_route} routes={routes.data ?? []} agents={agents.data ?? []} isAdmin={isAdmin} onChange={refresh} />
        <OutboundCard o={o} check={checks.outbound_route} agents={agents.data ?? []} isAdmin={isAdmin} onChange={refresh} />

        {isAdmin && (
          <Card title="Emergency stop" subtitle="Blocks new calls in this workspace and ends active ones. Every use is audited." pad>
            <div className="row">
              {me.emergency_stop ? (
                <>
                  <Banner tone="red" title="Calling is stopped for this workspace." icon={OctagonX} />
                  <span className="spacer" />
                  <button className="btn btn-secondary" onClick={() => stop.mutate(false)}>
                    Resume calling
                  </button>
                </>
              ) : (
                <button className="btn btn-danger" onClick={() => confirm("Stop all calling and end active calls in this workspace?") && stop.mutate(true)}>
                  <OctagonX size={16} aria-hidden /> Stop all calling
                </button>
              )}
            </div>
          </Card>
        )}
      </div>
    </>
  );
}

// ---------- Progress ----------

const STEPS: [string, string, LucideIcon][] = [
  ["livekit_api", "LiveKit", Radio],
  ["agent_worker", "Agent worker", Cpu],
  ["voice_model", "Gemini Live", Sparkles],
  ["inbound_route", "Inbound", PhoneIncoming],
  ["outbound_route", "Outbound", PhoneOutgoing],
];

function SetupSteps({ checks }: { checks: Record<string, ReadinessCheck> }) {
  const done = STEPS.filter(([k]) => checks[k]?.status === "ready").length;
  return (
    <section className="card card-pad" aria-label="Setup progress">
      <div className="row" style={{ marginBottom: 14 }}>
        <h2 className="card-title">Setup</h2>
        <span className="small muted">
          {done} of {STEPS.length} verified
        </span>
        <span className="spacer" />
        <div className="progress" aria-hidden>
          <div style={{ width: `${(done / STEPS.length) * 100}%` }} />
        </div>
      </div>
      <ol className="steps">
        {STEPS.map(([key, label, Icon], i) => {
          const status = checks[key]?.status;
          return (
            <li key={key} className={`step step-${status ?? "loading"}`}>
              <span className="step-icon" aria-hidden>
                {status === "ready" ? <Check size={16} strokeWidth={3} /> : <Icon size={16} />}
              </span>
              <span>
                <span className="step-label">
                  {i + 1}. {label}
                </span>
                <span className="step-status">{status === "ready" ? "Verified" : status === "unverified" ? "Needs a test" : status === "blocked" ? "Not set up" : "Checking…"}</span>
              </span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function Section({ icon: Icon, tint, title, subtitle, check, children, actions }: { icon: LucideIcon; tint: string; title: string; subtitle: string; check?: ReadinessCheck; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="card">
      <div className="card-head" style={{ alignItems: "flex-start", paddingTop: 18 }}>
        <div className="row-icon" style={{ background: tint, width: 38, height: 38, borderRadius: 10 }}>
          <Icon size={19} aria-hidden />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="row" style={{ flexWrap: "wrap" }}>
            <h2 className="card-title">{title}</h2>
            {check && <CheckPill status={check.status} />}
          </div>
          <p className="card-sub">{check?.message ?? subtitle}</p>
        </div>
        {actions}
      </div>
      <div className="card-pad" style={{ paddingTop: 4 }}>
        {children}
      </div>
    </section>
  );
}

// ---------- Shared sandbox line (trials) ----------

/** "+13127558589" → "+1 (312) 755-8589" for US numbers; others unchanged. */
function prettyNumber(e164: string): string {
  const m = /^\+1(\d{3})(\d{3})(\d{4})$/.exec(e164);
  return m ? `+1 (${m[1]}) ${m[2]}-${m[3]}` : e164;
}

function SandboxCard({ line, agents, isAdmin, onChange }: { line: SandboxLine; agents: AgentSummary[]; isAdmin: boolean; onChange: () => void }) {
  const toast = useToast();
  const inbound = agents.filter((a) => a.template.direction === "inbound");
  const chosen = inbound.find((a) => a.id === line.agent_id);
  const save = useMutation({
    mutationFn: (body: { agent_id?: string; outbound?: boolean; new_code?: boolean }) => api.put<SandboxLine>("/connections/sandbox", body),
    onSuccess: () => onChange(),
    onError: (e) => toast(e instanceof ApiError ? e.message : "Could not save"),
  });
  const number = line.number ? prettyNumber(line.number) : "";
  const copy = (text: string, what: string) => navigator.clipboard?.writeText(text).then(() => toast(`${what} copied`));

  return (
    <Section
      icon={Phone}
      tint="var(--tile-green)"
      title="Sandbox line"
      subtitle="Try your agents on a real phone call before connecting your own number. Shared by trial workspaces, with daily limits."
    >
      <div className="stack" style={{ gap: 14 }}>
        <div className="row" style={{ gap: 28, flexWrap: "wrap", alignItems: "flex-end" }}>
          <div className="stack" style={{ gap: 2 }}>
            <span className="field-label">Call</span>
            <button type="button" className="link-button mono" style={{ fontSize: 22, fontWeight: 600 }} onClick={() => copy(line.number ?? "", "Number")} title="Copy number">
              {number}
            </button>
          </div>
          <div className="stack" style={{ gap: 2 }}>
            <span className="field-label">Then key in your workspace code</span>
            <button type="button" className="link-button mono" style={{ fontSize: 22, fontWeight: 600, letterSpacing: 3 }} onClick={() => copy(line.code ?? "", "Code")} title="Copy code">
              {line.code}
            </button>
          </div>
          {isAdmin && (
            <button className="btn btn-plain" disabled={save.isPending} onClick={() => confirm("Make a new code? The current one stops working.") && save.mutate({ new_code: true })}>
              <RefreshCw size={14} aria-hidden /> New code
            </button>
          )}
        </div>

        <label className="field" style={{ maxWidth: 420 }}>
          <span className="field-label">Agent that answers</span>
          <select className="select" value={line.agent_id ?? ""} disabled={!isAdmin || save.isPending} onChange={(e) => save.mutate({ agent_id: e.target.value })}>
            <option value="">Choose an inbound agent…</option>
            {inbound.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
                {a.current ? ` · published v${a.current.number}` : " · not published"}
              </option>
            ))}
          </select>
          <span className="field-help">
            {chosen
              ? chosen.current
                ? `Callers who key in your code talk to ${chosen.name} (published v${chosen.current.number}).`
                : `Publish ${chosen.name} first; until then callers hear that the line isn't ready.`
              : "Until you choose one, callers with your code hear that the line isn't set up yet."}
          </span>
        </label>

        {line.outbound_available && (
          <div className="row" style={{ gap: 10, alignItems: "flex-start" }}>
            <Switch checked={line.outbound} onChange={(v) => save.mutate({ outbound: v })} label="Use the sandbox line for outbound test calls" disabled={!isAdmin || save.isPending} />
            <div className="stack" style={{ gap: 2 }}>
              <span>Use the sandbox line for outbound test calls</span>
              <span className="small muted">
                Calls show {number} as caller ID and go only to your approved test numbers (up to {line.max_destinations}). Turn this off once your own number is connected.
              </span>
            </div>
          </div>
        )}

        <div className="small muted">
          {line.calls_today} of {line.daily_limit} sandbox calls used in the last 24 hours · up to {Math.round(line.max_call_s / 60)} minutes each
        </div>
      </div>
    </Section>
  );
}

// ---------- LiveKit ----------

function LiveKitCard({ o, check, worker, isAdmin, onChange }: { o?: ConnectionsOverview; check?: ReadinessCheck; worker?: ReadinessCheck; isAdmin: boolean; onChange: () => void }) {
  const lk = o?.livekit;
  const [probe, setProbe] = useState<ProbeStatus | null>(null);
  const start = useMutation({ mutationFn: () => api.post<ProbeStatus>("/connections/agent-test"), onSuccess: setProbe });
  const latest = probe ?? o?.worker_probe ?? null;

  useEffect(() => {
    if (!probe || probe.status !== "waiting") return;
    const t = setTimeout(async () => {
      const next = await api.get<ProbeStatus>(`/connections/agent-test/${probe.id}`);
      setProbe(next);
      if (next.status !== "waiting") onChange();
    }, 1000);
    return () => clearTimeout(t);
  }, [probe, onChange]);

  return (
    <Section icon={Radio} tint="var(--tile-pink)" title="LiveKit" subtitle="Media, SIP and agent hosting." check={check}>
      {!lk?.configured ? (
        <div className="stack" style={{ gap: 12 }}>
          <p style={{ margin: 0 }}>Connect your LiveKit Cloud project. Keys stay on the server and are never shown in the Studio.</p>
          <ol className="setup-list">
            <li>
              In LiveKit Cloud, open <strong>Settings → API keys</strong> and create a key for this environment.
            </li>
            <li>Give the project URL, API key and secret to your CarePlix administrator. They are stored in the server's secret store, never in the Studio.</li>
            <li>
              Once they're in place, press <strong>Re-check</strong>.
            </li>
          </ol>
        </div>
      ) : (
        <div className="stack" style={{ gap: 16 }}>
          <div className="row" style={{ flexWrap: "wrap", gap: 24 }}>
            <Fact label="Project" value={lk.project ?? "—"} mono />
            <Fact label="Agent name" value={lk.agent_name} mono />
            <Fact label="Active rooms" value={String(lk.inventory?.active_rooms ?? "—")} />
            <Fact label="Carrier trunks" value={String(lk.inventory?.outbound_trunks.length ?? "—")} />
            <Fact label="Dispatch rules" value={String(lk.inventory?.dispatch_rules.length ?? "—")} />
          </div>
          {lk.error && <Banner tone="red" title={`LiveKit returned ${lk.error}.`}> Check the URL and API key.</Banner>}

          <div className="subcard">
            <div className="row" style={{ alignItems: "flex-start" }}>
              <div style={{ flex: 1 }}>
                <div className="row">
                  <strong>Agent worker</strong>
                  {worker && <CheckPill status={worker.status} />}
                </div>
                <div className="small muted" style={{ marginTop: 2 }}>
                  Sends a test dispatch to <span className="mono">{lk.agent_name}</span> and waits for the agent runtime to answer.
                </div>
                {latest && <ProbeLine probe={latest} />}
              </div>
              {isAdmin && (
                <button className="btn btn-tinted" onClick={() => start.mutate()} disabled={start.isPending || probe?.status === "waiting"}>
                  {probe?.status === "waiting" ? <span className="spinner" aria-label="Waiting" /> : <Cpu size={15} aria-hidden />} Test agent
                </button>
              )}
            </div>
            {start.error && <div className="field-error" style={{ marginTop: 8 }}>{(start.error as ApiError).message}</div>}
          </div>
        </div>
      )}
    </Section>
  );
}

function ProbeLine({ probe }: { probe: ProbeStatus }) {
  const text = {
    waiting: "Waiting for a worker to answer…",
    answered: `Answered in ${probe.latency_ms} ms by build ${probe.runtime_build ?? "unknown"} · ${timeAgo(probe.acked_at)}`,
    no_worker: "No worker answered within 30 seconds. Check that the agent runtime is running and connected to this LiveKit project.",
    failed: `Dispatch failed (${probe.error}).`,
  }[probe.status];
  const tone = probe.status === "answered" ? "var(--green)" : probe.status === "waiting" ? "var(--label-2)" : "var(--orange)";
  return (
    <div className="small" style={{ marginTop: 8, color: tone }} role="status">
      {text}
    </div>
  );
}

function Fact({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <div className="call-meta-label">{label}</div>
      <div className={`call-meta-value${mono ? " mono" : ""}`}>{value}</div>
    </div>
  );
}

// ---------- Gemini ----------

function GeminiCard({ o, check, isAdmin, onChange }: { o?: ConnectionsOverview; check?: ReadinessCheck; isAdmin: boolean; onChange: () => void }) {
  const m = o?.model;
  const [project, setProject] = useState("");
  const [location, setLocation] = useState("us-central1");
  const [model, setModel] = useState("");
  useEffect(() => {
    if (!m) return;
    setProject(m.gcp_project ?? "");
    setLocation(m.vertex_location ?? "us-central1");
    setModel(m.approved_model ?? m.known_models[0] ?? "");
  }, [m]);
  const dirty = !!m && (project !== (m.gcp_project ?? "") || location !== (m.vertex_location ?? "us-central1") || model !== (m.approved_model ?? ""));
  const save = useMutation({ mutationFn: () => api.put("/connections/model", { model_id: model, gcp_project: project, vertex_location: location }), onSuccess: onChange });
  const test = useMutation({ mutationFn: () => api.post("/connections/model/test"), onSettled: onChange });
  const c = m?.check;

  return (
    <Section icon={Sparkles} tint="var(--tile-navy)" title="Gemini Live" subtitle="Native-audio voice model on Vertex AI." check={check}>
      <div className="grid-3" style={{ gap: 14 }}>
        <label className="field">
          <span className="field-label">Google Cloud project</span>
          <input className="input mono" value={project} placeholder="careplix-voice-dev" onChange={(e) => setProject(e.target.value.trim())} disabled={!isAdmin} />
        </label>
        <label className="field">
          <span className="field-label">Vertex AI region</span>
          <input className="input mono" list="regions" value={location} onChange={(e) => setLocation(e.target.value.trim())} disabled={!isAdmin} />
          <datalist id="regions">
            {REGIONS.map((r) => (
              <option key={r} value={r} />
            ))}
          </datalist>
        </label>
        <label className="field">
          <span className="field-label">Approved model</span>
          <input className="input mono" list="models" value={model} onChange={(e) => setModel(e.target.value.trim())} disabled={!isAdmin} />
          <datalist id="models">
            {m?.known_models.map((x) => (
              <option key={x} value={x} />
            ))}
          </datalist>
        </label>
      </div>
      <div className="small muted" style={{ marginTop: 10 }}>
        Credentials: {m?.credential_source === "application_default" ? (
          <>
            the server's Google Cloud service identity (Application Default Credentials).
          </>
        ) : (
          "restricted service-account key from the server environment."
        )}{" "}
        Agents can only be published with this model.
      </div>
      {c && (
        <div className="subcard" style={{ marginTop: 14 }}>
          {c.ok ? (
            <div className="row" style={{ flexWrap: "wrap", gap: 24 }}>
              <Fact label="Connected in" value={`${c.connect_ms} ms`} />
              <Fact label="First audio" value={`${c.first_audio_ms} ms`} />
              <Fact label="Session resumption" value={c.resumption_handle_received ? "Supported" : "Not observed"} />
              <Fact label="Tested" value={timeAgo(c.at)} />
              {c.reply_transcript && <div className="small muted" style={{ flexBasis: "100%" }}>Model said: “{c.reply_transcript}”</div>}
            </div>
          ) : (
            <div className="small" style={{ color: "var(--red)" }}>
              Test failed · {c.error}
              {c.error_detail ? ` — ${c.error_detail}` : ""}
            </div>
          )}
        </div>
      )}
      {(save.error || test.error) && <div className="field-error" style={{ marginTop: 8 }}>{((save.error || test.error) as ApiError).message}</div>}
      {isAdmin && (
        <div className="row" style={{ marginTop: 16 }}>
          <span className="spacer" />
          <button className="btn btn-secondary" disabled={!dirty || !project || !model || save.isPending} onClick={() => save.mutate()}>
            Save
          </button>
          <button className="btn btn-primary" disabled={dirty || !m?.approved_model || test.isPending} onClick={() => test.mutate()} title={dirty ? "Save first" : undefined}>
            {test.isPending ? <span className="spinner" aria-label="Testing" /> : <AudioLines size={15} aria-hidden />} Test model
          </button>
        </div>
      )}
    </Section>
  );
}

// ---------- Inbound ----------

function InboundCard({ o, check, routes, agents, isAdmin, onChange }: { o?: ConnectionsOverview; check?: ReadinessCheck; routes: Route[]; agents: AgentSummary[]; isAdmin: boolean; onChange: () => void }) {
  const [adding, setAdding] = useState(false);
  const toast = useToast();
  const qualify = useMutation({
    mutationFn: (id: string) => api.post(`/connections/inbound/${id}/qualify`, {}),
    onSuccess: () => {
      toast("Inbound route qualified");
      onChange();
    },
  });
  const configured = !!o?.livekit.configured;
  return (
    <Section
      icon={PhoneIncoming}
      tint="var(--tile-teal)"
      title="Inbound numbers"
      subtitle="Calls to these numbers are answered by a CarePlix agent."
      check={check}
      actions={
        isAdmin && (
          <button className="btn btn-tinted" onClick={() => setAdding(true)} disabled={!configured}>
            <Plus size={15} aria-hidden /> Connect number
          </button>
        )
      }
    >
      {routes.length === 0 ? (
        <Empty icon={PhoneIncoming} title="No numbers connected">
          Buy a US number in LiveKit Cloud (<strong>Telephony → Phone numbers</strong>) or with <code className="mono">lk number purchase</code>, then connect it here. CarePlix creates the dispatch rule for you.
        </Empty>
      ) : (
        <ul className="list" style={{ margin: "0 -22px" }}>
          {routes.map((r) => (
            <li className="list-row" key={r.id} style={{ alignItems: "flex-start", paddingTop: 14, paddingBottom: 14 }}>
              <div className="row-main">
                <div className="row-title">
                  <span className="tabular">{r.number}</span> · {r.name}
                </div>
                <div className="row-sub" style={{ whiteSpace: "normal" }}>
                  Answers with {agents.find((a) => a.id === r.agent_id)?.name ?? "—"} · {r.provider === "livekit_number" ? "LiveKit number: inbound only, no forwarding" : "Carrier number"}
                  {typeof r.provider_refs.dispatch_rule_id === "string" && (
                    <>
                      {" "}
                      · rule <span className="mono">{String(r.provider_refs.dispatch_rule_id)}</span>
                      {r.provider_refs.scope === "project" && " (applies to every number in the LiveKit project; CarePlix answers only connected numbers)"}
                    </>
                  )}
                </div>
                <div className="small muted">{r.last_call_at ? `Last call ${timeAgo(r.last_call_at)}` : "Call this number from your phone to test it."}</div>
              </div>
              {r.verified_at ? (
                <Pill tone="green" icon={ShieldCheck}>
                  Qualified
                </Pill>
              ) : r.last_call_at && isAdmin ? (
                <button className="btn btn-secondary" onClick={() => qualify.mutate(r.id)} disabled={qualify.isPending}>
                  Mark qualified
                </button>
              ) : (
                <Pill tone="orange" icon={CircleDashed}>
                  Not qualified
                </Pill>
              )}
            </li>
          ))}
        </ul>
      )}
      {qualify.error && <div className="field-error">{(qualify.error as ApiError).message}</div>}
      {adding && <ConnectNumber agents={agents.filter((a) => a.template.direction === "inbound")} onClose={() => setAdding(false)} onDone={onChange} />}
    </Section>
  );
}

function ConnectNumber({ agents, onClose, onDone }: { agents: AgentSummary[]; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [name, setName] = useState("Authorization line");
  const [number, setNumber] = useState("");
  const [provider, setProvider] = useState("livekit_number");
  const [agentId, setAgentId] = useState(agents[0]?.id ?? "");
  const agent = agents.find((a) => a.id === agentId);
  const save = useMutation({
    mutationFn: () => api.post<{ dispatch_rule_id: string; reused: boolean }>("/connections/inbound", { name, number, provider, agent_id: agentId }),
    onSuccess: (r) => {
      toast(r.reused ? "Number connected using its existing dispatch rule" : "Number connected · dispatch rule created");
      onDone();
      onClose();
    },
  });
  return (
    <Sheet
      title="Connect inbound number"
      subtitle="CarePlix creates a LiveKit dispatch rule that sends calls to this number to the agent. The called number decides the agent; caller ID is never trusted."
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" onClick={() => save.mutate()} disabled={!number || !agentId || save.isPending}>
            Connect
          </button>
        </>
      }
    >
      <label className="field">
        <span className="field-label">Phone number</span>
        <input className="input tabular" placeholder="+1 202 555 0100" value={number} onChange={(e) => setNumber(e.target.value)} autoFocus />
        <span className="field-help">A number already in your LiveKit project.</span>
      </label>
      <label className="field">
        <span className="field-label">Name</span>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="field">
        <span className="field-label">Number type</span>
        <select className="select" value={provider} onChange={(e) => setProvider(e.target.value)}>
          <option value="livekit_number">LiveKit number (inbound only)</option>
          <option value="carrier">Carrier number on an inbound trunk</option>
        </select>
      </label>
      <label className="field">
        <span className="field-label">Answering agent</span>
        <select className="select" value={agentId} onChange={(e) => setAgentId(e.target.value)}>
          {agents.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name} {a.current ? `· v${a.current.number}` : "· not published"}
            </option>
          ))}
        </select>
        {agent && !agent.current && <span className="field-help" style={{ color: "var(--orange)" }}>Publish this agent before calling the number; unpublished agents can't answer.</span>}
      </label>
      {save.error && <Banner tone="red" title={(save.error as ApiError).message} />}
    </Sheet>
  );
}

// ---------- Outbound ----------

function OutboundCard({ o, check, agents, isAdmin, onChange }: { o?: ConnectionsOverview; check?: ReadinessCheck; agents: AgentSummary[]; isAdmin: boolean; onChange: () => void }) {
  const toast = useToast();
  const [adding, setAdding] = useState(false);
  const [destination, setDestination] = useState("");
  const [testAttempt, setTestAttempt] = useState<string | null>(() => localStorage.getItem("cpx-test-call"));
  const out = o?.outbound;
  const trunks = o?.livekit.inventory?.outbound_trunks ?? [];
  const allDestinations = [...new Set([...(out?.approved_destinations ?? []), ...(out?.env_destinations ?? [])])];
  const outboundAgents = agents.filter((a) => a.template.direction === "outbound" && a.current);
  const [agentId, setAgentId] = useState("");
  const [testDest, setTestDest] = useState("");

  const update = useMutation({ mutationFn: (body: object) => api.put("/connections/outbound", body), onSuccess: onChange });
  const call = useMutation({
    mutationFn: () => api.post<{ attempt_id: string }>("/connections/outbound/test-call", { agent_id: agentId || outboundAgents[0]?.id, destination: testDest || allDestinations[0] }),
    onSuccess: (r) => {
      localStorage.setItem("cpx-test-call", r.attempt_id);
      setTestAttempt(r.attempt_id);
    },
  });
  const qualify = useMutation({
    mutationFn: () => api.post("/connections/outbound/qualify", { attempt_id: testAttempt }),
    onSuccess: () => {
      toast("Outbound route qualified");
      onChange();
    },
  });
  const attempt = useQuery({
    queryKey: ["call", testAttempt],
    queryFn: () => api.get<Attempt>(`/calls/${testAttempt}`),
    enabled: !!testAttempt,
    refetchInterval: (q) => (q.state.data && ["Ended", "Failed", "Cancelled"].includes(q.state.data.state) ? false : 2000),
  });

  const removeDestination = (n: string) => update.mutate({ approved_destinations: (out?.approved_destinations ?? []).filter((x) => x !== n) });
  const addDestination = () => {
    if (!destination.trim()) return;
    update.mutate({ approved_destinations: [...(out?.approved_destinations ?? []), destination] }, { onSuccess: () => setDestination("") });
  };
  const a = attempt.data;

  return (
    <Section
      icon={PhoneOutgoing}
      tint="var(--tile-blue)"
      title="Outbound calling"
      subtitle="Calls go out through a SIP carrier trunk in LiveKit."
      check={check}
      actions={
        isAdmin && (
          <button className="btn btn-tinted" onClick={() => setAdding(true)} disabled={!o?.livekit.configured}>
            <Plus size={15} aria-hidden /> Add carrier trunk
          </button>
        )
      }
    >
      <div className="stack" style={{ gap: 18 }}>
        <Banner tone="blue" title="LiveKit numbers can't place outbound calls.">
          {" "}
          Use a SIP trunk from a carrier such as Twilio, Telnyx or Plivo. Its SIP password goes to LiveKit only.
        </Banner>
        <div className="grid-2" style={{ gap: 18 }}>
          <label className="field">
            <span className="field-label">Carrier trunk</span>
            <select className="select" value={out?.trunk_id ?? ""} onChange={(e) => update.mutate({ trunk_id: e.target.value })} disabled={!isAdmin || trunks.length === 0}>
              <option value="">{trunks.length ? "Choose a trunk" : "No trunks in LiveKit yet"}</option>
              {trunks.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name || t.id} · {t.address} · {t.numbers.join(", ")}
                </option>
              ))}
            </select>
            {out?.trunk_from_env && <span className="field-help">Set in the server configuration.</span>}
          </label>
          <div className="field">
            <span className="field-label">Approved test destinations</span>
            <div className="row" style={{ flexWrap: "wrap" }}>
              {(out?.approved_destinations ?? []).map((n) => (
                <span className="chip tabular" key={n}>
                  {n}
                  {isAdmin && (
                    <button type="button" aria-label={`Remove ${n}`} onClick={() => removeDestination(n)} style={{ border: 0, background: "none", cursor: "pointer", padding: 0, color: "var(--label-3)", display: "inline-flex" }}>
                      <X size={12} />
                    </button>
                  )}
                </span>
              ))}
              {out?.env_destinations.map((n) => (
                <span className="chip tabular" key={`env-${n}`} title="From APPROVED_TEST_DESTINATIONS">
                  {n} · env
                </span>
              ))}
            </div>
            {isAdmin && (
              <form
                className="row"
                onSubmit={(e) => {
                  e.preventDefault();
                  addDestination();
                }}
              >
                <input className="input tabular" placeholder="+1 your test phone" value={destination} onChange={(e) => setDestination(e.target.value)} />
                <button className="btn btn-secondary" type="submit">
                  Add
                </button>
              </form>
            )}
            <span className="field-help">Release D only dials numbers on this list. Use phones you own.</span>
          </div>
        </div>
        {update.error && <div className="field-error">{(update.error as ApiError).message}</div>}

        <div className="subcard">
          <strong>Qualification call</strong>
          <div className="small muted" style={{ margin: "2px 0 12px" }}>
            Calls an approved number with a published outbound agent. Answer it, check two-way audio, then confirm. The route is qualified only after you confirm.
          </div>
          <div className="row" style={{ flexWrap: "wrap" }}>
            <select className="select" style={{ width: "auto" }} value={agentId} onChange={(e) => setAgentId(e.target.value)} aria-label="Agent">
              {outboundAgents.length === 0 && <option value="">Publish an outbound agent first</option>}
              {outboundAgents.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.name} · v{x.current?.number}
                </option>
              ))}
            </select>
            <select className="select" style={{ width: "auto" }} value={testDest} onChange={(e) => setTestDest(e.target.value)} aria-label="Destination">
              {allDestinations.length === 0 && <option value="">Add a destination first</option>}
              {allDestinations.map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
            <button className="btn btn-primary" disabled={!isAdmin || !out?.trunk_id || !outboundAgents.length || !allDestinations.length || call.isPending} onClick={() => call.mutate()}>
              <PhoneOutgoing size={15} aria-hidden /> Place test call
            </button>
          </div>
          {call.error && <CallBlocked error={call.error} onCleared={() => call.reset()} />}
          {a && (
            <div className="row" style={{ marginTop: 12, flexWrap: "wrap" }}>
              <AttemptPill state={a.state} substate={a.substate} />
              <span className="small muted">
                {a.destination} · {timeAgo(a.created_at)}
                {a.disposition && a.disposition !== "completed" ? ` · ${a.disposition.replace(/_/g, " ")}` : ""}
              </span>
              <Link to={`/calls/${a.id}`} className="btn btn-plain small">
                Open call <ExternalLink size={12} aria-hidden />
              </Link>
              <span className="spacer" />
              {out?.verified && out.evidence_attempt_id === a.id ? (
                <Pill tone="green" icon={ShieldCheck}>
                  Qualified
                </Pill>
              ) : a.state === "Ended" && a.connected_at ? (
                <button className="btn btn-secondary" onClick={() => qualify.mutate()} disabled={qualify.isPending}>
                  I heard two-way audio — qualify route
                </button>
              ) : null}
            </div>
          )}
          {qualify.error && <div className="field-error">{(qualify.error as ApiError).message}</div>}
        </div>
      </div>
      {adding && <AddTrunk onClose={() => setAdding(false)} onDone={onChange} />}
    </Section>
  );
}

function AddTrunk({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [form, setForm] = useState({ name: "Carrier", address: "", number: "", username: "", password: "", transport: "auto" });
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const save = useMutation({
    mutationFn: () => api.post<{ trunk_id: string }>("/connections/outbound/trunks", form),
    onSuccess: (r) => {
      toast(`Trunk ${r.trunk_id} created and selected`);
      onDone();
      onClose();
    },
  });
  return (
    <Sheet
      title="Add carrier trunk"
      subtitle="Creates an outbound SIP trunk in your LiveKit project. Credentials are sent to LiveKit and are not stored by CarePlix."
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" onClick={() => save.mutate()} disabled={!form.address || !form.number || save.isPending}>
            Create trunk
          </button>
        </>
      }
    >
      <label className="field">
        <span className="field-label">Name</span>
        <input className="input" value={form.name} onChange={set("name")} />
      </label>
      <label className="field">
        <span className="field-label">Carrier SIP address</span>
        <input className="input mono" placeholder="careplix.pstn.twilio.com" value={form.address} onChange={set("address")} autoFocus />
        <span className="field-help">From your carrier's SIP trunk (termination) settings.</span>
      </label>
      <label className="field">
        <span className="field-label">Caller number</span>
        <input className="input tabular" placeholder="+1 202 555 0199" value={form.number} onChange={set("number")} />
        <span className="field-help">A number you own at the carrier. It appears as the caller ID.</span>
      </label>
      <div className="grid-2" style={{ gap: 14 }}>
        <label className="field">
          <span className="field-label">SIP username</span>
          <input className="input" autoComplete="off" value={form.username} onChange={set("username")} />
        </label>
        <label className="field">
          <span className="field-label">SIP password</span>
          <input className="input" type="password" autoComplete="new-password" value={form.password} onChange={set("password")} />
        </label>
      </div>
      <label className="field">
        <span className="field-label">Transport</span>
        <select className="select" value={form.transport} onChange={set("transport")}>
          <option value="auto">Automatic</option>
          <option value="tls">TLS (encrypted signalling)</option>
          <option value="tcp">TCP</option>
          <option value="udp">UDP</option>
        </select>
      </label>
      {save.error && <Banner tone="red" title={(save.error as ApiError).message} />}
    </Sheet>
  );
}
