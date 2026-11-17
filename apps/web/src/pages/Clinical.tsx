// Clinical support: refills, discharge follow-up, pre-visit and the approved instructions library.
// The agent never makes a clinical decision: these pages are where a clinician decides, signs off
// and approves the exact words a patient hears.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, BookCheck, CheckCircle2, CircleDashed, ClipboardList, Copy, FileUp, Plus, ShieldAlert, Stethoscope } from "lucide-react";
import { useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import {
  api,
  ApiError,
  type AnswerLine,
  type ChartBrief,
  type ClinicalDetail,
  type ClinicalList,
  type ClinicalRow,
  type ClinicalStage,
  type ClinicalType,
  type Instruction,
  type InstructionList,
  type PlaybookRun,
  type UpcomingVisit,
} from "../api";
import { useCan } from "../App";
import { Banner, Card, Empty, ErrorState, ListSkeleton, Pill, Segmented, Sheet, StatTile, Toolbar, timeAgo, useToast } from "../ui";

type Tone = "green" | "orange" | "red" | "blue" | "purple" | "gray";

const STAGE: Record<ClinicalStage, [Tone, string]> = {
  new: ["gray", "New"],
  preparing: ["blue", "Preparing"],
  waiting_on_patient: ["blue", "Waiting on patient"],
  with_clinician: ["orange", "With the clinician"],
  red_flag: ["red", "Red flag"],
  decided: ["blue", "Decided"],
  told: ["green", "Patient told"],
  reviewed: ["green", "Reviewed"],
  handed_off: ["orange", "With staff"],
  blocked: ["orange", "Blocked"],
  done: ["green", "Done"],
  failed: ["red", "Failed"],
  closed: ["gray", "Closed"],
};

const DECISION_LABEL: Record<string, string> = {
  approved: "Approve",
  needs_visit: "Visit needed first",
  denied: "Don't renew",
  other: "Other (a person tells the patient)",
};

const PATIENT_FIELDS: { key: string; label: string; type?: string; placeholder?: string }[] = [
  { key: "patient_name", label: "Patient (synthetic)", placeholder: "Synthetic Patient Alpha" },
  { key: "date_of_birth", label: "Date of birth", type: "date" },
  { key: "patient_phone", label: "Mobile phone", type: "tel", placeholder: "+1 202 555 0142" },
];

const useErr = () => {
  const toast = useToast();
  return (e: unknown) => toast(e instanceof ApiError ? e.message : "Something went wrong");
};

function StagePill({ stage }: { stage: ClinicalStage }) {
  const [tone, label] = STAGE[stage] ?? ["gray", stage];
  return (
    <Pill tone={tone} icon={stage === "red_flag" ? ShieldAlert : stage === "done" || stage === "told" || stage === "reviewed" ? CheckCircle2 : CircleDashed}>
      {label}
    </Pill>
  );
}

function Steps({ run }: { run: PlaybookRun | null }) {
  if (!run) return null;
  return (
    <ol className="small" style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 2 }}>
      {run.steps.map((s, i) => (
        <li key={i} className={s.state === "todo" ? "muted" : undefined} style={{ fontWeight: s.state === "current" ? 600 : undefined }}>
          {s.label}
          {s.state === "current" && run.state === "waiting" ? " · waiting" : ""}
        </li>
      ))}
    </ol>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="stack" style={{ gap: 6 }}>
      <h3 className="field-label" style={{ margin: 0 }}>
        {title}
      </h3>
      {children}
    </section>
  );
}

function Answers({ lines, empty }: { lines: AnswerLine[]; empty: string }) {
  if (!lines.length) return <p className="small muted" style={{ margin: 0 }}>{empty}</p>;
  return (
    <ul className="list" style={{ margin: 0 }}>
      {lines.map((a) => (
        <li key={a.question} className="list-row" style={{ alignItems: "flex-start", padding: "8px 0" }}>
          <div className="row-main stack" style={{ gap: 2 }}>
            <span className="small" style={{ fontWeight: 600 }}>
              {a.label}
            </span>
            <span className="small">
              {a.answer === "text" ? `“${a.detail}”` : a.answer}
              {a.answer !== "text" && a.detail ? ` · “${a.detail}”` : ""}
              <span className="muted"> · {a.source}</span>
            </span>
          </div>
          {a.level === "red_flag" && (
            <Pill tone="red" icon={ShieldAlert}>
              Red flag
            </Pill>
          )}
          {a.level === "attention" && <Pill tone="orange">Look at</Pill>}
        </li>
      ))}
    </ul>
  );
}

// --- the list pages -------------------------------------------------------------------------------

function ClinicalPage({
  type,
  title,
  description,
  banner,
  actions,
  meta,
  extra,
}: {
  type: ClinicalType;
  title: string;
  description: string;
  banner: ReactNode;
  actions?: ReactNode;
  meta: (r: ClinicalRow) => ReactNode;
  extra?: ReactNode;
}) {
  const [params, setParams] = useSearchParams();
  const [status, setStatus] = useState<"open" | "closed" | "all">("open");
  const list = useQuery({ queryKey: ["clinical", type, status], queryFn: () => api.get<ClinicalList>(`/clinical/cases?type=${type}&status=${status}`), refetchInterval: 8000 });
  const open = params.get("case");
  const d = list.data;
  const count = (...s: ClinicalStage[]) => s.reduce((n, k) => n + (d?.counts[k] ?? 0), 0);
  return (
    <>
      <Toolbar eyebrow="Clinical" title={title} description={description} actions={actions} />
      <div className="content">
        {banner}
        {extra}
        {d && status === "open" && (
          <div className="grid-3" style={{ gridTemplateColumns: "repeat(4, minmax(0, 1fr))" }}>
            <StatTile value={count("red_flag")} label="Red flags" tint="var(--tile-red)" />
            <StatTile value={count("with_clinician")} label="With the clinician" />
            <StatTile value={count("waiting_on_patient", "preparing", "new", "decided")} label="In progress" />
            <StatTile value={count("handed_off", "blocked")} label="With staff" />
          </div>
        )}
        <Card
          title="Cases"
          actions={
            <Segmented
              label="Show"
              value={status}
              onChange={setStatus}
              options={[
                { value: "open", label: "Open" },
                { value: "closed", label: "Closed" },
                { value: "all", label: "All" },
              ]}
            />
          }
        >
          {list.isPending && <ListSkeleton rows={3} />}
          {list.error && <ErrorState error={list.error} onRetry={() => list.refetch()} />}
          {d && d.cases.length === 0 && <Empty icon={ClipboardList} title="Nothing here" />}
          {d && d.cases.length > 0 && (
            <ul className="list">
              {d.cases.map((r) => (
                <li key={r.id} className="list-row" style={{ cursor: "pointer" }} onClick={() => setParams({ case: r.id })}>
                  <div className="row-main stack" style={{ gap: 2 }}>
                    <span className="row-title">
                      {r.case_ref} · {r.subject ?? "Patient"}
                    </span>
                    <span className="small muted">{meta(r)}</span>
                  </div>
                  <StagePill stage={r.stage} />
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
      {open && <ClinicalSheet id={open} onClose={() => setParams({})} />}
    </>
  );
}

function NewCaseSheet({ type, title, subtitle, fields, onClose }: { type: ClinicalType; title: string; subtitle: string; fields: { key: string; label: string; type?: string; placeholder?: string }[]; onClose: () => void }) {
  const qc = useQueryClient();
  const [, setParams] = useSearchParams();
  const [values, setValues] = useState<Record<string, string>>({});
  const create = useMutation({
    mutationFn: () => api.post<ClinicalDetail>("/clinical/cases", { case_type: type, fields: Object.fromEntries(Object.entries(values).filter(([, v]) => v.trim())) }),
    onSuccess: (d) => {
      qc.invalidateQueries({ queryKey: ["clinical"] });
      onClose();
      setParams({ case: d.id });
    },
  });
  return (
    <Sheet
      title={title}
      subtitle={subtitle}
      onClose={onClose}
      footer={
        <>
          <span className="spacer" />
          <button className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" disabled={create.isPending} onClick={() => create.mutate()}>
            Start
          </button>
        </>
      }
    >
      <div className="stack" style={{ gap: 14 }}>
        <Banner tone="blue" title="Synthetic data only">
          The patient's name must include “Synthetic”, “Sample”, “Example” or “Test”. Only approved test numbers are texted or called.
        </Banner>
        {fields.map((f) => (
          <label className="field" key={f.key}>
            <span className="field-label">{f.label}</span>
            <input className="input" type={f.type ?? "text"} placeholder={f.placeholder} value={values[f.key] ?? ""} onChange={(e) => setValues({ ...values, [f.key]: e.target.value })} />
          </label>
        ))}
        {create.error && (
          <p className="field-error" role="alert" style={{ margin: 0 }}>
            {(create.error as Error).message}
          </p>
        )}
      </div>
    </Sheet>
  );
}

export function Refills() {
  const canStart = useCan("start_task");
  const [adding, setAdding] = useState(false);
  return (
    <>
      <ClinicalPage
        type="refill_request"
        title="Refills"
        description="Refill requests by phone, text or staff entry. The agent captures the medicine, dose and pharmacy, lays out the protocol facts from the EHR mirror, and tells the patient the clinician's decision in approved words."
        banner={
          <Banner tone="blue" title="No prescribing here">
            A clinician decides every request. Any prescription is sent by the clinician from the EHR; a prescribing partner comes later.
          </Banner>
        }
        actions={
          canStart && (
            <button className="btn btn-primary" onClick={() => setAdding(true)}>
              <Plus size={15} aria-hidden /> New request
            </button>
          )
        }
        meta={(r) => [r.medication, r.pharmacy, r.decision ? `decided: ${r.decision.replace("_", " ")}` : null, timeAgo(r.created_at)].filter(Boolean).join(" · ")}
      />
      {adding && (
        <NewCaseSheet
          type="refill_request"
          title="New refill request"
          subtitle="A request taken by hand. The playbook reads the protocol facts and puts it in the clinician queue."
          fields={[
            ...PATIENT_FIELDS,
            { key: "medication", label: "Medicine" },
            { key: "dose", label: "Dose on the label" },
            { key: "pharmacy", label: "Pharmacy" },
          ]}
          onClose={() => setAdding(false)}
        />
      )}
    </>
  );
}

export function Discharges() {
  const canStart = useCan("start_task");
  const qc = useQueryClient();
  const toast = useToast();
  const onError = useErr();
  const [adding, setAdding] = useState(false);
  const importFile = useMutation({
    mutationFn: async (file: File) => api.post<{ created: number }>("/clinical/discharges/import", { csv: await file.text() }),
    onSuccess: (r) => {
      toast(`${r.created} discharge follow-ups started`);
      qc.invalidateQueries({ queryKey: ["clinical"] });
    },
    onError,
  });
  return (
    <>
      <ClinicalPage
        type="discharge_followup"
        title="Discharge follow-up"
        description="A hospital discharge notice becomes a call within two business days with a scripted check. Red-flag answers reach a clinician at once."
        banner={
          <Banner tone="blue" title="Notices come in by hand or import for now">
            Add one, import a CSV (patient_name, patient_phone, date_of_birth, facility, discharged_on), or load discharge facts on Care opportunities. The ADT feed comes later.
          </Banner>
        }
        actions={
          canStart && (
            <>
              <label className="btn btn-secondary" style={{ cursor: "pointer" }}>
                <FileUp size={15} aria-hidden /> Import CSV
                <input
                  type="file"
                  accept=".csv,text/csv"
                  className="sr-only"
                  onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) importFile.mutate(f);
                    e.target.value = "";
                  }}
                />
              </label>
              <button className="btn btn-primary" onClick={() => setAdding(true)}>
                <Plus size={15} aria-hidden /> Add discharge
              </button>
            </>
          )
        }
        meta={(r) => [r.facility, r.discharged_on && `out ${r.discharged_on}`, r.call_by && `call by ${r.call_by}`, `${r.answered}/${r.questions} answered`].filter(Boolean).join(" · ")}
      />
      {adding && (
        <NewCaseSheet
          type="discharge_followup"
          title="Add a discharge"
          subtitle="The patient is called within two business days of the discharge date."
          fields={[...PATIENT_FIELDS, { key: "facility", label: "Discharged from" }, { key: "discharged_on", label: "Discharge date", type: "date" }, { key: "clinician_name", label: "Clinician of record" }]}
          onClose={() => setAdding(false)}
        />
      )}
    </>
  );
}

export function Previsit() {
  const canStart = useCan("start_task");
  const qc = useQueryClient();
  const onError = useErr();
  const [, setParams] = useSearchParams();
  const [adding, setAdding] = useState(false);
  const visits = useQuery({ queryKey: ["previsit-upcoming"], queryFn: () => api.get<{ visits: UpcomingVisit[] }>("/clinical/previsit/upcoming") });
  const prepare = useMutation({
    mutationFn: (id: string) => api.post<ClinicalDetail>("/clinical/previsit", { appointment_id: id }),
    onSuccess: (d) => {
      qc.invalidateQueries({ queryKey: ["clinical"] });
      qc.invalidateQueries({ queryKey: ["previsit-upcoming"] });
      setParams({ case: d.id });
    },
    onError,
  });
  const upcoming = visits.data?.visits ?? [];
  return (
    <>
      <ClinicalPage
        type="previsit_prep"
        title="Pre-visit"
        description="A questionnaire by secure text link (or a call), the answers summarised, and a chart-prep brief for the clinician: problems, medicines, open care gaps, outside records and the answers."
        banner={
          <Banner tone="blue" title="Prepared, never interpreted">
            The brief lists what the chart and the patient say. Answers the questionnaire marks for attention come first; nothing is diagnosed or ranked by the agent.
          </Banner>
        }
        actions={
          canStart && (
            <button className="btn btn-secondary" onClick={() => setAdding(true)}>
              <Plus size={15} aria-hidden /> Prepare by hand
            </button>
          )
        }
        extra={
          canStart && (
            <Card title="Upcoming visits" subtitle="Booked in the next 7 days, from the EHR mirror">
              {visits.isPending && <ListSkeleton rows={2} />}
              {visits.data && upcoming.length === 0 && <p className="card-pad small muted" style={{ margin: 0 }}>No booked visits in the next week.</p>}
              {upcoming.length > 0 && (
                <ul className="list">
                  {upcoming.slice(0, 8).map((v) => (
                    <li key={v.id} className="list-row">
                      <div className="row-main">
                        <span className="row-title">{v.patient}</span> <span className="small muted">{new Date(v.starts_at).toLocaleString()}</span>
                      </div>
                      {v.prepared ? (
                        <Pill tone="green">Prepared</Pill>
                      ) : (
                        <button className="btn btn-secondary btn-sm" disabled={prepare.isPending} onClick={() => prepare.mutate(v.id)}>
                          Prepare
                        </button>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          )
        }
        meta={(r) => [r.appointment_at && `visit ${r.appointment_at}`, r.clinician_name, `${r.answered}/${r.questions} answered`].filter(Boolean).join(" · ")}
      />
      {adding && (
        <NewCaseSheet
          type="previsit_prep"
          title="Prepare a visit by hand"
          subtitle="Sends the questionnaire link and builds the brief."
          fields={[...PATIENT_FIELDS, { key: "clinician_name", label: "Clinician" }, { key: "appointment_at", label: "Visit", type: "datetime-local" }]}
          onClose={() => setAdding(false)}
        />
      )}
    </>
  );
}

// --- the case sheet -------------------------------------------------------------------------------

function ClinicalSheet({ id, onClose }: { id: string; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const onError = useErr();
  const q = useQuery({ queryKey: ["clinical-case", id], queryFn: () => api.get<ClinicalDetail>(`/clinical/cases/${id}`), refetchInterval: 6000 });
  const set = (d: ClinicalDetail) => {
    qc.setQueryData(["clinical-case", id], d);
    qc.invalidateQueries({ queryKey: ["clinical"] });
    qc.invalidateQueries({ queryKey: ["inbox"] });
  };
  const signOff = useMutation({ mutationFn: () => api.post<ClinicalDetail>(`/clinical/cases/${id}/sign-off`), onSuccess: (d) => (set(d), toast("Signed off")), onError });
  const link = useMutation({
    mutationFn: () => api.post<{ url: string }>(`/clinical/cases/${id}/form-link`),
    onSuccess: async (r) => {
      try {
        await navigator.clipboard.writeText(r.url);
        toast("New link copied; the old one no longer works");
      } catch {
        toast(r.url);
      }
    },
    onError,
  });
  const d = q.data;
  const type = d?.case_type as ClinicalType | undefined;
  const ready = d && d.stage === "with_clinician";
  return (
    <Sheet
      wide
      title={d ? `${d.case_ref} · ${d.type_name}` : "Case"}
      subtitle={d ? [d.subject, d.case.status_reason].filter(Boolean).join(" · ") : undefined}
      onClose={onClose}
      footer={
        d && (
          <>
            {type !== "refill_request" && d.script && (
              <button className="btn btn-secondary" disabled={link.isPending} onClick={() => link.mutate()}>
                <Copy size={14} aria-hidden /> New questionnaire link
              </button>
            )}
            <span className="spacer" />
            {type !== "refill_request" && ready && d.can_decide && (
              <button className="btn btn-primary" disabled={signOff.isPending || d.red_flags.length > 0 && !d.red_flag_ack} onClick={() => signOff.mutate()}>
                <Stethoscope size={15} aria-hidden /> {type === "previsit_prep" ? "Brief reviewed" : "Answers reviewed"}
              </button>
            )}
          </>
        )
      }
    >
      {q.isPending && <ListSkeleton rows={4} />}
      {q.error && <ErrorState error={q.error} onRetry={() => q.refetch()} />}
      {d && (
        <div className="stack" style={{ gap: 18 }}>
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            <StagePill stage={d.stage} />
            {d.case.next_step && <span className="small muted">Next: {d.case.next_step}</span>}
          </div>
          {d.red_flags.length > 0 && <RedFlag d={d} onDone={set} />}
          {type === "refill_request" && <RefillBody d={d} onDone={set} />}
          {type === "discharge_followup" && (
            <Section title="Discharge">
              <p className="small" style={{ margin: 0 }}>
                {d.facility} · out {d.discharged_on} · <b>call by {d.call_by}</b>
              </p>
            </Section>
          )}
          {type === "previsit_prep" && d.brief && <Brief b={d.brief} />}
          {type !== "refill_request" && (
            <Section title={type === "previsit_prep" ? "Questionnaire answers" : "Scripted check"}>
              <Answers lines={d.answers} empty="No answers yet." />
              {d.remaining.length > 0 && d.script && <StaffAnswers d={d} onDone={set} />}
            </Section>
          )}
          <Section title="Playbook">
            <Steps run={d.run} />
          </Section>
          <Section title="Completion contract">
            <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
              {d.case.contract.parts.map((p) => (
                <li key={p.key} style={{ color: p.met ? undefined : "var(--label-2)" }}>
                  {p.met ? "✓" : "○"} {p.label}: {p.detail}
                </li>
              ))}
            </ul>
          </Section>
        </div>
      )}
    </Sheet>
  );
}

function RedFlag({ d, onDone }: { d: ClinicalDetail; onDone: (d: ClinicalDetail) => void }) {
  const onError = useErr();
  const [action, setAction] = useState("called_patient");
  const [note, setNote] = useState("");
  const ack = useMutation({ mutationFn: () => api.post<ClinicalDetail>(`/clinical/cases/${d.id}/red-flag`, { action, note }), onSuccess: onDone, onError });
  const labels = d.red_flags.map((f) => f.label.toLowerCase()).join(", ");
  if (d.red_flag_ack)
    return (
      <Banner tone="green" title={`Red flag reviewed by ${d.red_flag_ack.name}`}>
        {d.red_flag_ack.action.replace(/_/g, " ")}
        {d.red_flag_ack.note ? ` · ${d.red_flag_ack.note}` : ""}
      </Banner>
    );
  return (
    <Banner tone="red" title="Red flag: a clinician needs to act now" icon={AlertTriangle}>
      <div className="stack" style={{ gap: 8 }}>
        <span>The patient answered yes or unsure to: {labels}.</span>
        {d.can_decide ? (
          <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
            <select className="select" style={{ width: "auto" }} value={action} onChange={(e) => setAction(e.target.value)} aria-label="What you did">
              <option value="called_patient">I called the patient</option>
              <option value="sent_to_emergency">Sent to emergency care</option>
              <option value="booked_visit">Booked a visit</option>
              <option value="no_action_needed">No action needed</option>
              <option value="other">Other</option>
            </select>
            <input className="input" style={{ flex: 1, minWidth: 160 }} placeholder="Note (optional)" value={note} maxLength={300} onChange={(e) => setNote(e.target.value)} />
            <button className="btn btn-primary" disabled={ack.isPending} onClick={() => ack.mutate()}>
              Acknowledge
            </button>
          </div>
        ) : (
          <span className="small">Only a clinician can acknowledge it.</span>
        )}
      </div>
    </Banner>
  );
}

function RefillBody({ d, onDone }: { d: ClinicalDetail; onDone: (d: ClinicalDetail) => void }) {
  const onError = useErr();
  const library = useQuery({ queryKey: ["instructions"], queryFn: () => api.get<InstructionList>("/clinical/instructions") });
  const [outcome, setOutcome] = useState<"approved" | "needs_visit" | "denied" | "other">("approved");
  const [instructionId, setInstructionId] = useState("");
  const [note, setNote] = useState("");
  const options = (library.data?.instructions ?? []).filter((i) => i.topic === "refill" && i.state === "approved" && (!i.outcome || i.outcome === outcome));
  const chosen = options.find((i) => i.id === instructionId) ?? options[0];
  const decide = useMutation({
    mutationFn: () => api.post<ClinicalDetail>(`/clinical/cases/${d.id}/decide`, { outcome, instruction_id: outcome === "other" ? null : chosen?.id, note }),
    onSuccess: onDone,
    onError,
  });
  const facts = d.case.fields.filter((f) => ["medication", "dose", "pharmacy", "requested_by"].includes(f.key));
  return (
    <>
      <Section title="Request">
        <p className="small" style={{ margin: 0 }}>
          {facts.map((f) => `${f.label}: ${f.value ?? "—"}`).join(" · ")}
        </p>
      </Section>
      <Section title="Protocol facts from the EHR mirror">
        {d.protocol.length === 0 ? (
          <p className="small muted" style={{ margin: 0 }}>
            Not read yet.
          </p>
        ) : (
          <ul className="list" style={{ margin: 0 }}>
            {d.protocol.map((f) => (
              <li key={f.key} className="list-row" style={{ padding: "6px 0" }}>
                <div className="row-main small">
                  <b>{f.label}</b> · {f.value} <span className="muted">· {f.source}</span>
                </div>
                {f.meets === false && <Pill tone="orange">Look at</Pill>}
              </li>
            ))}
          </ul>
        )}
      </Section>
      {d.decided ? (
        <Section title="Decision">
          <p className="small" style={{ margin: 0 }}>
            <b>{DECISION_LABEL[d.decided.outcome] ?? d.decided.outcome}</b> by {d.decided.name} {timeAgo(d.decided.at)}
            {d.decided.instruction_title ? ` · words: “${d.decided.instruction_title}” v${d.decided.version}` : " · a person tells the patient"}
            {d.decided.note ? ` · note: ${d.decided.note}` : ""}
          </p>
          {d.delivered && <p className="small muted" style={{ margin: 0 }}>Delivered word for word by {d.delivered.by}.</p>}
        </Section>
      ) : d.stage === "with_clinician" && d.can_decide ? (
        <Section title="Your decision">
          <Segmented label="Decision" value={outcome} onChange={(v) => (setOutcome(v), setInstructionId(""))} options={(["approved", "needs_visit", "denied", "other"] as const).map((v) => ({ value: v, label: DECISION_LABEL[v].split(" (")[0] }))} />
          {outcome !== "other" &&
            (options.length === 0 ? (
              <Banner tone="orange" title="No approved words for this decision">
                Approve wording on the Instructions page, or choose Other and a person will tell the patient.
              </Banner>
            ) : (
              <>
                <select className="select" value={chosen?.id ?? ""} onChange={(e) => setInstructionId(e.target.value)} aria-label="Approved words the patient will hear">
                  {options.map((i) => (
                    <option key={i.id} value={i.id}>
                      {i.title} (v{i.version})
                    </option>
                  ))}
                </select>
                {chosen && <blockquote className="small" style={{ margin: 0, paddingLeft: 10, borderLeft: "3px solid var(--separator)" }}>{chosen.body}</blockquote>}
              </>
            ))}
          <input className="input" placeholder="Note for the chart (not sent to the patient)" value={note} maxLength={300} onChange={(e) => setNote(e.target.value)} />
          <div className="row">
            <span className="spacer" />
            <button className="btn btn-primary" disabled={decide.isPending || (outcome !== "other" && !chosen)} onClick={() => decide.mutate()}>
              <Stethoscope size={15} aria-hidden /> Decide
            </button>
          </div>
        </Section>
      ) : (
        <p className="small muted" style={{ margin: 0 }}>
          {d.stage === "with_clinician" ? "Waiting for a clinician's decision." : "The clinician decides once the protocol facts are read."}
        </p>
      )}
    </>
  );
}

function StaffAnswers({ d, onDone }: { d: ClinicalDetail; onDone: (d: ClinicalDetail) => void }) {
  const onError = useErr();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [values, setValues] = useState<Record<string, { answer: string; detail: string }>>({});
  const questions = (d.script?.questions ?? []).filter((q) => !d.answers.some((a) => a.question === q.key));
  const save = useMutation({
    mutationFn: () =>
      api.post<{ red_flag: boolean; say?: string; case: ClinicalDetail }>(`/clinical/cases/${d.id}/answers`, {
        answers: Object.entries(values)
          .filter(([, v]) => v.answer && (v.answer !== "text" || v.detail.trim()))
          .map(([question, v]) => ({ question, answer: v.answer, detail: v.detail || null })),
      }),
    onSuccess: (r) => {
      onDone(r.case);
      setValues({});
      setOpen(false);
      if (r.red_flag && r.say) toast(`Red flag. Tell the patient: “${r.say}”`);
    },
    onError,
  });
  if (!open)
    return (
      <div>
        <button className="btn btn-secondary btn-sm" onClick={() => setOpen(true)}>
          Record answers from a call
        </button>
      </div>
    );
  return (
    <div className="subcard stack" style={{ gap: 10, padding: 12 }}>
      <span className="small muted">Ask each question word for word and record what the patient says.</span>
      {questions.map((q) => {
        const v = values[q.key] ?? { answer: q.kind === "text" ? "text" : "", detail: "" };
        const put = (patch: Partial<typeof v>) => setValues({ ...values, [q.key]: { ...v, ...patch } });
        return (
          <label key={q.key} className="field">
            <span className="field-label">
              {q.text} {q.red_flag && <span className="pill tone-red">Red flag if yes</span>}
            </span>
            <div className="row" style={{ gap: 6 }}>
              {q.kind === "yes_no" && (
                <select className="select" style={{ width: 130 }} value={v.answer} onChange={(e) => put({ answer: e.target.value })}>
                  <option value="">—</option>
                  <option value="yes">Yes</option>
                  <option value="no">No</option>
                  <option value="unsure">Unsure</option>
                  <option value="declined">Declined</option>
                </select>
              )}
              <input className="input" placeholder={q.kind === "text" ? "Their words" : "Their words (optional)"} value={v.detail} maxLength={300} onChange={(e) => put({ detail: e.target.value })} />
            </div>
          </label>
        );
      })}
      <div className="row">
        <span className="spacer" />
        <button className="btn btn-plain" onClick={() => setOpen(false)}>
          Cancel
        </button>
        <button className="btn btn-primary" disabled={save.isPending} onClick={() => save.mutate()}>
          Save answers
        </button>
      </div>
    </div>
  );
}

function Brief({ b }: { b: ChartBrief }) {
  return (
    <Section title="Chart-prep brief">
      {!b.linked && (
        <Banner tone="orange" title="Not linked to a chart">
          This case has no EHR patient, so only the questionnaire is in the brief.
        </Banner>
      )}
      {b.attention.length > 0 && (
        <p className="small" style={{ margin: 0 }}>
          <b>Patient flagged:</b> {b.attention.join(", ")}
        </p>
      )}
      <div className="grid-2" style={{ gap: 12 }}>
        <div className="small">
          <b>Problems</b>
          <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{b.problems.length ? b.problems.map((p, i) => <li key={i}>{p.code} {p.since ? <span className="muted">since {p.since}</span> : null}</li>) : <li className="muted">None on file</li>}</ul>
        </div>
        <div className="small">
          <b>Medicines</b>
          <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{b.medicines.length ? b.medicines.map((m, i) => <li key={i}>{m.name} {m.dose}</li>) : <li className="muted">None on file</li>}</ul>
        </div>
        <div className="small">
          <b>Open care gaps</b>
          <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{b.care_gaps.length ? b.care_gaps.map((g, i) => <li key={i}>{g.program} <span className="muted">({g.status})</span></li>) : <li className="muted">None open</li>}</ul>
        </div>
        <div className="small">
          <b>Outside records</b>
          <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{b.outside_records.length ? b.outside_records.map((r, i) => <li key={i}>{r.summary} · {r.source} <span className="muted">{r.date}</span></li>) : <li className="muted">None received</li>}</ul>
        </div>
      </div>
      <p className="small muted" style={{ margin: 0 }}>
        Last visit {b.last_visit ?? "none in the mirror"} · next {b.next_visit ?? "none booked"}
      </p>
    </Section>
  );
}

// --- the approved instructions library -----------------------------------------------------------

const TOPICS: Instruction["topic"][] = ["refill", "discharge", "previsit", "general"];

export function Instructions() {
  const qc = useQueryClient();
  const toast = useToast();
  const onError = useErr();
  const canStart = useCan("start_task");
  const canSign = useCan("clinical_signoff");
  const canEdit = canStart || canSign;
  const q = useQuery({ queryKey: ["instructions"], queryFn: () => api.get<InstructionList>("/clinical/instructions") });
  const [editing, setEditing] = useState<Instruction | "new" | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["instructions"] });
  const act = useMutation({
    mutationFn: ({ i, verb }: { i: Instruction; verb: "approve" | "retire" }) => api.post<Instruction>(`/clinical/instructions/${i.id}/${verb}`),
    onSuccess: (i) => (toast(i.state === "approved" ? "Approved: these exact words can now reach patients" : "Retired"), refresh()),
    onError,
  });
  const d = q.data;
  return (
    <>
      <Toolbar
        eyebrow="Clinical"
        title="Approved instructions"
        description="Words a clinician has approved, delivered to patients exactly as written. Anything a clinician wants to say that isn't here goes to a person to say."
        actions={
          canEdit && (
            <button className="btn btn-primary" onClick={() => setEditing("new")}>
              <Plus size={15} aria-hidden /> New instruction
            </button>
          )
        }
      />
      <div className="content">
        <Banner tone="blue" title="Only approved words are sent">
          Starter wording arrives as drafts for your clinical lead. Changing approved words makes a new draft version; nothing changed is sent until a clinician approves it again. Texts can't carry medicines, doses or diagnoses.
        </Banner>
        {q.isPending && (
          <Card>
            <ListSkeleton rows={4} />
          </Card>
        )}
        {q.error && (
          <Card>
            <ErrorState error={q.error} onRetry={() => q.refetch()} />
          </Card>
        )}
        {d &&
          TOPICS.map((topic) => {
            const rows = d.instructions.filter((i) => i.topic === topic);
            if (!rows.length) return null;
            return (
              <Card key={topic} title={topic === "previsit" ? "Pre-visit" : topic[0].toUpperCase() + topic.slice(1)}>
                <ul className="list">
                  {rows.map((i) => (
                    <li key={i.id} className="list-row" style={{ alignItems: "flex-start", gap: 12 }}>
                      <BookCheck size={16} aria-hidden style={{ color: "var(--label-2)", marginTop: 3 }} />
                      <div className="row-main stack" style={{ gap: 4 }}>
                        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                          <span className="row-title">{i.title}</span>
                          <span className="small muted mono">
                            {i.key} v{i.version}
                          </span>
                          <span className="chip">{i.channel === "any" ? "Text or call" : i.channel === "text" ? "Text" : "Call only"}</span>
                          {i.outcome && <span className="chip">{DECISION_LABEL[i.outcome]}</span>}
                        </div>
                        <p className="small" style={{ margin: 0 }}>
                          {i.body}
                        </p>
                        {i.approved_by && <span className="small muted">Approved by {i.approved_by} {timeAgo(i.approved_at)}</span>}
                      </div>
                      <div className="stack" style={{ gap: 6, justifyItems: "end", flex: "none" }}>
                        <Pill tone={i.state === "approved" ? "green" : i.state === "draft" ? "orange" : "gray"}>{i.state === "draft" ? "Draft" : i.state === "approved" ? "Approved" : "Retired"}</Pill>
                        <div className="row" style={{ gap: 6 }}>
                          {canEdit && i.state !== "retired" && (
                            <button className="btn btn-plain btn-sm" onClick={() => setEditing(i)}>
                              Edit
                            </button>
                          )}
                          {d.can_approve && i.state === "draft" && (
                            <button className="btn btn-primary btn-sm" disabled={act.isPending} onClick={() => act.mutate({ i, verb: "approve" })}>
                              Approve
                            </button>
                          )}
                          {d.can_approve && i.state === "approved" && (
                            <button className="btn btn-secondary btn-sm" disabled={act.isPending} onClick={() => act.mutate({ i, verb: "retire" })}>
                              Retire
                            </button>
                          )}
                        </div>
                      </div>
                    </li>
                  ))}
                </ul>
              </Card>
            );
          })}
      </div>
      {editing && <InstructionSheet item={editing === "new" ? null : editing} onClose={() => setEditing(null)} onDone={refresh} />}
    </>
  );
}

function InstructionSheet({ item, onClose, onDone }: { item: Instruction | null; onClose: () => void; onDone: () => void }) {
  const [v, setV] = useState({
    key: item?.key ?? "",
    title: item?.title ?? "",
    topic: item?.topic ?? "refill",
    outcome: item?.outcome ?? "",
    channel: item?.channel ?? "any",
    body: item?.body ?? "",
  });
  const save = useMutation({
    mutationFn: () => {
      const body = { title: v.title, topic: v.topic, outcome: v.outcome || null, channel: v.channel, body: v.body };
      return item ? api.put<Instruction>(`/clinical/instructions/${item.id}`, body) : api.post<Instruction>("/clinical/instructions", { ...body, key: v.key });
    },
    onSuccess: () => (onDone(), onClose()),
  });
  return (
    <Sheet
      title={item ? "Edit instruction" : "New instruction"}
      subtitle="Saved as a draft. A clinician approves it before any patient hears it."
      onClose={onClose}
      footer={
        <>
          <span className="spacer" />
          <button className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" disabled={save.isPending || !v.title.trim() || v.body.trim().length < 10} onClick={() => save.mutate()}>
            Save draft
          </button>
        </>
      }
    >
      <div className="stack" style={{ gap: 14 }}>
        {!item && (
          <label className="field">
            <span className="field-label">Key</span>
            <input className="input mono" value={v.key} placeholder="refill_lab_first" onChange={(e) => setV({ ...v, key: e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, "_") })} />
          </label>
        )}
        <label className="field">
          <span className="field-label">Title</span>
          <input className="input" value={v.title} onChange={(e) => setV({ ...v, title: e.target.value })} />
        </label>
        <div className="grid-3" style={{ gap: 10 }}>
          <label className="field">
            <span className="field-label">Topic</span>
            <select className="select" value={v.topic} onChange={(e) => setV({ ...v, topic: e.target.value as Instruction["topic"] })}>
              {TOPICS.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="field-label">Tells the patient</span>
            <select className="select" value={v.outcome} onChange={(e) => setV({ ...v, outcome: e.target.value as Instruction["outcome"] & string })}>
              <option value="">Any (not a decision)</option>
              <option value="approved">Refill approved</option>
              <option value="needs_visit">Visit needed first</option>
              <option value="denied">Not renewed</option>
            </select>
          </label>
          <label className="field">
            <span className="field-label">Delivered by</span>
            <select className="select" value={v.channel} onChange={(e) => setV({ ...v, channel: e.target.value as Instruction["channel"] })}>
              <option value="any">Text, else a call</option>
              <option value="text">Text only</option>
              <option value="call">Call only</option>
            </select>
          </label>
        </div>
        <label className="field">
          <span className="field-label">Exact words</span>
          <textarea className="textarea" rows={4} value={v.body} maxLength={600} onChange={(e) => setV({ ...v, body: e.target.value })} />
          <span className="field-help">You can use {"{first_name}"} and {"{practice_phone}"}. Texts start with the practice's name.</span>
        </label>
        {save.error && (
          <p className="field-error" role="alert" style={{ margin: 0 }}>
            {(save.error as Error).message}
          </p>
        )}
      </div>
    </Sheet>
  );
}

