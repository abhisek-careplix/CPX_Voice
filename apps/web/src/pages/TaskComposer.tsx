import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, ChevronLeft, Lock, PhoneOutgoing, Search } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, ApiError, type AgentSummary, type Attempt, type CaseItem, type Readiness, type Task, type Template } from "../api";
import { Banner, Card, humanize, Toolbar } from "../ui";

export default function TaskComposer() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const agents = useQuery({ queryKey: ["agents"], queryFn: () => api.get<AgentSummary[]>("/agents") });
  const templates = useQuery({ queryKey: ["templates"], queryFn: () => api.get<Template[]>("/templates") });
  const readiness = useQuery({ queryKey: ["readiness"], queryFn: () => api.get<Readiness>("/readiness") });
  const [query, setQuery] = useState("");
  const cases = useQuery({ queryKey: ["cases", query], queryFn: () => api.get<CaseItem[]>(`/cases?q=${encodeURIComponent(query)}`) });
  const [agentId, setAgentId] = useState("");
  const [caseId, setCaseId] = useState("");
  const [destination, setDestination] = useState("");
  const [note, setNote] = useState("");
  // One key per composed attempt: a double click or a retried request returns the same call (FR11).
  const idempotencyKey = useRef(crypto.randomUUID());

  const outboundAgents = (agents.data ?? []).filter((a) => a.template.direction === "outbound");
  const agent = outboundAgents.find((a) => a.id === agentId);
  const template = templates.data?.find((t) => t.id === agent?.template.id);
  const selectedCase = cases.data?.find((c) => c.id === caseId);
  const outbound = readiness.data?.checks.find((c) => c.name === "outbound_route");
  const phoneBlocked = outbound?.status === "blocked";

  const missing = useMemo(() => {
    if (!template || !selectedCase) return [];
    return template.required_context.filter((f) => selectedCase.context[f] === null || selectedCase.context[f] === undefined || selectedCase.context[f] === "");
  }, [template, selectedCase]);

  const start = useMutation({
    mutationFn: async () => {
      const task = await api.post<Task>("/tasks", { agent_id: agentId, case_id: caseId, destination, additional_instructions: note || null });
      return api.post<Attempt>(`/tasks/${task.id}/calls`, {}, { "Idempotency-Key": idempotencyKey.current });
    },
    onSuccess: (attempt) => {
      qc.invalidateQueries({ queryKey: ["workspace-summary"] });
      navigate(`/calls/${attempt.id}`);
    },
  });
  const error = start.error instanceof ApiError ? start.error : null;
  const destError = error && ["invalid_destination", "destination_not_approved"].includes(error.code) ? error.message : null;
  const ready = agentId && caseId && destination && !phoneBlocked;

  return (
    <>
      <Toolbar
        eyebrow={
          <Link to="/tasks" className="row" style={{ gap: 2 }}>
            <ChevronLeft size={15} aria-hidden /> Tasks
          </Link>
        }
        title="New task"
        description="Pick a case, a published agent and an approved number. The agent calls, records what it learns, and the result waits for a reviewer."
      />
      <div className="content">
        {phoneBlocked && (
          <Banner tone="orange" title="Phone connection missing" icon={AlertCircle}>
            {" "}
            {outbound?.message} <Link to="/connections">Open Connections</Link>
          </Banner>
        )}
        <div className="split">
          <Card>
            <form
              className="card-pad stack"
              style={{ gap: 20 }}
              onSubmit={(e) => {
                e.preventDefault();
                if (ready) start.mutate();
              }}
            >
              <label className="field">
                <span className="field-label">Agent</span>
                <select className="select" value={agentId} onChange={(e) => setAgentId(e.target.value)} required>
                  <option value="">Choose a published agent</option>
                  {outboundAgents.map((a) => (
                    <option key={a.id} value={a.id} disabled={!a.current}>
                      {a.name} · {a.template.name} {a.current ? `· v${a.current.number}` : "· not published"}
                    </option>
                  ))}
                </select>
                {outboundAgents.some((a) => !a.current) && <span className="field-help">Unpublished agents can't place calls. Publish them from the Agents screen.</span>}
              </label>

              <div className="field">
                <span className="field-label">Case</span>
                <div className="row" style={{ position: "relative" }}>
                  <Search size={16} style={{ position: "absolute", left: 12, color: "var(--label-3)" }} aria-hidden />
                  <input className="input" style={{ paddingLeft: 36 }} placeholder="Search by reference, service or payer" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search cases" />
                </div>
                <div className="card" style={{ boxShadow: "none", maxHeight: 240, overflowY: "auto" }}>
                  {cases.data?.length === 0 && <div className="empty small">No accessible case matches.</div>}
                  {cases.data?.map((c) => (
                    <button key={c.id} type="button" className="list-row" aria-pressed={c.id === caseId} onClick={() => setCaseId(c.id)} style={{ background: c.id === caseId ? "var(--accent-tint)" : undefined, minHeight: 46 }}>
                      <div className="row-main">
                        <div className="row-title">
                          {c.case_ref} · {String(c.context.service_description)}
                        </div>
                        <div className="row-sub">
                          {String(c.context.payer_name)} · submitted {String(c.context.submitted_on)}
                        </div>
                      </div>
                      {c.synthetic && <span className="sample-label">Synthetic</span>}
                    </button>
                  ))}
                </div>
              </div>

              <label className="field">
                <span className="field-label">Destination</span>
                <input className="input tabular" value={destination} onChange={(e) => setDestination(e.target.value)} placeholder="+1 202 555 0142" inputMode="tel" aria-invalid={!!destError} required />
                {destError ? <span className="field-error">{destError}</span> : <span className="field-help">E.164 format. Only approved test numbers can be called in this release.</span>}
              </label>

              <label className="field">
                <span className="field-label">Additional instructions (optional)</span>
                <textarea className="textarea" style={{ minHeight: 80 }} value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} placeholder="e.g. Ask whether an expedited review is possible." />
                <span className="field-help">Bounded by the template. Cannot add tools or disclosures.</span>
              </label>

              {error && !destError && <Banner tone="red" title={error.message} />}
              <div className="row">
                <span className="spacer" />
                <button className="btn btn-primary btn-lg" type="submit" disabled={!ready || start.isPending}>
                  <PhoneOutgoing size={18} aria-hidden /> {start.isPending ? "Starting call…" : "Start Call"}
                </button>
              </div>
            </form>
          </Card>

          <Card title="Call brief" subtitle="What the agent will know and do on this call.">
            {!template || !selectedCase ? (
              <div className="empty small">Choose an agent and a case to preview the brief.</div>
            ) : (
              <div className="card-pad stack" style={{ gap: 14, paddingTop: 4 }}>
                <div>
                  <div className="field-label">Objective</div>
                  <p style={{ margin: "4px 0 0" }}>{template.objective}</p>
                </div>
                <div>
                  <div className="field-label">Context provided</div>
                  <ul className="list small" style={{ marginTop: 4 }}>
                    {template.required_context.map((f) => (
                      <li key={f} className="row" style={{ padding: "3px 0" }}>
                        <span className="muted" style={{ width: 150 }}>
                          {humanize(f)}
                        </span>
                        {missing.includes(f) ? <span style={{ color: "var(--orange)" }}>Missing</span> : <span className="mono">{String(selectedCase.context[f])}</span>}
                      </li>
                    ))}
                  </ul>
                </div>
                <div>
                  <div className="field-label">Desired findings</div>
                  <div className="row" style={{ flexWrap: "wrap", marginTop: 6 }}>
                    {Object.keys(template.output_fields).map((f) => (
                      <span key={f} className="chip">
                        {humanize(f)}
                      </span>
                    ))}
                  </div>
                </div>
                <div>
                  <div className="field-label row" style={{ gap: 4 }}>
                    <Lock size={12} aria-hidden /> Restricted actions
                  </div>
                  <ul className="small muted" style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                    <li>No transfers or promises of transfer</li>
                    <li>Never dials a number spoken on the call</li>
                    <li>No disclosure outside the allowed fields</li>
                    <li>No clinical advice or financial commitments</li>
                  </ul>
                </div>
              </div>
            )}
          </Card>
        </div>
      </div>
    </>
  );
}
