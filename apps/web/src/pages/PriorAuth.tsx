import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, ClipboardList, FileCheck, FileSearch, FlaskConical, Hourglass, Paperclip, Play, UserRoundCheck } from "lucide-react";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api";
import { useCan } from "../App";
import { Banner, Card, Empty, ErrorState, ListSkeleton, Pill, Segmented, Sheet, Toolbar, timeAgo, useToast } from "../ui";

// ---------- Types (mirror apps/api priorauth/api.py) ----------

type CaseStatus = "open" | "in_progress" | "done" | "handed_off" | "blocked" | "failed" | "closed";
type AuthStatus = "approved" | "denied" | "pending_review" | "pending_additional_information" | "not_found" | "unknown";

interface AuthRow {
  id: string;
  case_ref: string;
  status: CaseStatus;
  status_reason: string | null;
  patient: string | null;
  payer: string | null;
  service: string | null;
  cpt_code: string | null;
  visit_at: string | null;
  auth_status: AuthStatus | null;
  auth_number: string | null;
  valid_from: string | null;
  valid_to: string | null;
  route: "pas" | "portal" | "call" | "fax" | null;
  route_label: string | null;
  checks: number;
  follow_up_calls: number;
  missing: string[];
  packet_checked: boolean;
  due_at: string | null;
  overdue: boolean;
  next_step: string | null;
  owner: string | null;
  source: string | null;
  source_label: string | null;
  trigger: { eligibility_check_id: string | null; services: string[]; rule_id: string | null } | null;
  playbook_state: string | null;
  created_at: string | null;
  synthetic: boolean;
}

interface WorkList {
  items: AuthRow[];
  counts: { total: number; missing_items: number; waiting_on_payer: number; handed_off: number; overdue: number };
  trigger_on: boolean;
}

interface PacketItem {
  key: string;
  label: string;
  kind: "fact" | "document";
  required: boolean;
  present: boolean;
  source: "case" | "ehr" | "staff" | null;
  ref?: string | null;
  document_label?: string | null;
}

interface Rule {
  id: string;
  payer: string;
  appointment_type_ref: string | null;
  appointment_type_name: string | null;
  cpt_code: string;
  service_description: string;
  icd_codes: string[];
  lead_days: number;
  enabled: boolean;
  note: string | null;
}

type Tone = "green" | "orange" | "red" | "blue" | "purple" | "gray";

const AUTH: Record<AuthStatus, { tone: Tone; label: string }> = {
  approved: { tone: "green", label: "Approved" },
  denied: { tone: "red", label: "Denied" },
  pending_review: { tone: "blue", label: "Pended" },
  pending_additional_information: { tone: "orange", label: "Payer wants more" },
  not_found: { tone: "orange", label: "Not on file" },
  unknown: { tone: "gray", label: "Unknown" },
};

const CASE: Record<CaseStatus, { tone: Tone; label: string }> = {
  open: { tone: "gray", label: "Open" },
  in_progress: { tone: "blue", label: "Working" },
  done: { tone: "green", label: "Done" },
  handed_off: { tone: "orange", label: "Needs a person" },
  blocked: { tone: "orange", label: "Packet incomplete" },
  failed: { tone: "red", label: "Failed" },
  closed: { tone: "gray", label: "Closed" },
};

function AuthPill({ row }: { row: AuthRow }) {
  if (!row.auth_status) return <Pill tone="gray">Not submitted</Pill>;
  const a = AUTH[row.auth_status] ?? AUTH.unknown;
  return <Pill tone={a.tone}>{a.label}</Pill>;
}

/** A label mid-sentence: "Diagnosis codes (ICD-10)" → "diagnosis codes (ICD-10)"; "CPT code" stays. */
const inSentence = (label: string) => (/^[A-Z]{2}/.test(label) ? label : label.charAt(0).toLowerCase() + label.slice(1));

const day = (iso: string | null) => (iso ? new Date(iso).toLocaleDateString() : "–");

export default function PriorAuth() {
  const canAdmin = useCan("admin");
  const qc = useQueryClient();
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const openId = params.get("case");
  const [view, setView] = useState<"open" | "done" | "all">("open");
  const list = useQuery({ queryKey: ["prior-auth", view], queryFn: () => api.get<WorkList>(`/prior-auth?view=${view}`) });
  const rules = useQuery({ queryKey: ["prior-auth-rules"], queryFn: () => api.get<Rule[]>("/prior-auth-rules") });
  const samples = useMutation({
    mutationFn: () => api.post<Rule[]>("/prior-auth-rules/samples", {}),
    onSuccess: (r) => {
      toast(r.length ? `${r.length} synthetic payer rule${r.length === 1 ? "" : "s"} added` : "The sample rules are already there");
      qc.invalidateQueries({ queryKey: ["prior-auth-rules"] });
    },
    onError: (e) => toast((e as ApiError).message),
  });
  const rows = list.data?.items ?? [];
  const counts = list.data?.counts;

  return (
    <>
      <Toolbar
        eyebrow="Back office"
        title="Prior authorizations"
        description="Visits whose eligibility check or a payer rule says they need prior auth open a case on their own. The playbook builds the packet from the EHR, submits it (the payer's API first, then a call), chases the decision and writes the auth number back. Anything it can't finish lands here and in the review inbox."
        actions={
          canAdmin && (
            <button className="btn btn-secondary" onClick={() => samples.mutate()} disabled={samples.isPending}>
              <FlaskConical size={15} aria-hidden /> Add sample payer rules
            </button>
          )
        }
      />
      <div className="content">
        {list.data && !list.data.trigger_on && (
          <Banner tone="gray" title="Automatic detection is off." icon={AlertCircle}>
            {" "}
            The <span className="mono">auth_required</span> playbook trigger isn't enabled in this environment, so new visits don't open cases on their own.
          </Banner>
        )}

        <div className="grid-3" style={{ gridTemplateColumns: "repeat(4, minmax(0, 1fr))" }}>
          <Stat icon={ClipboardList} tint="var(--tile-blue)" value={counts?.total} label={view === "open" ? "Open" : "Shown"} />
          <Stat icon={Paperclip} tint="var(--tile-orange)" value={counts?.missing_items} label="Missing packet items" />
          <Stat icon={Hourglass} tint="var(--tile-purple)" value={counts?.waiting_on_payer} label="Waiting on the payer" />
          <Stat icon={UserRoundCheck} tint="var(--tile-green)" value={counts?.handed_off} label="Need a person" />
        </div>

        <Card
          title="Work list"
          subtitle="Overdue first, then by due date. Select one for its packet and the payer's answer."
          actions={<Segmented label="Show" value={view} onChange={setView} options={[{ value: "open", label: "Open" }, { value: "done", label: "Finished" }, { value: "all", label: "All" }]} />}
        >
          {list.isPending && <ListSkeleton rows={4} />}
          {list.error && <ErrorState error={list.error} onRetry={() => list.refetch()} title="Couldn't load prior auths" />}
          {list.data && rows.length === 0 && (
            <Empty icon={FileSearch} title={view === "open" ? "Nothing waiting" : "No prior auths here"}>
              Cases open when a visit's eligibility check flags prior auth, or when a payer rule names its appointment type. Add the sample payer rules to see them on the synthetic schedule.
            </Empty>
          )}
          {rows.length > 0 && (
            <ul className="list">
              {rows.map((r) => (
                <li key={r.id}>
                  <button className="list-row" style={{ width: "100%", textAlign: "left" }} onClick={() => setParams({ case: r.id }, { replace: true })}>
                    <div className="row-main">
                      <div className="row-title">
                        <span className="mono">{r.case_ref}</span> · {r.payer ?? "Payer unknown"}
                        {r.cpt_code ? <span className="mono small muted"> · CPT {r.cpt_code}</span> : null}
                      </div>
                      <div className="row-sub">
                        {r.service ?? "Service not named"}
                        {r.visit_at ? ` · visit ${r.visit_at}` : ""}
                        {` · due ${day(r.due_at)}`}
                        {r.route_label ? ` · ${r.route_label}` : ""}
                        {r.auth_number ? ` · ${r.auth_number}` : ""}
                      </div>
                      {r.missing.length > 0 && <div className="row-sub" style={{ color: "var(--orange, #b45309)" }}>Missing: {r.missing.join(", ")}</div>}
                      {r.missing.length === 0 && r.next_step && ["handed_off", "blocked"].includes(r.status) && <div className="row-sub">Next: {r.next_step}</div>}
                    </div>
                    {r.synthetic && <span className="sample-label">Synthetic</span>}
                    {r.overdue && <Pill tone="red">Overdue</Pill>}
                    <Pill tone={CASE[r.status].tone}>{CASE[r.status].label}</Pill>
                    <AuthPill row={r} />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="Payer rules" subtitle="Which appointment types need prior auth with which payer. A 271 flag opens a case without a rule; a rule also names the CPT and diagnosis codes.">
          {rules.isPending && <ListSkeleton rows={2} icon={false} />}
          {rules.error && <ErrorState error={rules.error} onRetry={() => rules.refetch()} title="Couldn't load payer rules" />}
          {rules.data && rules.data.length === 0 && (
            <p className="small muted" style={{ margin: 0 }}>
              No payer rules yet.
            </p>
          )}
          {rules.data && rules.data.length > 0 && (
            <ul className="list">
              {rules.data.map((rule) => (
                <li className="list-row" key={rule.id}>
                  <div className="row-main">
                    <div className="row-title">
                      {rule.service_description} <span className="mono small muted">CPT {rule.cpt_code}</span>
                    </div>
                    <div className="row-sub">
                      {rule.appointment_type_name ?? `Appointment type ${rule.appointment_type_ref}`} · {rule.payer === "*" ? "every payer" : rule.payer} · starts {rule.lead_days} days before the visit
                      {rule.icd_codes.length ? ` · ICD-10 ${rule.icd_codes.join(", ")}` : ""}
                    </div>
                  </div>
                  {!rule.enabled && <Pill tone="gray">Off</Pill>}
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
      {openId && <AuthSheet id={openId} row={rows.find((r) => r.id === openId)} onClose={() => setParams({}, { replace: true })} />}
    </>
  );
}

function AuthSheet({ id, row, onClose }: { id: string; row: AuthRow | undefined; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const canStart = useCan("start_task");
  const packet = useQuery({ queryKey: ["prior-auth", "packet", id], queryFn: () => api.get<{ items: PacketItem[]; missing: PacketItem[] }>(`/prior-auth/${id}/packet`) });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["prior-auth"] });
    qc.invalidateQueries({ queryKey: ["cases"] });
    qc.invalidateQueries({ queryKey: ["inbox"] });
  };
  const resume = useMutation({
    mutationFn: () => api.post(`/cases/${id}/playbook/resume`),
    onSuccess: () => {
      toast("Playbook resumed");
      refresh();
    },
    onError: (e) => toast((e as ApiError).message),
  });
  const r = row;
  const stuck = r && (r.status === "handed_off" || r.status === "blocked");
  return (
    <Sheet
      wide
      title={r ? `${r.case_ref} · ${r.payer ?? "Payer"}` : "Prior authorization"}
      subtitle={r ? `${r.service ?? "Service"}${r.cpt_code ? ` · CPT ${r.cpt_code}` : ""}${r.visit_at ? ` · visit ${r.visit_at}` : ""}` : undefined}
      onClose={onClose}
      footer={
        <>
          <Link className="btn btn-secondary" to={`/cases?case=${id}`}>
            Open the case
          </Link>
          <span className="spacer" />
          {canStart && stuck && (
            <button className="btn btn-primary" onClick={() => resume.mutate()} disabled={resume.isPending}>
              {resume.isPending ? <span className="spinner" aria-label="Resuming" /> : <Play size={15} aria-hidden />} Resume the playbook
            </button>
          )}
        </>
      }
    >
      <div className="stack" style={{ gap: 22 }}>
        {r && (
          <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
            <Pill tone={CASE[r.status].tone}>{CASE[r.status].label}</Pill>
            <AuthPill row={r} />
            {r.synthetic && <span className="sample-label">Synthetic</span>}
            <span className="small muted">
              {r.source_label ?? "Case"} · opened {timeAgo(r.created_at)}
              {r.owner ? ` · owner ${r.owner}` : ""}
            </span>
          </div>
        )}

        {r && stuck && r.status_reason && (
          <Banner tone="orange" title={r.status === "blocked" ? "The packet isn't complete." : "Handed to a person."} icon={AlertCircle}>
            {" "}
            {r.status_reason} {r.next_step && r.next_step !== r.status_reason ? `Next: ${r.next_step}` : ""}
          </Banner>
        )}

        {r && (
          <section className="stack" style={{ gap: 6 }}>
            <h3 className="field-label">The payer's answer</h3>
            <p style={{ margin: 0 }}>
              {r.auth_status === "approved"
                ? `Approved${r.auth_number ? `, auth ${r.auth_number}` : ""}${r.valid_from ? `, valid ${r.valid_from} to ${r.valid_to ?? "?"}` : ""}.`
                : r.auth_status
                  ? `${(AUTH[r.auth_status] ?? AUTH.unknown).label}${r.auth_number ? ` (ref ${r.auth_number})` : ""}.`
                  : "Not submitted yet."}
              {r.route_label ? ` Route: ${r.route_label}.` : ""}
              {r.checks ? ` ${r.checks} electronic check${r.checks === 1 ? "" : "s"}.` : ""}
              {r.follow_up_calls ? ` ${r.follow_up_calls} follow-up call${r.follow_up_calls === 1 ? "" : "s"}.` : ""}
            </p>
            {r.trigger && (r.trigger.services.length > 0 || r.trigger.rule_id) && (
              <p className="small muted" style={{ margin: 0 }}>
                Opened because {r.trigger.services.length ? `the eligibility check flagged ${r.trigger.services.join(", ")}` : "a payer rule names this visit's appointment type"}.
              </p>
            )}
          </section>
        )}

        <section className="stack" style={{ gap: 6 }}>
          <h3 className="field-label">Packet</h3>
          {packet.isPending && <ListSkeleton rows={4} icon={false} />}
          {packet.error && <ErrorState error={packet.error} onRetry={() => packet.refetch()} title="Couldn't load the packet" />}
          {packet.data && (
            <ul className="list">
              {packet.data.items.map((item) => (
                <PacketRow key={item.key} caseId={id} item={item} canAttach={canStart} onDone={refresh} />
              ))}
            </ul>
          )}
          <p className="small muted" style={{ margin: 0 }}>
            Missing facts are filled on the case. Documents come from the EHR when it syncs them; until then attach each by its EHR document id. A complete packet resumes the playbook.
          </p>
        </section>
      </div>
    </Sheet>
  );
}

function PacketRow({ caseId, item, canAttach, onDone }: { caseId: string; item: PacketItem; canAttach: boolean; onDone: () => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [ref, setRef] = useState("");
  const [label, setLabel] = useState("");
  const attach = useMutation({
    mutationFn: () => api.post<{ missing: PacketItem[]; resumed: boolean }>(`/prior-auth/${caseId}/packet`, { item: item.key, ref, label: label || null }),
    onSuccess: (res) => {
      toast(res.resumed ? "Packet complete: the playbook is submitting it" : res.missing.length ? `Attached. Still missing: ${res.missing.map((m) => inSentence(m.label)).join(", ")}` : "Attached");
      setRef("");
      setLabel("");
      qc.invalidateQueries({ queryKey: ["prior-auth", "packet", caseId] });
      onDone();
    },
    onError: (e) => toast((e as ApiError).message),
  });
  const where = { case: "On the case", ehr: "From the EHR", staff: "Attached by staff" } as const;
  return (
    <li className="list-row" style={{ alignItems: "flex-start", flexWrap: "wrap" }}>
      <div className="row-main">
        <div className="row-title">
          {item.label}
          {!item.required && <span className="small muted"> · optional</span>}
        </div>
        <div className="row-sub">
          {item.present ? `${item.source ? where[item.source] : "Present"}${item.ref ? ` · ${item.ref}` : ""}${item.document_label && item.source === "staff" ? ` · ${item.document_label}` : ""}` : item.kind === "fact" ? "Fill this in on the case" : "Not in the EHR mirror"}
        </div>
      </div>
      {item.present ? <Pill tone="green" icon={FileCheck}>Present</Pill> : <Pill tone={item.required ? "orange" : "gray"}>{item.required ? "Missing" : "None"}</Pill>}
      {!item.present && item.kind === "document" && canAttach && (
        <form
          className="row"
          style={{ gap: 6, width: "100%", marginTop: 8 }}
          onSubmit={(e) => {
            e.preventDefault();
            if (ref.trim()) attach.mutate();
          }}
        >
          <input className="input" placeholder="EHR document id" value={ref} onChange={(e) => setRef(e.target.value)} aria-label={`${item.label}: EHR document id`} style={{ maxWidth: 180 }} />
          <input className="input" placeholder="Title (optional)" value={label} onChange={(e) => setLabel(e.target.value)} aria-label={`${item.label}: title`} />
          <button className="btn btn-secondary" type="submit" disabled={!ref.trim() || attach.isPending}>
            <Paperclip size={14} aria-hidden /> Attach
          </button>
        </form>
      )}
    </li>
  );
}

function Stat({ icon: Icon, tint, value, label }: { icon: typeof ClipboardList; tint: string; value: number | undefined; label: string }) {
  return (
    <div className="card stat row" style={{ gap: 14 }}>
      <div className="row-icon" style={{ background: tint, width: 40, height: 40 }}>
        <Icon size={20} aria-hidden />
      </div>
      <div>
        <div className="stat-value">{value ?? <span className="skeleton" style={{ display: "inline-block", width: 40, height: 26 }} />}</div>
        <div className="stat-label">{label}</div>
      </div>
    </div>
  );
}
