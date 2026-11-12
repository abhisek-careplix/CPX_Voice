import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, ChevronLeft, Lock, PhoneOutgoing, Search, ShieldCheck } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, ApiError, type Attempt, type CaseDetail, type CaseItem, type Readiness, type Task, type Template } from "../api";
import { Banner, Card, humanize, Toolbar } from "../ui";
import { CaseStatusPill, FactList, useCaseTypes } from "./Cases";

const PACK_NAME: Record<string, string> = {
  "clinical-scope": "Clinical scope",
  "patient-privacy": "Patient privacy",
  "financial-safety": "Financial safety",
  "manipulation-resistance": "Manipulation resistance",
  "outreach-consent": "Outreach consent",
};

export default function TaskComposer() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [params] = useSearchParams();
  const templates = useQuery({ queryKey: ["templates"], queryFn: () => api.get<Template[]>("/templates") });
  const caseTypes = useCaseTypes();
  const readiness = useQuery({ queryKey: ["readiness"], queryFn: () => api.get<Readiness>("/readiness") });
  const [query, setQuery] = useState("");
  const cases = useQuery({ queryKey: ["cases", "composer", query], queryFn: () => api.get<CaseItem[]>(`/cases?q=${encodeURIComponent(query)}`) });
  const [caseId, setCaseId] = useState(params.get("case") ?? "");
  const detail = useQuery({ queryKey: ["case", caseId], queryFn: () => api.get<CaseDetail>(`/cases/${caseId}`), enabled: !!caseId });
  const [agentId, setAgentId] = useState("");
  const [destination, setDestination] = useState("");
  const [note, setNote] = useState("");
  // One key per composed attempt: a double click or a retried request returns the same call (FR11).
  const idempotencyKey = useRef(crypto.randomUUID());

  const selected = detail.data;
  const plan = selected?.plan;
  // Picking a case fills in the agent built for it and, when it's approved, the number on file.
  useEffect(() => {
    if (!plan) return;
    setAgentId(plan.suggested_agent_id ?? "");
    setDestination(plan.destination.on_file_approved && plan.destination.on_file ? plan.destination.on_file : "");
  }, [plan]);

  const agent = plan?.agents.find((a) => a.id === agentId);
  const template = templates.data?.find((t) => t.id === agent?.template_id);
  const templateNames = (ids: string[]) => ids.map((id) => templates.data?.find((t) => t.id === id)?.name ?? humanize(id)).join(" or ");
  const outbound = readiness.data?.checks.find((c) => c.name === "outbound_route");
  const phoneBlocked = outbound?.status === "blocked";

  const start = useMutation({
    mutationFn: async () => {
      const task = await api.post<Task>("/tasks", { agent_id: agentId, case_id: caseId, destination, additional_instructions: note || null });
      return api.post<Attempt>(`/tasks/${task.id}/calls`, {}, { "Idempotency-Key": idempotencyKey.current });
    },
    onSuccess: (attempt) => {
      qc.invalidateQueries({ queryKey: ["workspace-summary"] });
      qc.invalidateQueries({ queryKey: ["cases"] });
      navigate(`/calls/${attempt.id}`);
    },
  });
  const error = start.error instanceof ApiError ? start.error : null;
  const destError = error && ["invalid_destination", "destination_not_approved"].includes(error.code) ? error.message : null;
  const blocked = !!selected && (selected.status === "closed" || selected.missing.length > 0);
  const ready = agentId && caseId && destination && !phoneBlocked && !blocked;

  return (
    <>
      <Toolbar
        eyebrow={
          <Link to="/tasks" className="row" style={{ gap: 2 }}>
            <ChevronLeft size={15} aria-hidden /> Tasks
          </Link>
        }
        title="New task"
        description="Start from a case. CarePlix picks an agent built for that kind of work and the number on file; you confirm and start the call. The result waits for a reviewer."
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
              <div className="field">
                <span className="field-label">1 · Case</span>
                <div className="row" style={{ position: "relative" }}>
                  <Search size={16} style={{ position: "absolute", left: 12, color: "var(--label-3)" }} aria-hidden />
                  <input className="input" style={{ paddingLeft: 36 }} placeholder="Search by reference, patient, payer or service" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search cases" />
                </div>
                <div className="card" style={{ boxShadow: "none", maxHeight: 260, overflowY: "auto" }}>
                  {cases.data?.length === 0 && (
                    <div className="empty small">
                      No case matches. <Link to="/cases">Add cases</Link>
                    </div>
                  )}
                  {cases.data?.map((c) => (
                    <button
                      key={c.id}
                      type="button"
                      className="list-row"
                      aria-pressed={c.id === caseId}
                      onClick={() => {
                        setCaseId(c.id);
                        start.reset();
                      }}
                      style={{ background: c.id === caseId ? "var(--accent-tint)" : undefined, minHeight: 46, width: "100%", textAlign: "left" }}
                    >
                      <div className="row-main">
                        <div className="row-title">
                          <span className="mono">{c.case_ref}</span> · {c.type_name}
                        </div>
                        <div className="row-sub">{[c.subject, c.headline].filter(Boolean).join(" · ")}</div>
                      </div>
                      {c.status !== "open" && <CaseStatusPill status={c.status} />}
                    </button>
                  ))}
                </div>
              </div>

              <label className="field">
                <span className="field-label">2 · Agent</span>
                <select className="select" value={agentId} onChange={(e) => setAgentId(e.target.value)} required disabled={!plan}>
                  <option value="">{plan ? "Choose an agent" : "Choose a case first"}</option>
                  {plan?.agents.map((a) => (
                    <option key={a.id} value={a.id} disabled={!a.published}>
                      {a.name} {a.published ? `· v${a.version}` : "· not published"}
                    </option>
                  ))}
                </select>
                {plan && plan.agents.length === 0 && (
                  <span className="field-error">
                    No agent can work a {selected?.type_name.toLowerCase()} case yet. Create one from the {templateNames(caseTypes.data?.find((t) => t.id === selected?.case_type)?.agents ?? [])} template on <Link to="/agents">Agents</Link>.
                  </span>
                )}
                {plan && plan.agents.length > 0 && !plan.agents.some((a) => a.published) && <span className="field-help">Publish one of these agents on the Agents screen to place calls.</span>}
                {plan && plan.agents.some((a) => a.published) && <span className="field-help">Only agents built for a {selected?.type_name.toLowerCase()} case are listed.</span>}
              </label>

              <div className="field">
                <label className="field-label" htmlFor="destination">
                  3 · Number to call
                </label>
                <input id="destination" className="input tabular" value={destination} onChange={(e) => setDestination(e.target.value)} placeholder="+1 202 555 0142" inputMode="tel" aria-invalid={!!destError} required />
                {plan && (
                  <span className="field-help">
                    {plan.destination.on_file
                      ? `${plan.destination.label} on file: ${plan.destination.on_file}${plan.destination.on_file_approved ? "." : ". It isn't an approved test number, so call an approved one in its place."}`
                      : `No ${plan.destination.label.toLowerCase()} number on file.`}
                  </span>
                )}
                {plan && plan.destination.approved.length > 0 && (
                  <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
                    {plan.destination.approved.map((n) => (
                      <button key={n} type="button" className="chip" aria-pressed={destination === n} onClick={() => setDestination(n)} style={{ cursor: "pointer", background: destination === n ? "var(--accent-tint)" : undefined }}>
                        <span className="mono">{n}</span>
                      </button>
                    ))}
                  </div>
                )}
                {destError ? <span className="field-error">{destError}</span> : <span className="field-help">Only approved test numbers can be called in this release.</span>}
              </div>

              <label className="field">
                <span className="field-label">Additional instructions (optional)</span>
                <textarea className="textarea" style={{ minHeight: 80 }} value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} placeholder="e.g. Ask whether an expedited review is possible." />
                <span className="field-help">Bounded by the agent's rules. Cannot add tools or disclosures.</span>
              </label>

              {selected?.status === "closed" && <Banner tone="orange" title="This case is closed. Reopen it under Cases to work it again." />}
              {selected && selected.missing.length > 0 && <Banner tone="orange" title="This case is missing facts the call needs. Complete it under Cases first." />}
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
            {!selected || !plan ? (
              <div className="empty small">Choose a case to preview the brief.</div>
            ) : (
              <div className="card-pad stack" style={{ gap: 14, paddingTop: 4 }}>
                <div>
                  <div className="field-label">Calling</div>
                  <p style={{ margin: "4px 0 0" }}>
                    {plan.destination.label}: {plan.destination.name ?? "not given"}
                  </p>
                </div>
                <div>
                  <div className="field-label">Objective</div>
                  <p style={{ margin: "4px 0 0" }}>{plan.objective ?? template?.objective ?? "Chosen by the agent's template."}</p>
                </div>
                <div>
                  <div className="field-label">Facts from the case</div>
                  <FactList fields={template?.required_context.length ? selected.fields.filter((f) => template.required_context.includes(f.key)) : selected.fields} />
                </div>
                {template && (
                  <div>
                    <div className="field-label">Reports back</div>
                    <div className="row" style={{ flexWrap: "wrap", marginTop: 6 }}>
                      {Object.keys(template.output_fields).map((f) => (
                        <span key={f} className="chip">
                          {humanize(f)}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
                {plan.packs.length > 0 && (
                  <div>
                    <div className="field-label">Guardrails added for this kind of case</div>
                    <div className="row" style={{ flexWrap: "wrap", marginTop: 6 }}>
                      {plan.packs.map((p) => (
                        <span key={p} className="chip">
                          <ShieldCheck size={12} aria-hidden /> {PACK_NAME[p] ?? p}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
                <div>
                  <div className="field-label row" style={{ gap: 4 }}>
                    <Lock size={12} aria-hidden /> Restricted actions
                  </div>
                  <ul className="small muted" style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                    <li>No transfers or promises of transfer</li>
                    <li>Never dials a number spoken on the call</li>
                    <li>Never says the facts marked hidden</li>
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
