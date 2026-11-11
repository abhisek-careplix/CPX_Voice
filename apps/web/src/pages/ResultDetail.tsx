import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Building2, CheckCircle2, ChevronLeft, CircleDashed, Copy, History, Pencil, Phone, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError, type CallEvent, type CheckPhase, type Finding, type TaskResult } from "../api";
import { useCan } from "../App";
import { Transcript } from "../live";
import { AttemptPill, Banner, Card, ErrorState, DIRECTION_LABEL, FindingPill, Segmented, TaskPill, Toolbar, duration, humanize, timeAgo, useToast } from "../ui";

type Outcome = "Completed" | "Partial" | "Unsuccessful";

export default function ResultDetail() {
  const { id = "" } = useParams();
  const result = useQuery({ queryKey: ["result", id], queryFn: () => api.get<TaskResult>(`/tasks/${id}/result`), refetchInterval: (q) => {
      const t = q.state.data?.task;
      const pending = Object.values(t?.electronic_check ?? {}).some((p) => p?.state === "pending");
      return t?.state === "Running" || pending ? 2500 : false;
    },
  });
  const [highlight, setHighlight] = useState<Set<string>>(new Set());
  const toast = useToast();
  const r = result.data;

  const events: CallEvent[] = useMemo(
    () =>
      (r?.transcript ?? []).map((s, i) => ({
        seq: i,
        type: "transcript.segment",
        source: "worker",
        occurred_at: s.at,
        payload: { segment_id: s.id, speaker: s.speaker, text: s.text, playout: s.playout },
      })),
    [r?.transcript],
  );

  if (!r)
    return (
      <>
        <Toolbar eyebrow={<Link to="/results" className="row" style={{ gap: 2 }}><ChevronLeft size={15} aria-hidden /> Results</Link>} title={result.error ? "Not available" : <span className="skeleton" style={{ display: "inline-block", width: 260, height: 28 }} aria-label="Loading" />} />
        {result.error && (
          <div className="content">
            <Card>
              <ErrorState error={result.error} onRetry={() => result.refetch()} />
            </Card>
          </div>
        )}
      </>
    );
  const latest = new Map<string, Finding>();
  // Call findings carry transcript evidence; payer-system values are shown in the payer check panel.
  for (const f of r.findings) if (f.source !== "system") latest.set(f.field, f);

  const copySummary = () => {
    const lines = [
      `${r.task.case?.case_ref ?? "Inbound call"} — ${r.task.state}`,
      r.task.outcome_reason ? `Outcome: ${r.task.outcome_reason}` : "",
      ...Object.keys(r.expected_fields).map((f) => {
        const x = latest.get(f);
        return `${humanize(f)}: ${x ? (x.value ?? `unresolved (${x.null_reason})`) : "not captured"}${x ? ` [${x.status.replace(/_/g, " ")}]` : ""}`;
      }),
      r.task.next_action ? `Next action: ${r.task.next_action}` : "",
    ].filter(Boolean);
    navigator.clipboard.writeText(lines.join("\n")).then(() => toast("Summary copied"));
  };

  const showEvidence = (ids: string[]) => {
    setHighlight(new Set(ids));
    document.getElementById(`seg-${ids[ids.length - 1]}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  };

  return (
    <>
      <Toolbar
        eyebrow={
          <Link to="/results" className="row" style={{ gap: 2 }}>
            <ChevronLeft size={15} aria-hidden /> Results
          </Link>
        }
        title={
          <span className="row" style={{ gap: 12 }}>
            {r.task.case?.case_ref ?? "Inbound call"}
            <TaskPill state={r.task.state} />
            {r.task.case?.synthetic && <span className="sample-label">Synthetic</span>}
          </span>
        }
        actions={
          <button className="btn btn-secondary" onClick={copySummary}>
            <Copy size={15} aria-hidden /> Copy Summary
          </button>
        }
      />
      <div className="content">
        <OutcomeCard result={r} />
        <ReadinessCard result={r} />
        <PayerCheckCard result={r} />

        <div className="split">
          <Card title="Findings" subtitle="Each value links to the conversation that supports it. Corrections keep the original.">
            <ul className="list">
              {Object.entries(r.expected_fields).map(([field, description]) => (
                <FindingRow key={field} field={field} description={description} finding={latest.get(field)} critical={r.critical_fields.includes(field)} onEvidence={showEvidence} taskId={id} />
              ))}
            </ul>
          </Card>
          <div className="stack" style={{ gap: 20 }}>
            <Card title="Calls" subtitle="Call disposition is separate from the business outcome.">
              <ul className="list">
                {r.attempts.map((a) => (
                  <li key={a.id}>
                    <Link to={`/calls/${a.id}`} className="list-row">
                      <Phone size={16} color="var(--label-3)" aria-hidden />
                      <div className="row-main">
                        <div className="row-title">{DIRECTION_LABEL[a.direction]}</div>
                        <div className="row-sub">
                          {timeAgo(a.created_at)} · {duration(a.connected_at, a.ended_at)} {a.disposition ? `· ${a.disposition.replace(/_/g, " ")}` : ""}
                        </div>
                      </div>
                      <AttemptPill state={a.state} substate={a.substate} />
                    </Link>
                  </li>
                ))}
              </ul>
            </Card>
            <Card title="Conversation">
              <Transcript events={events} highlight={highlight} emptyText="No conversation recorded." />
            </Card>
          </div>
        </div>
      </div>
    </>
  );
}

function OutcomeCard({ result }: { result: TaskResult }) {
  const canReview = useCan("review");
  const qc = useQueryClient();
  const toast = useToast();
  const t = result.task;
  const [outcome, setOutcome] = useState<Outcome>("Partial");
  const [reason, setReason] = useState("");
  const [next, setNext] = useState(t.next_action ?? "");
  const save = useMutation({
    mutationFn: () => api.post(`/tasks/${t.id}/outcome`, { outcome, reason, next_action: next || null }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["result", t.id] });
      qc.invalidateQueries({ queryKey: ["workspace-summary"] });
      toast(`Outcome recorded: ${outcome}`);
    },
  });

  if (t.state === "Running") {
    return <Banner tone="blue" title="Call in progress">{" "}The result is prepared when the call ends.</Banner>;
  }
  if (t.state !== "Review") {
    return (
      <Card pad>
        <div className="row" style={{ alignItems: "flex-start", gap: 16 }}>
          <div style={{ flex: 1 }}>
            <div className="field-label">Business outcome</div>
            <div style={{ fontSize: 22, fontWeight: 600, letterSpacing: "-0.02em", margin: "4px 0" }}>{t.state}</div>
            <p style={{ margin: 0 }}>{t.outcome_reason}</p>
          </div>
          {t.next_action && (
            <div style={{ flex: 1 }}>
              <div className="field-label">Next action</div>
              <p style={{ margin: "4px 0 0" }}>{t.next_action}</p>
            </div>
          )}
        </div>
      </Card>
    );
  }
  return (
    <Card title="Record the business outcome" subtitle="A connected or ended call is not proof the objective was met. Decide from the evidence below." pad>
      <div className="stack" style={{ gap: 14 }}>
        <Segmented<Outcome>
          label="Outcome"
          value={outcome}
          onChange={setOutcome}
          options={[
            { value: "Completed", label: "Completed" },
            { value: "Partial", label: "Partial" },
            { value: "Unsuccessful", label: "Unsuccessful" },
          ]}
        />
        <div className="grid-2">
          <label className="field">
            <span className="field-label">Reason</span>
            <textarea className="textarea" style={{ minHeight: 70 }} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. Status pending additional information; PT notes requested; date range unclear." />
          </label>
          <label className="field">
            <span className="field-label">Next action</span>
            <textarea className="textarea" style={{ minHeight: 70 }} value={next} onChange={(e) => setNext(e.target.value)} />
          </label>
        </div>
        {save.error && <span className="field-error">{(save.error as ApiError).message}</span>}
        <div className="row">
          <span className="spacer" />
          <button className="btn btn-primary" disabled={!canReview || reason.trim().length < 3 || save.isPending} onClick={() => save.mutate()}>
            Record outcome
          </button>
        </div>
        {!canReview && <span className="field-help">Only reviewers can record outcomes.</span>}
      </div>
    </Card>
  );
}

function FindingRow({ field, description, finding, critical, onEvidence, taskId }: { field: string; description: string; finding?: Finding; critical: boolean; onEvidence: (ids: string[]) => void; taskId: string }) {
  const canReview = useCan("review");
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(finding?.value ?? "");
  const [status, setStatus] = useState<"reviewed" | "conflicting">("reviewed");
  const [reason, setReason] = useState("");
  const [history, setHistory] = useState(false);
  const save = useMutation({
    mutationFn: () => api.post<Finding>(`/findings/${finding!.id}/revisions`, { value: value || null, status, reason }),
    onSuccess: () => {
      setEditing(false);
      setReason("");
      qc.invalidateQueries({ queryKey: ["result", taskId] });
    },
  });

  return (
    <li className="list-row" style={{ alignItems: "flex-start", paddingTop: 14, paddingBottom: 14, display: "block" }}>
      <div className="row" style={{ alignItems: "flex-start" }}>
        <div className="row-main">
          <div className="row" style={{ gap: 6 }}>
            <span style={{ fontWeight: 600 }}>{humanize(field)}</span>
            {critical && <span className="chip">Critical</span>}
          </div>
          <div className="small muted" style={{ whiteSpace: "normal" }}>
            {description}
          </div>
        </div>
        {finding ? <FindingPill status={finding.status} /> : <span className="pill tone-gray">Not captured</span>}
      </div>
      {finding && (
        <div className="stack" style={{ gap: 6, marginTop: 8 }}>
          <div className="row" style={{ flexWrap: "wrap" }}>
            {finding.value !== null ? <span className="finding-value">{finding.value}</span> : <span className="small" style={{ color: "var(--orange)" }}>Unresolved — {finding.null_reason}</span>}
            {finding.evidence_segment_ids.length > 0 && (
              <button className="btn btn-plain small" style={{ height: 24 }} onClick={() => onEvidence(finding.evidence_segment_ids)}>
                Show evidence
              </button>
            )}
            {canReview && !editing && (
              <button className="btn btn-plain small" style={{ height: 24 }} onClick={() => setEditing(true)}>
                <Pencil size={12} aria-hidden /> Correct
              </button>
            )}
            {finding.revisions.length > 0 && (
              <button className="btn btn-plain small" style={{ height: 24 }} onClick={() => setHistory(!history)} aria-expanded={history}>
                <History size={12} aria-hidden /> {finding.revisions.length} revision{finding.revisions.length > 1 ? "s" : ""}
              </button>
            )}
          </div>
          {finding.original_phrasing && <div className="small muted">They said: “{finding.original_phrasing}”</div>}
          {history &&
            finding.revisions.map((rev, i) => (
              <div key={i} className="small muted" style={{ paddingLeft: 10, borderLeft: "2px solid var(--separator)" }}>
                <span className="diff-before">{rev.prior_value ?? "empty"}</span> → <span className="diff-after">{rev.new_value ?? "empty"}</span> · {rev.reason} · {rev.actor}, {timeAgo(rev.at)}
              </div>
            ))}
          {editing && (
            <div className="card" style={{ boxShadow: "none", padding: 14, display: "grid", gap: 10 }}>
              <input className="input mono" value={value} onChange={(e) => setValue(e.target.value)} aria-label={`Corrected ${humanize(field)}`} />
              <Segmented
                label="Verification"
                value={status}
                onChange={setStatus}
                options={[
                  { value: "reviewed", label: "Verified" },
                  { value: "conflicting", label: "Mark conflicting" },
                ]}
              />
              <input className="input" placeholder="Reason for correction (required)" value={reason} onChange={(e) => setReason(e.target.value)} />
              <div className="row">
                <span className="spacer" />
                <button className="btn btn-secondary" onClick={() => setEditing(false)}>
                  Cancel
                </button>
                <button className="btn btn-primary" disabled={reason.trim().length < 3 || save.isPending} onClick={() => save.mutate()}>
                  Save correction
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </li>
  );
}

const VERDICT = {
  match: { label: "Matches call", tone: "var(--green)", icon: CheckCircle2 },
  conflict: { label: "Conflicts with call", tone: "var(--red)", icon: AlertTriangle },
  call_missing: { label: "Not heard on call", tone: "var(--label-2)", icon: CircleDashed },
  system_missing: { label: "Not in payer record", tone: "var(--label-2)", icon: CircleDashed },
} as const;

function PhaseView({ title, phase }: { title: string; phase?: CheckPhase }) {
  if (!phase) return null;
  const s = phase.status;
  return (
    <div className="subcard" style={{ flex: 1, minWidth: 260 }}>
      <div className="row">
        <strong>{title}</strong>
        <span className="spacer" />
        {phase.state === "pending" ? (
          <span className="pill tone-blue">
            <span className="spinner" style={{ width: 10, height: 10 }} aria-hidden /> Checking
          </span>
        ) : phase.state === "done" ? (
          <span className="small muted tabular">{phase.duration_ms} ms</span>
        ) : (
          <span className="pill tone-gray">{phase.state === "skipped" ? (phase.reason ?? "skipped").replace(/_/g, " ") : "failed"}</span>
        )}
      </div>
      {s && (
        <div className="stack" style={{ gap: 4, marginTop: 8 }}>
          <div className="row" style={{ flexWrap: "wrap" }}>
            <span className="finding-value">{s.reported_status.replace(/_/g, " ")}</span>
            {s.review_action && <span className="chip mono">X12 306 · {s.review_action}</span>}
            {s.final && <span className="pill tone-green">Final</span>}
          </div>
          {s.authorization_reference && (
            <div className="small">
              Reference <span className="mono">{s.authorization_reference}</span>
            </div>
          )}
          {s.reason && <div className="small muted">{s.reason}</div>}
        </div>
      )}
      {phase.error && <div className="field-error" style={{ marginTop: 6 }}>{phase.error}</div>}
      {phase.connection && (
        <div className="small muted" style={{ marginTop: 8 }}>
          {phase.connection.name}
          {phase.connection.synthetic ? " · synthetic" : ""}
        </div>
      )}
      {phase.comparison && (
        <ul className="list" style={{ marginTop: 10 }}>
          {Object.entries(phase.comparison).map(([field, verdict]) => {
            const v = VERDICT[verdict];
            return (
              <li key={field} className="row small" style={{ padding: "3px 0" }}>
                <v.icon size={14} color={v.tone} aria-hidden />
                <span style={{ width: 170 }}>{humanize(field)}</span>
                <span style={{ color: v.tone }}>{v.label}</span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function PayerCheckCard({ result }: { result: TaskResult }) {
  const qc = useQueryClient();
  const canRun = useCan("start_task");
  const check = result.task.electronic_check;
  const run = useMutation({
    mutationFn: () => api.post(`/integrations/tasks/${result.task.id}/check`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["result", result.task.id] }),
  });
  if (!result.task.case) return null;
  return (
    <Card
      title={
        <span className="row">
          <Building2 size={16} aria-hidden /> Payer system check
        </span>
      }
      subtitle="Da Vinci PAS status from the payer's Prior Authorization API, before and after the call."
      actions={
        canRun && (
          <button className="btn btn-secondary" onClick={() => run.mutate()} disabled={run.isPending}>
            <RefreshCw size={14} className={run.isPending ? "spin" : ""} aria-hidden /> Check now
          </button>
        )
      }
    >
      <div className="card-pad row" style={{ alignItems: "stretch", flexWrap: "wrap", paddingTop: 4 }}>
        {!check ? (
          <span className="small muted">No electronic check has run for this task.</span>
        ) : (
          <>
            <PhaseView title="Before the call" phase={check.pre_call} />
            <PhaseView title="After the call" phase={check.post_call} />
            <PhaseView title="Latest manual check" phase={check.manual} />
          </>
        )}
      </div>
    </Card>
  );
}

const CAPABILITY_LABEL: Record<string, string> = {
  "provider.npi.validate": "Provider NPI · NPPES",
  "code.icd10.validate": "Diagnosis · ICD-10-CM",
  "code.hcpcs.validate": "Service code · HCPCS",
  "coverage.policy.lookup": "Medicare coverage · NCD/LCD",
};

const CHECK_TONE = { confirmed: "green", invalid: "red", unverifiable: "orange", skipped: "gray", error: "red" } as const;

function ReadinessCard({ result }: { result: TaskResult }) {
  const phase = result.task.electronic_check?.readiness;
  if (!phase) return null;
  return (
    <Card
      title={
        <span className="row">
          <CheckCircle2 size={16} aria-hidden /> Case readiness
        </span>
      }
      subtitle="Validated against public systems of record before any call: NPPES, NLM Clinical Tables, CMS Coverage."
    >
      {phase.state === "pending" ? (
        <div className="card-pad small muted">Checking…</div>
      ) : phase.state !== "done" ? (
        <div className="card-pad small muted">Skipped ({phase.reason?.replace(/_/g, " ")}).</div>
      ) : (
        <ul className="list">
          {phase.checks?.map((c, i) => (
            <li key={i} className="list-row" style={{ alignItems: "flex-start", paddingTop: 12, paddingBottom: 12 }}>
              <div className="row-main">
                <div className="row" style={{ flexWrap: "wrap" }}>
                  <span style={{ fontWeight: 600 }}>{CAPABILITY_LABEL[c.capability] ?? c.capability}</span>
                  <span className="finding-value">{c.subject}</span>
                </div>
                <div className="small muted" style={{ whiteSpace: "normal", marginTop: 2 }}>
                  {c.detail.display ?? c.detail.name ?? c.detail.reason ?? ""}
                  {c.detail.suggestions?.length ? ` · did you mean ${c.detail.suggestions.slice(0, 3).join(", ")}?` : ""}
                </div>
                {c.detail.policies?.length ? (
                  <ul className="small" style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                    {c.detail.policies.map((p) => (
                      <li key={p.id}>
                        <a href={p.url} target="_blank" rel="noreferrer">
                          {p.type} {p.id}
                        </a>{" "}
                        {p.title}
                        {p.contractor ? <span className="muted"> · {p.contractor}</span> : null}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
              <span className="small muted tabular">{c.duration_ms} ms</span>
              <span className={`pill tone-${CHECK_TONE[c.status]}`}>{c.status}</span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
