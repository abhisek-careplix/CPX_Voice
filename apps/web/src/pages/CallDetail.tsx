import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, ClipboardCheck, PhoneMissed, PhoneOff } from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type AgentSummary, type CallDetail as Detail, type Template } from "../api";
import { useCan } from "../App";
import { FindingsChecklist, ToolActivity, Transcript, useCallEvents } from "../live";
import { AttemptPill, Banner, Card, ErrorState, DIRECTION_LABEL, duration, Toolbar, useNow, useToast } from "../ui";

const ACTIVE = ["Created", "Queued", "Dialing", "Connected", "Ending"];
const DISPOSITION: Record<string, string> = {
  busy: "Destination was busy",
  no_answer: "No answer from destination",
  voicemail: "Reached voicemail",
  agent_unavailable: "No agent was available, so the call was not dialed",
  voice_unavailable: "Voice service unavailable — call could not start",
  interrupted: "Call interrupted — findings so far are saved",
  rejected: "The call was rejected by the network",
  failed: "The call failed",
};

export default function CallDetail() {
  const { id = "" } = useParams();
  const qc = useQueryClient();
  const toast = useToast();
  const canStop = useCan("stop_calls");
  const detail = useQuery({ queryKey: ["call", id], queryFn: () => api.get<Detail>(`/calls/${id}`), refetchInterval: (q) => (q.state.data && ACTIVE.includes(q.state.data.state) ? 3000 : false) });
  const agents = useQuery({ queryKey: ["agents"], queryFn: () => api.get<AgentSummary[]>("/agents") });
  const templates = useQuery({ queryKey: ["templates"], queryFn: () => api.get<Template[]>("/templates") });
  const { events, ended } = useCallEvents(id);
  const [highlight, setHighlight] = useState<Set<string>>(new Set());
  const end = useMutation({
    mutationFn: () => api.post(`/calls/${id}/end`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["call", id] });
      toast("Call ended");
    },
  });
  const c = detail.data;
  const active = !!c && ACTIVE.includes(c.state) && !ended;
  useNow(1000, active);
  const templateId = agents.data?.find((a) => a.id === c?.agent.id)?.template.id;
  const template = templates.data?.find((t) => t.id === templateId);

  if (!c)
    return (
      <>
        <Toolbar eyebrow={<Link to="/calls" className="row" style={{ gap: 2 }}><ChevronLeft size={15} aria-hidden /> Calls</Link>} title={detail.error ? "Not available" : <span className="skeleton" style={{ display: "inline-block", width: 260, height: 28 }} aria-label="Loading" />} />
        {detail.error && (
          <div className="content">
            <Card>
              <ErrorState error={detail.error} onRetry={() => detail.refetch()} />
            </Card>
          </div>
        )}
      </>
    );

  return (
    <>
      <Toolbar
        eyebrow={
          <Link to="/calls" className="row" style={{ gap: 2 }}>
            <ChevronLeft size={15} aria-hidden /> Calls
          </Link>
        }
        title={
          <span className="row" style={{ gap: 12 }}>
            {DIRECTION_LABEL[c.direction]}
            <AttemptPill state={c.state} substate={c.substate} />
            {c.sandbox && <span className="sample-label">Sandbox</span>}
          </span>
        }
        actions={
          c.task_id && !active ? (
            <Link to={`/results/${c.task_id}`} className="btn btn-primary">
              <ClipboardCheck size={15} aria-hidden /> Review result
            </Link>
          ) : null
        }
      />
      <div className="content">
        <section className="card call-header" aria-label="Call details">
          <Meta label="Agent" value={`${c.agent.name} v${c.agent.version}${c.agent.version_state === "draft" ? " (draft)" : ""}`} />
          <Meta label="Case" value={c.case_ref ?? "Not linked"} />
          <Meta label={c.direction === "inbound" ? "Caller (reported, not verified)" : "Destination"} value={c.destination ?? c.caller_reported ?? "Browser"} />
          <Meta label="Duration" value={duration(c.connected_at ?? c.created_at, c.ended_at)} />
          <Meta label="Model" value={c.pins.model_id ?? "—"} mono />
          <Meta label="Policy" value={c.pins.policy_version ?? "—"} mono />
        </section>

        {!active && c.disposition && c.disposition !== "completed" && (
          <Banner tone={c.state === "Failed" ? "red" : "orange"} title={DISPOSITION[c.disposition] ?? c.disposition.replace(/_/g, " ")} icon={PhoneMissed}>
            {c.task_id ? " The task is waiting for review — nothing is retried automatically." : ""}
          </Banner>
        )}

        <div className="split">
          <Card title="Conversation" subtitle="Model transcription. Interrupted speech is marked; it may not have been heard in full.">
            <Transcript
              events={events}
              highlight={highlight}
              emptyText={active ? (c.state === "Dialing" ? "Dialing…" : c.state === "Queued" ? "Waiting for the agent to join…" : "Listening…") : "No conversation was recorded."}
            />
          </Card>
          <div className="stack" style={{ gap: 20 }}>
            <Card title="What the agent was told" subtitle="Recorded when the agent joined: the exact persona, party and facts this run used.">
              {c.told.recorded ? (
                <div className="card-pad stack small" style={{ gap: 8, paddingTop: 4 }}>
                  <div>
                    <span className="muted">Persona</span> {c.told.persona.name ?? "—"} · {c.agent.version_state === "draft" ? "draft" : "published"} v{c.agent.version}
                  </div>
                  {c.told.persona.greeting && (
                    <div>
                      <span className="muted">Opens with</span> “{c.told.persona.greeting}”
                    </div>
                  )}
                  <div>
                    <span className="muted">Calling for</span> <strong>{c.told.acting_for ?? "—"}</strong>
                    {c.told.scenario_id ? " (Voice Studio scenario)" : c.case_ref ? ` (case ${c.case_ref})` : " (no case)"}
                  </div>
                  {c.told.objective && (
                    <div>
                      <span className="muted">Objective</span> {c.told.objective}
                    </div>
                  )}
                  {Object.keys(c.told.facts).length > 0 && (
                    <div>
                      <span className="muted">Facts it could share</span>
                      <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                        {Object.entries(c.told.facts).map(([k, v]) => (
                          <li key={k}>
                            <span className="mono">{k}</span>: {v === null ? "—" : String(v)}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </div>
              ) : (
                <div className="card-pad small muted">{active ? "Waiting for the agent to join…" : "Not recorded for this run (runs before this was added, or the agent never joined)."}</div>
              )}
            </Card>
            <Card title="Task checklist">
              {template ? (
                <FindingsChecklist
                  expected={Object.keys(template.output_fields)}
                  findings={c.findings}
                  onEvidence={(ids) => {
                    setHighlight(new Set(ids));
                    document.getElementById(`seg-${ids[ids.length - 1]}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
                  }}
                />
              ) : (
                <div className="skeleton" style={{ height: 80, margin: 20 }} />
              )}
            </Card>
            <Card title="Tool activity" subtitle="Every action is authorised by the server.">
              <ToolActivity tools={c.tools} />
            </Card>
          </div>
        </div>

        {active && canStop && (
          <div className="end-bar" role="region" aria-label="Call controls">
            <AttemptPill state={c.state} substate={c.substate} />
            <span className="tabular small muted">{duration(c.connected_at ?? c.created_at)}</span>
            <span className="spacer" />
            <span className="small muted">Live transfer is not available on this route.</span>
            <button className="btn btn-danger" onClick={() => end.mutate()} disabled={end.isPending || c.state === "Ending"}>
              <PhoneOff size={16} aria-hidden /> End Call
            </button>
          </div>
        )}
      </div>
    </>
  );
}

function Meta({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <div className="call-meta-label">{label}</div>
      <div className={`call-meta-value${mono ? " mono" : ""}`}>{value}</div>
    </div>
  );
}
