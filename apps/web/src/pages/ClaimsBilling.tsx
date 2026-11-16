import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, CheckCircle2, FileText, Inbox, Paperclip, PhoneCall, Receipt, Send, ShieldCheck, Wrench } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../api";
import { useCan } from "../App";
import { Banner, Card, Empty, ErrorState, ListSkeleton, Pill, timeAgo, useToast } from "../ui";

// ---------- Types (mirror apps/api claims billing views) ----------

export type Stage = "draft" | "ready" | "blocked" | "submitted" | "accepted" | "rejected" | "paid" | "partially_paid" | "denied" | "void";

export interface Finding {
  code: string;
  severity: "error" | "warning";
  field: string;
  line_id: string | null;
  message: string;
  source: "careplix" | "clearinghouse";
}

export interface ClaimLine {
  line_id: string;
  cpt: string;
  description?: string | null;
  modifiers: string[];
  units: string;
  charge: string;
  dx: number[];
  date_of_service: string;
}

export interface BilledClaim {
  id: string;
  claim_number: string;
  payer_name: string;
  stage: Stage | null;
  place_of_service: string | null;
  diagnoses: string[];
  lines: ClaimLine[];
  frequency: string | null;
  replaces_claim_id: string | null;
  test_outcome: "paid" | "partial" | "denied" | null;
  scrub: { clean: boolean; findings: Finding[]; checked_at: string; clearinghouse: string } | null;
  submit_mode: string | null;
  submitted_at: string | null;
  ack_status: string | null;
  paid_total: string | null;
  patient_responsibility: string | null;
}

interface Transaction {
  id: string;
  kind: "validate" | "submit" | "277ca" | "835" | "attachment";
  mode: string;
  transaction_id: string | null;
  ok: boolean;
  summary: Record<string, unknown>;
  message: string | null;
  has_raw: boolean;
  created_at: string;
}

interface Reason {
  kind: "277" | "edit" | "carc" | "rarc";
  level?: string;
  group?: string;
  code: string | null;
  text?: string | null;
  message?: string | null;
  amount?: string;
}

interface WorkItem {
  id: string;
  claim_id: string;
  kind: "rejection" | "denial";
  category: string;
  route: "fix_resubmit" | "appeal" | "payer_call" | "write_off";
  state: "open" | "done" | "dismissed";
  title: string;
  reasons: Reason[];
  suggestion: string | null;
  amount: string | null;
  due_on: string | null;
  case_id: string | null;
  appeal_id: string | null;
  intent_ids: string[];
  resolution: string | null;
  claim_number: string;
  payer_name: string;
  created_at: string;
}

interface Posting {
  id: string;
  claim_id: string | null;
  patient_control_number: string;
  payer_claim_number: string;
  payer_name: string | null;
  outcome: "paid" | "partially_paid" | "denied" | "reversed";
  charge: string;
  paid: string;
  patient_responsibility: string;
  adjustments: { group: string; code: string; amount: string; text?: string | null }[];
  remarks: string[];
  trace_number: string | null;
  paid_on: string | null;
  state: "proposed" | "posting" | "posted" | "manual" | "review" | "needs_review";
  intent_ids: string[];
  note: string | null;
}

interface AppealRow {
  id: string;
  claim_id: string;
  claim_number: string;
  payer_name: string;
  level: string;
  state: "draft" | "approved" | "ready_to_send" | "sent" | "decided" | "withdrawn";
  due_on: string;
  days_left: number | null;
  overdue: boolean;
  evidence: { key: string; label: string; have: boolean }[];
  attachments: { attachment_id: string; label: string }[];
  send_note: string | null;
}

interface Charge {
  id: string;
  source: "sample" | "ehr";
  payer_name: string | null;
  date_of_service: string;
  cpt_code: string;
  description: string | null;
  modifiers: string[];
  amount: string;
  diagnoses: string[];
  claim_id: string | null;
}

interface Profile {
  organization: string;
  npi: string;
  tax_id: string;
  taxonomy: string | null;
  address1: string;
  city: string;
  state: string;
  postal_code: string;
  submitter_name: string;
  contact_phone: string;
}

const money = (v: string | null | undefined) => (v ? `$${Number(v).toFixed(2)}` : "–");
export const STAGE_LABEL: Record<Stage, string> = {
  draft: "Draft",
  ready: "Ready to submit",
  blocked: "Needs fixes",
  submitted: "Submitted",
  accepted: "Accepted",
  rejected: "Rejected",
  paid: "Paid",
  partially_paid: "Partially paid",
  denied: "Denied",
  void: "Void",
};
const STAGE_TONE: Record<Stage, "green" | "orange" | "red" | "blue" | "purple" | "gray"> = {
  draft: "gray",
  ready: "blue",
  blocked: "orange",
  submitted: "purple",
  accepted: "blue",
  rejected: "red",
  paid: "green",
  partially_paid: "green",
  denied: "red",
  void: "gray",
};
const ROUTE_LABEL = { fix_resubmit: "Fix and resubmit", appeal: "Appeal", payer_call: "Payer call", write_off: "Write-off (staff approve)" } as const;
const CH_VALIDATION: Record<string, string> = { passed: "passed", failed: "failed", not_connected: "skipped (not connected)", error: "unavailable, try again", skipped: "skipped" };
const KIND_LABEL = { validate: "Validation", submit: "Submission", "277ca": "Acknowledgement (277CA)", "835": "Remittance (835)", attachment: "Attachment (275)" } as const;

export function StagePill({ stage }: { stage: Stage }) {
  return <Pill tone={STAGE_TONE[stage]}>{STAGE_LABEL[stage]}</Pill>;
}

function useInvalidate() {
  const qc = useQueryClient();
  return () => {
    for (const key of ["claims", "claim-work", "era", "appeals", "charges", "claim-tx"]) qc.invalidateQueries({ queryKey: [key] });
  };
}

function errorToast(toast: (m: string) => void) {
  return (e: unknown) => toast((e as ApiError).message);
}

// ---------- The built claim inside the claim sheet ----------

export function BilledClaimPanel({ claim }: { claim: BilledClaim }) {
  const canStart = useCan("start_task");
  const canReview = useCan("review");
  const toast = useToast();
  const invalidate = useInvalidate();
  const editable = claim.stage && ["draft", "ready", "blocked", "rejected"].includes(claim.stage);
  const [dx, setDx] = useState(claim.diagnoses.join(", "));
  const [lines, setLines] = useState(claim.lines.map((l) => ({ line_id: l.line_id, modifiers: l.modifiers.join(" "), units: l.units, dx: l.dx.join(" ") })));
  const [raw, setRaw] = useState<{ id: string; text: string } | null>(null);
  const tx = useQuery({ queryKey: ["claim-tx", claim.id], queryFn: () => api.get<Transaction[]>(`/claims/${claim.id}/transactions`) });
  const save = useMutation({
    mutationFn: () =>
      api.patch(`/claims/${claim.id}`, {
        diagnoses: dx
          .split(/[,\s]+/)
          .map((d) => d.trim())
          .filter(Boolean),
        lines: lines.map((l) => ({
          line_id: l.line_id,
          modifiers: l.modifiers.split(/[,\s]+/).filter(Boolean),
          units: l.units,
          dx: l.dx
            .split(/[,\s]+/)
            .filter(Boolean)
            .map(Number)
            .filter((n) => n > 0),
        })),
      }),
    onSuccess: () => {
      toast("Saved. Scrub it again before submitting.");
      invalidate();
    },
    onError: errorToast(toast),
  });
  const outcome = useMutation({
    mutationFn: (value: string) => api.patch(`/claims/${claim.id}`, { test_outcome: value }),
    onSuccess: invalidate,
    onError: errorToast(toast),
  });
  const scrub = useMutation({
    mutationFn: () => api.post<BilledClaim>(`/claims/${claim.id}/scrub`),
    onSuccess: (r) => {
      toast(r.scrub?.clean ? "Clean: ready to submit" : "The scrub found problems");
      invalidate();
    },
    onError: errorToast(toast),
  });
  const submit = useMutation({
    mutationFn: () => api.post<BilledClaim & { result: { state: string; message: string | null } }>(`/claims/${claim.id}/submit`),
    onSuccess: (r) => {
      toast(r.result.state === "submitted" ? `${r.claim_number} submitted` : r.result.message ?? r.result.state);
      invalidate();
    },
    onError: errorToast(toast),
  });
  const showRaw = async (id: string) => {
    try {
      setRaw({ id, text: JSON.stringify(await api.get<unknown>(`/claims/${claim.id}/transactions/${id}/raw`), null, 2) });
    } catch (e) {
      toast((e as ApiError).message);
    }
  };
  const findings = claim.scrub?.findings ?? [];
  return (
    <div className="stack" style={{ gap: 18 }}>
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        {claim.stage && <StagePill stage={claim.stage} />}
        {claim.frequency === "replacement" && <span className="chip">Corrected claim</span>}
        {claim.submit_mode === "test" && <span className="chip">Sent in test mode</span>}
        {claim.paid_total && <span className="small muted">Paid {money(claim.paid_total)} · patient owes {money(claim.patient_responsibility)}</span>}
      </div>

      <section className="stack" style={{ gap: 6 }}>
        <h3 className="field-label">Diagnoses (ICD-10-CM, the first is primary)</h3>
        {editable && canStart ? (
          <input className="input mono" value={dx} onChange={(e) => setDx(e.target.value)} placeholder="E11.9, I10" />
        ) : (
          <p className="mono" style={{ margin: 0 }}>
            {claim.diagnoses.join(", ") || "–"}
          </p>
        )}
      </section>

      <section className="stack" style={{ gap: 6 }}>
        <h3 className="field-label">Service lines · place of service {claim.place_of_service}</h3>
        <ul className="list">
          {claim.lines.map((l, i) => (
            <li className="list-row" key={l.line_id} style={{ alignItems: "center", gap: 10 }}>
              <div className="row-main">
                <div className="row-title">
                  <span className="mono">{l.cpt}</span> {l.description ? `· ${l.description}` : ""}
                </div>
                <div className="row-sub">
                  {l.line_id} · {l.date_of_service} · {money(l.charge)}
                </div>
              </div>
              {editable && canStart ? (
                <>
                  <label className="small muted">
                    Mods{" "}
                    <input className="input mono" style={{ width: 80 }} value={lines[i]?.modifiers ?? ""} onChange={(e) => setLines(lines.map((x, j) => (j === i ? { ...x, modifiers: e.target.value } : x)))} />
                  </label>
                  <label className="small muted">
                    Units{" "}
                    <input className="input mono" style={{ width: 52 }} value={lines[i]?.units ?? ""} onChange={(e) => setLines(lines.map((x, j) => (j === i ? { ...x, units: e.target.value } : x)))} />
                  </label>
                  <label className="small muted">
                    Dx{" "}
                    <input className="input mono" style={{ width: 64 }} value={lines[i]?.dx ?? ""} onChange={(e) => setLines(lines.map((x, j) => (j === i ? { ...x, dx: e.target.value } : x)))} />
                  </label>
                </>
              ) : (
                <span className="mono small muted">
                  {l.modifiers.join("-") || "no mods"} · ×{l.units} · dx {l.dx.join(",") || "–"}
                </span>
              )}
            </li>
          ))}
        </ul>
        {editable && canStart && (
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            <button className="btn btn-secondary btn-sm" onClick={() => save.mutate()} disabled={save.isPending}>
              Save changes
            </button>
            <label className="small muted row" style={{ gap: 6 }}>
              Test payer outcome
              <select className="input" style={{ width: 170 }} value={claim.test_outcome ?? "none"} onChange={(e) => outcome.mutate(e.target.value)}>
                <option value="paid">Paid in full</option>
                <option value="partial">Partially paid</option>
                <option value="denied">Denied</option>
                <option value="none">Real payer (acknowledgement only)</option>
              </select>
            </label>
          </div>
        )}
      </section>

      {claim.scrub && (
        <section className="stack" style={{ gap: 6 }}>
          <h3 className="field-label">
            Scrub {claim.scrub.clean ? "· clean" : "· needs fixes"} · checked {timeAgo(claim.scrub.checked_at)}
          </h3>
          {findings.length === 0 ? (
            <p className="small muted" style={{ margin: 0 }}>
              No findings. Clearinghouse validation: {CH_VALIDATION[claim.scrub.clearinghouse] ?? claim.scrub.clearinghouse}.
            </p>
          ) : (
            <ul className="list">
              {findings.map((f, i) => (
                <li className="list-row" key={i}>
                  <div className="row-main">
                    <div className="row-title">{f.message}</div>
                    <div className="row-sub">
                      {f.source === "clearinghouse" ? "Clearinghouse" : "CarePlix"} · {f.code}
                      {f.line_id ? ` · ${f.line_id}` : ""}
                    </div>
                  </div>
                  <Pill tone={f.severity === "error" ? "red" : "orange"}>{f.severity === "error" ? "Error" : "Warning"}</Pill>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {editable && canStart && (
        <div className="row" style={{ gap: 8 }}>
          <button className="btn btn-secondary" onClick={() => scrub.mutate()} disabled={scrub.isPending}>
            <ShieldCheck size={15} aria-hidden /> Scrub
          </button>
          <button className="btn btn-primary" onClick={() => submit.mutate()} disabled={submit.isPending}>
            {submit.isPending ? <span className="spinner" aria-label="Submitting" /> : <Send size={15} aria-hidden />} {claim.stage === "rejected" ? "Resubmit" : "Submit"}
          </button>
        </div>
      )}

      <section className="stack" style={{ gap: 6 }}>
        <h3 className="field-label">Clearinghouse transactions (evidence)</h3>
        {tx.isPending && <ListSkeleton rows={2} icon={false} />}
        {tx.data && tx.data.length === 0 && (
          <p className="small muted" style={{ margin: 0 }}>
            Nothing sent yet.
          </p>
        )}
        {tx.data && tx.data.length > 0 && (
          <ul className="list">
            {tx.data.map((t) => (
              <li className="list-row" key={t.id}>
                <div className="row-main">
                  <div className="row-title">
                    {KIND_LABEL[t.kind]} {t.ok ? "" : "· problem"}
                  </div>
                  <div className="row-sub">
                    {t.mode} · {timeAgo(t.created_at)}
                    {t.transaction_id ? ` · ${t.transaction_id}` : ""}
                    {t.message ? ` · ${t.message}` : ""}
                    {Array.isArray(t.summary.errors) && t.summary.errors.length ? ` · ${(t.summary.errors as string[]).join("; ")}` : ""}
                  </div>
                </div>
                {t.has_raw && canReview && (
                  <button className="btn btn-secondary btn-sm" onClick={() => (raw?.id === t.id ? setRaw(null) : showRaw(t.id))}>
                    {raw?.id === t.id ? "Hide" : "View"}
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
        {raw && (
          <pre className="mono small" style={{ maxHeight: 320, overflow: "auto", background: "var(--fill-2, rgba(127,127,127,.08))", padding: 12, borderRadius: 8, margin: 0 }}>
            {raw.text}
          </pre>
        )}
      </section>
    </div>
  );
}

// ---------- Work queue: rejections and denials ----------

export function WorkQueue({ onOpenClaim }: { onOpenClaim: (id: string) => void }) {
  const canStart = useCan("start_task");
  const toast = useToast();
  const invalidate = useInvalidate();
  const work = useQuery({ queryKey: ["claim-work"], queryFn: () => api.get<WorkItem[]>("/claims/work") });
  const route = useMutation({
    mutationFn: ({ id, value }: { id: string; value: string }) => api.post(`/claims/work/${id}/route`, { route: value }),
    onSuccess: invalidate,
    onError: errorToast(toast),
  });
  const correct = useMutation({
    mutationFn: (id: string) => api.post<{ id: string; claim_number: string }>(`/claims/work/${id}/correct`),
    onSuccess: (r) => {
      toast(`Corrected claim ${r.claim_number} drafted`);
      invalidate();
      onOpenClaim(r.id);
    },
    onError: errorToast(toast),
  });
  const done = useMutation({
    mutationFn: ({ id, dismiss }: { id: string; dismiss: boolean }) => api.post(`/claims/work/${id}/done`, { dismiss }),
    onSuccess: invalidate,
    onError: errorToast(toast),
  });
  return (
    <Card title="Work queue" subtitle="Rejections (from acknowledgements) and denials (from remittances), with what to fix and where each goes next.">
      {work.isPending && <ListSkeleton rows={3} />}
      {work.error && <ErrorState error={work.error} onRetry={() => work.refetch()} />}
      {work.data && work.data.length === 0 && (
        <Empty icon={Inbox} title="Nothing to work">
          Rejected and denied claims land here.
        </Empty>
      )}
      {work.data && work.data.length > 0 && (
        <ul className="list">
          {work.data.map((w) => (
            <li className="list-row" key={w.id} style={{ alignItems: "flex-start", flexWrap: "wrap" }}>
              <div className="row-main" style={{ minWidth: 260 }}>
                <div className="row-title">
                  <button className="link-button" onClick={() => onOpenClaim(w.claim_id)}>
                    {w.title}
                  </button>
                </div>
                <div className="row-sub">
                  {w.payer_name} · {w.category.replace(/_/g, " ")}
                  {w.amount ? ` · ${money(w.amount)}` : ""}
                  {w.due_on ? ` · due ${w.due_on}` : ""}
                </div>
                <div className="small" style={{ marginTop: 4 }}>
                  {w.reasons
                    .slice(0, 4)
                    .map((r) => [r.group ? `${r.group}-${r.code}` : r.code, r.text ?? r.message].filter(Boolean).join(" "))
                    .join(" · ")}
                </div>
                {w.suggestion && <div className="small muted" style={{ marginTop: 4 }}>{w.suggestion}</div>}
                {w.resolution && <div className="small muted">{w.resolution}</div>}
                {w.case_id && (
                  <Link className="small" to={`/cases?case=${w.case_id}`}>
                    <PhoneCall size={12} aria-hidden /> Payer call case
                  </Link>
                )}
              </div>
              <Pill tone={w.kind === "rejection" ? "orange" : "red"}>{w.kind === "rejection" ? "Rejected" : "Denied"}</Pill>
              {canStart && (
                <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                  {w.kind === "denial" && (
                    <select className="input" style={{ width: 190 }} value={w.route} onChange={(e) => route.mutate({ id: w.id, value: e.target.value })}>
                      {Object.entries(ROUTE_LABEL).map(([v, label]) => (
                        <option key={v} value={v}>
                          {label}
                        </option>
                      ))}
                    </select>
                  )}
                  {w.kind === "denial" && w.route === "fix_resubmit" && (
                    <button className="btn btn-secondary btn-sm" onClick={() => correct.mutate(w.id)}>
                      <Wrench size={14} aria-hidden /> Corrected claim
                    </button>
                  )}
                  {w.kind === "rejection" && (
                    <button className="btn btn-secondary btn-sm" onClick={() => onOpenClaim(w.claim_id)}>
                      <Wrench size={14} aria-hidden /> Fix the claim
                    </button>
                  )}
                  <button className="btn btn-secondary btn-sm" onClick={() => done.mutate({ id: w.id, dismiss: false })}>
                    <CheckCircle2 size={14} aria-hidden /> Done
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

// ---------- Remittances (ERA posting proposals) ----------

const POSTING_STATE = {
  proposed: ["Waiting for approval", "orange"],
  posting: ["Posting", "blue"],
  posted: ["Posted", "green"],
  manual: ["Post by hand", "gray"],
  review: ["Denied: see the work queue", "red"],
  needs_review: ["Needs review", "red"],
} as const;

export function Remittances({ onOpenClaim }: { onOpenClaim: (id: string) => void }) {
  const isAdmin = useCan("admin");
  const canStart = useCan("start_task");
  const toast = useToast();
  const invalidate = useInvalidate();
  const era = useQuery({ queryKey: ["era"], queryFn: () => api.get<Posting[]>("/claims/era") });
  const approve = useMutation({
    mutationFn: (id: string) => api.post<Posting>(`/claims/era/${id}/approve`),
    onSuccess: (r) => {
      toast(r.note ?? "Approved");
      invalidate();
    },
    onError: errorToast(toast),
  });
  const posted = useMutation({
    mutationFn: (id: string) => api.post(`/claims/era/${id}/posted`),
    onSuccess: invalidate,
    onError: errorToast(toast),
  });
  return (
    <Card title="Remittances" subtitle="Each payer payment becomes posting proposals for the EHR: the insurance payment and the contractual write-offs. Patient responsibility stays on the patient's balance. Nothing posts without approval.">
      {era.isPending && <ListSkeleton rows={3} />}
      {era.error && <ErrorState error={era.error} onRetry={() => era.refetch()} />}
      {era.data && era.data.length === 0 && (
        <Empty icon={Receipt} title="No remittances yet">
          Payments arrive as 835 remittances after the payer adjudicates a submitted claim.
        </Empty>
      )}
      {era.data && era.data.length > 0 && (
        <ul className="list">
          {era.data.map((p) => {
            const [label, tone] = POSTING_STATE[p.state];
            return (
              <li className="list-row" key={p.id} style={{ alignItems: "flex-start" }}>
                <div className="row-main">
                  <div className="row-title">
                    {p.claim_id ? (
                      <button className="link-button mono" onClick={() => onOpenClaim(p.claim_id!)}>
                        {p.patient_control_number}
                      </button>
                    ) : (
                      <span className="mono">{p.patient_control_number}</span>
                    )}{" "}
                    · {p.payer_name} · paid {money(p.paid)} of {money(p.charge)}
                  </div>
                  <div className="row-sub">
                    {p.adjustments.map((a) => `${a.group}-${a.code} ${money(a.amount)}`).join(" · ") || "No adjustments"}
                    {p.remarks.length ? ` · remarks ${p.remarks.join(", ")}` : ""} · patient {money(p.patient_responsibility)}
                    {p.trace_number ? ` · trace ${p.trace_number}` : ""}
                    {p.paid_on ? ` · ${p.paid_on}` : ""}
                  </div>
                  {p.note && <div className="small muted">{p.note}</div>}
                </div>
                <Pill tone={tone}>{label}</Pill>
                {p.state === "proposed" && isAdmin && (
                  <button className="btn btn-primary btn-sm" onClick={() => approve.mutate(p.id)} disabled={approve.isPending}>
                    Approve and post
                  </button>
                )}
                {(p.state === "manual" || p.state === "needs_review") && canStart && (
                  <button className="btn btn-secondary btn-sm" onClick={() => posted.mutate(p.id)}>
                    Mark posted
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

// ---------- Appeals ----------

export function Appeals() {
  const canStart = useCan("start_task");
  const canReview = useCan("review");
  const toast = useToast();
  const invalidate = useInvalidate();
  const [open, setOpen] = useState<string | null>(null);
  const [text, setText] = useState("");
  const list = useQuery({ queryKey: ["appeals"], queryFn: () => api.get<AppealRow[]>("/claims/appeals") });
  const load = async (id: string) => {
    if (open === id) return setOpen(null);
    try {
      const a = await api.get<{ letter: string }>(`/claims/appeals/${id}`);
      setText(a.letter);
      setOpen(id);
    } catch (e) {
      toast((e as ApiError).message);
    }
  };
  const act = useMutation({
    mutationFn: ({ id, path, body }: { id: string; path: string; body?: unknown }) =>
      path === "letter" ? api.put(`/claims/appeals/${id}/letter`, body) : api.post(`/claims/appeals/${id}/${path}`, body ?? {}),
    onSuccess: () => {
      invalidate();
      toast("Saved");
    },
    onError: errorToast(toast),
  });
  const attach = async (id: string, file: File) => {
    const data = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",", 2)[1] ?? "");
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(file);
    });
    act.mutate({ id, path: "attachments", body: { label: file.name.slice(0, 80), content_type: file.type, data_base64: data } });
  };
  return (
    <Card title="Appeals" subtitle="Drafted from the claim, the remittance and the evidence. Staff edit and approve every letter; fax and portal sending aren't connected yet, so approved appeals are sent by hand.">
      {list.isPending && <ListSkeleton rows={2} />}
      {list.error && <ErrorState error={list.error} onRetry={() => list.refetch()} />}
      {list.data && list.data.length === 0 && (
        <Empty icon={FileText} title="No appeals">
          Denials routed to an appeal get a drafted letter here.
        </Empty>
      )}
      {list.data && list.data.length > 0 && (
        <ul className="list">
          {list.data.map((a) => (
            <li key={a.id} className="list-row" style={{ flexDirection: "column", alignItems: "stretch" }}>
              <div className="row" style={{ gap: 10 }}>
                <div className="row-main">
                  <div className="row-title">
                    <span className="mono">{a.claim_number}</span> · {a.payer_name} · {a.level.replace("_", " ")}
                  </div>
                  <div className="row-sub">
                    Due {a.due_on}
                    {a.days_left !== null ? ` (${a.days_left < 0 ? `${-a.days_left} days late` : `${a.days_left} days left`})` : ""} · evidence:{" "}
                    {a.evidence.map((e) => `${e.label}${e.have ? " ✓" : " (missing)"}`).join("; ")}
                    {a.attachments.length ? ` · ${a.attachments.length} attachment(s)` : ""}
                  </div>
                  {a.send_note && <div className="small muted">{a.send_note}</div>}
                </div>
                {a.overdue && <Pill tone="red">Overdue</Pill>}
                <Pill tone={a.state === "draft" ? "orange" : a.state === "sent" || a.state === "decided" ? "green" : "blue"}>{a.state.replace(/_/g, " ")}</Pill>
                {canReview && (
                  <button className="btn btn-secondary btn-sm" onClick={() => load(a.id)}>
                    {open === a.id ? "Close" : "Letter"}
                  </button>
                )}
              </div>
              {open === a.id && (
                <div className="stack" style={{ gap: 8, marginTop: 10 }}>
                  <textarea className="input mono small" rows={16} value={text} onChange={(e) => setText(e.target.value)} disabled={!["draft", "approved"].includes(a.state)} />
                  <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                    {a.state === "draft" && (
                      <>
                        <button className="btn btn-secondary btn-sm" onClick={() => act.mutate({ id: a.id, path: "letter", body: { text } })}>
                          Save letter
                        </button>
                        <button className="btn btn-primary btn-sm" onClick={() => act.mutate({ id: a.id, path: "approve", body: { channel: "fax" } })}>
                          Approve
                        </button>
                      </>
                    )}
                    {canStart && (a.state === "approved" || a.state === "ready_to_send") && (
                      <button className="btn btn-primary btn-sm" onClick={() => act.mutate({ id: a.id, path: "sent", body: { note: "Sent by hand" } })}>
                        Mark sent
                      </button>
                    )}
                    {canStart && a.state === "sent" && (
                      <>
                        <button className="btn btn-secondary btn-sm" onClick={() => act.mutate({ id: a.id, path: "decided", body: { outcome: "overturned" } })}>
                          Overturned
                        </button>
                        <button className="btn btn-secondary btn-sm" onClick={() => act.mutate({ id: a.id, path: "decided", body: { outcome: "upheld" } })}>
                          Upheld
                        </button>
                      </>
                    )}
                    {canStart && (
                      <label className="btn btn-secondary btn-sm">
                        <Paperclip size={14} aria-hidden /> Attach records (275)
                        <input type="file" accept="application/pdf,image/png,image/jpeg,image/tiff" hidden onChange={(e) => e.target.files?.[0] && attach(a.id, e.target.files[0])} />
                      </label>
                    )}
                  </div>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

// ---------- Charges ----------

interface Visit {
  key: string;
  label: string;
}

export function Charges() {
  const canStart = useCan("start_task");
  const toast = useToast();
  const invalidate = useInvalidate();
  const charges = useQuery({ queryKey: ["charges"], queryFn: () => api.get<Charge[]>("/claims/charges") });
  const visits = useQuery({ queryKey: ["charge-visits"], queryFn: () => api.get<Visit[]>("/claims/charges/visits") });
  const [pick, setPick] = useState<string>("");
  const add = useMutation({
    mutationFn: () => api.post<Charge[]>("/claims/charges/samples", { visits: pick ? [pick] : null }),
    onSuccess: (r) => {
      toast(`${r.length} synthetic charges added`);
      invalidate();
    },
    onError: errorToast(toast),
  });
  const pull = useMutation({
    mutationFn: () => api.post<{ name: string; ok: boolean; added: number; error: string | null }[]>("/claims/charges/pull"),
    onSuccess: (r) => {
      toast(r.map((x) => (x.ok ? `${x.name}: ${x.added} new charges` : `${x.name}: ${x.error}`)).join(" · "));
      invalidate();
    },
    onError: errorToast(toast),
  });
  const build = useMutation({
    mutationFn: () => api.post<unknown[]>("/claims/build", {}),
    onSuccess: (r) => {
      toast(`${r.length} draft claim${r.length === 1 ? "" : "s"} built`);
      invalidate();
    },
    onError: errorToast(toast),
  });
  const unbilled = (charges.data ?? []).filter((c) => !c.claim_id).length;
  return (
    <Card
      title="Charges"
      subtitle="Billed services from the EHR (or synthetic visits). Unbilled charges become draft claims, one per patient, provider, payer and date of service."
      actions={
        canStart && (
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            <select className="input" style={{ width: 260 }} value={pick} onChange={(e) => setPick(e.target.value)}>
              <option value="">All sample visits</option>
              {(visits.data ?? []).map((v) => (
                <option key={v.key} value={v.key}>
                  {v.label}
                </option>
              ))}
            </select>
            <button className="btn btn-secondary" onClick={() => add.mutate()} disabled={add.isPending}>
              Add synthetic charges
            </button>
            <button className="btn btn-secondary" onClick={() => pull.mutate()} disabled={pull.isPending}>
              Read EHR charges
            </button>
            <button className="btn btn-primary" onClick={() => build.mutate()} disabled={build.isPending || unbilled === 0}>
              Build claims ({unbilled})
            </button>
          </div>
        )
      }
    >
      {charges.isPending && <ListSkeleton rows={3} />}
      {charges.error && <ErrorState error={charges.error} onRetry={() => charges.refetch()} />}
      {charges.data && charges.data.length === 0 && (
        <Empty icon={Receipt} title="No charges yet">
          Add synthetic visits, or read the practice's charges from the EHR.
        </Empty>
      )}
      {charges.data && charges.data.length > 0 && (
        <ul className="list">
          {charges.data.map((c) => (
            <li className="list-row" key={c.id}>
              <div className="row-main">
                <div className="row-title">
                  <span className="mono">{c.cpt_code}</span>
                  {c.modifiers.length ? <span className="mono">-{c.modifiers.join("-")}</span> : null} · {c.description ?? "Service"} · {money(c.amount)}
                </div>
                <div className="row-sub">
                  {c.date_of_service} · {c.payer_name ?? "no payer"} · dx {c.diagnoses.join(", ") || "none"}
                </div>
              </div>
              <span className="chip">{c.source === "ehr" ? "EHR" : "Synthetic"}</span>
              <Pill tone={c.claim_id ? "green" : "gray"}>{c.claim_id ? "On a claim" : "Unbilled"}</Pill>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

// ---------- Billing profile ----------

const PROFILE_FIELDS: { key: keyof Profile; label: string }[] = [
  { key: "organization", label: "Billing provider" },
  { key: "npi", label: "Billing NPI" },
  { key: "tax_id", label: "Tax ID (EIN)" },
  { key: "taxonomy", label: "Taxonomy" },
  { key: "address1", label: "Address" },
  { key: "city", label: "City" },
  { key: "state", label: "State" },
  { key: "postal_code", label: "ZIP" },
  { key: "submitter_name", label: "Submitter" },
  { key: "contact_phone", label: "Billing phone" },
];

export function BillingProfileCard() {
  const isAdmin = useCan("admin");
  const toast = useToast();
  const qc = useQueryClient();
  const profile = useQuery({ queryKey: ["billing-profile"], queryFn: () => api.get<Profile>("/clearinghouse/billing-profile") });
  const [draft, setDraft] = useState<Partial<Profile>>({});
  const save = useMutation({
    mutationFn: () => api.put<Profile>("/clearinghouse/billing-profile", draft),
    onSuccess: () => {
      toast("Billing profile saved");
      setDraft({});
      qc.invalidateQueries({ queryKey: ["billing-profile"] });
    },
    onError: errorToast(toast),
  });
  const p = profile.data;
  return (
    <Card title="Billing profile" subtitle="On every claim. Test claims that should come back with a remittance use the NPI and tax ID enrolled with the clearinghouse's test payer.">
      {profile.isPending && <ListSkeleton rows={2} icon={false} />}
      {profile.error && <ErrorState error={profile.error} />}
      {p && (
        <div className="stack" style={{ gap: 10 }}>
          <div className="grid-3">
            {PROFILE_FIELDS.map((f) => (
              <label key={f.key} className="stack" style={{ gap: 4 }}>
                <span className="field-label">{f.label}</span>
                <input className="input mono" disabled={!isAdmin} value={draft[f.key] ?? p[f.key] ?? ""} onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })} />
              </label>
            ))}
          </div>
          {isAdmin && Object.keys(draft).length > 0 && (
            <div>
              <button className="btn btn-primary btn-sm" onClick={() => save.mutate()} disabled={save.isPending}>
                Save
              </button>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

export function TestModeBanner() {
  return (
    <Banner tone="blue" title="Clearinghouse test mode." icon={AlertCircle}>
      {" "}
      Only synthetic claims are sent. Claims with a test payer outcome go to the clearinghouse's test payer, which acknowledges them and sends a test remittance (paid, partially paid or denied); others get an acknowledgement only. Test mode has no 276 claim status.
    </Banner>
  );
}

// ---------- Apply one clearinghouse report by transaction ID ----------

export function ReportFetch() {
  const canStart = useCan("start_task");
  const toast = useToast();
  const invalidate = useInvalidate();
  const [tx, setTx] = useState("");
  const [kind, setKind] = useState<"277" | "835">("277");
  const fetchOne = useMutation({
    mutationFn: () => api.post<{ claims: number; unmatched: number }>("/claims/clearinghouse/fetch", { transaction_id: tx.trim(), kind }),
    onSuccess: (r) => {
      toast(`${r.claims} claim update${r.claims === 1 ? "" : "s"}${r.unmatched ? ` · ${r.unmatched} not ours` : ""}`);
      setTx("");
      invalidate();
    },
    onError: errorToast(toast),
  });
  if (!canStart) return null;
  return (
    <Card title="Clearinghouse responses" subtitle="Acknowledgements and remittances arrive by webhook (or Check for responses). To apply one by hand, paste its transaction ID.">
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        <input className="input mono" style={{ maxWidth: 320 }} placeholder="Transaction ID" value={tx} onChange={(e) => setTx(e.target.value)} />
        <select className="input" style={{ width: 200 }} value={kind} onChange={(e) => setKind(e.target.value as "277" | "835")}>
          <option value="277">Acknowledgement (277CA)</option>
          <option value="835">Remittance (835)</option>
        </select>
        <button className="btn btn-secondary" onClick={() => fetchOne.mutate()} disabled={!/^[A-Za-z0-9_-]+$/.test(tx.trim()) || fetchOne.isPending}>
          Apply
        </button>
      </div>
    </Card>
  );
}
