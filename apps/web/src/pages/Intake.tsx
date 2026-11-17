// Intake and registration: forms by secure link, insurance card capture, and the demographics and
// coverage proposed to the EHR as write intents a person approves.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, ClipboardList, Copy, IdCard, Plus } from "lucide-react";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError, type IntakeDetail, type IntakeRow, type ReminderCadence } from "../api";
import { useCan } from "../App";
import { Banner, Card, Empty, ErrorState, ListSkeleton, Pill, Sheet, Toolbar, timeAgo, useToast } from "../ui";

const FIELDS = [
  { key: "patient_name", label: "Patient (synthetic)", placeholder: "Synthetic Patient Alpha" },
  { key: "date_of_birth", label: "Date of birth", type: "date" },
  { key: "patient_phone", label: "Mobile phone", type: "tel", placeholder: "+1 202 555 0142" },
  { key: "appointment_at", label: "First visit", type: "datetime-local" },
];

const stage = (r: IntakeRow): ["green" | "orange" | "blue" | "gray", string] =>
  r.status === "done" ? ["green", "Registered"] : r.status === "handed_off" || r.status === "blocked" ? ["orange", "With the front desk"] : r.submitted_at ? ["blue", "Form received"] : ["blue", "Link sent"];

export default function Intake() {
  const canStart = useCan("start_task");
  const [params, setParams] = useSearchParams();
  const [adding, setAdding] = useState(false);
  const list = useQuery({ queryKey: ["intake"], queryFn: () => api.get<{ cases: IntakeRow[] }>("/intake"), refetchInterval: 8000 });
  const cadence = useQuery({ queryKey: ["reminder-cadence"], queryFn: () => api.get<ReminderCadence>("/reminders/cadence") });
  const open = params.get("case");
  return (
    <>
      <Toolbar
        eyebrow="Front Office"
        title="Intake & registration"
        description="Registration forms by secure link, with insurance card photos. The patient's demographics and coverage go to the EHR as write intents a person approves."
        actions={
          canStart && (
            <button className="btn btn-primary" onClick={() => setAdding(true)}>
              <Plus size={15} aria-hidden /> Send registration
            </button>
          )
        }
      />
      <div className="content">
        <Banner tone="blue" title="EHR registration writes need NexHealth's v3.1 beta">
          Until it's enabled, approved demographics and coverage become a front-desk task in the hub, with the card photos on the case. Labs serves the form behind the Studio sign-in; a patient-facing forms host comes before real patients.
        </Banner>
        <Card title="Registrations">
          {list.isPending && <ListSkeleton rows={3} />}
          {list.error && <ErrorState error={list.error} onRetry={() => list.refetch()} />}
          {list.data && list.data.cases.length === 0 && <Empty icon={ClipboardList} title="No registrations yet" />}
          {list.data && list.data.cases.length > 0 && (
            <ul className="list">
              {list.data.cases.map((r) => {
                const [tone, label] = stage(r);
                return (
                  <li key={r.id} className="list-row" style={{ cursor: "pointer" }} onClick={() => setParams({ case: r.id })}>
                    <div className="row-main stack" style={{ gap: 2 }}>
                      <span className="row-title">
                        {r.case_ref} · {r.subject ?? "Patient"}
                      </span>
                      <span className="small muted">
                        {[r.has_card_photo ? "card photo" : r.no_insurance ? "no insurance" : null, r.linked ? "linked to the EHR" : "not linked to a chart", timeAgo(r.created_at)].filter(Boolean).join(" · ")}
                      </span>
                    </div>
                    <Pill tone={tone}>{label}</Pill>
                  </li>
                );
              })}
            </ul>
          )}
        </Card>
        {cadence.data && (
          <Card title="Appointment reminders" subtitle="What the reminder playbook sends, and what each reply does">
            <div className="card-pad stack" style={{ gap: 8, paddingTop: 0 }}>
              <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                {cadence.data.stages.map((s) => (
                  <span className="chip" key={s.stage}>
                    <CalendarClock size={12} aria-hidden /> {s.hours_before >= 48 ? `${s.hours_before / 24} days` : `${s.hours_before} hours`} before
                  </span>
                ))}
              </div>
              <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
                {cadence.data.replies.map((r) => (
                  <li key={r.reply}>
                    <b>{r.reply}</b>: {r.does}
                  </li>
                ))}
              </ul>
              <span className="small muted">{cadence.data.fallback}</span>
            </div>
          </Card>
        )}
      </div>
      {adding && <NewIntake onClose={() => setAdding(false)} onOpen={(id) => setParams({ case: id })} />}
      {open && <IntakeSheet id={open} onClose={() => setParams({})} />}
    </>
  );
}

function NewIntake({ onClose, onOpen }: { onClose: () => void; onOpen: (id: string) => void }) {
  const qc = useQueryClient();
  const [values, setValues] = useState<Record<string, string>>({});
  const create = useMutation({
    mutationFn: () => api.post<IntakeDetail>("/intake", { fields: Object.fromEntries(Object.entries(values).filter(([, v]) => v.trim())) }),
    onSuccess: (d) => {
      qc.invalidateQueries({ queryKey: ["intake"] });
      onClose();
      onOpen(d.id);
    },
  });
  return (
    <Sheet
      title="Send registration"
      subtitle="Texts the patient a secure link to their registration form."
      onClose={onClose}
      footer={
        <>
          <span className="spacer" />
          <button className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" disabled={create.isPending} onClick={() => create.mutate()}>
            Send
          </button>
        </>
      }
    >
      <div className="stack" style={{ gap: 14 }}>
        <Banner tone="blue" title="Synthetic data only">
          The patient's name must include “Synthetic”, “Sample”, “Example” or “Test”; only approved test numbers get texts.
        </Banner>
        {FIELDS.map((f) => (
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

function IntakeSheet({ id, onClose }: { id: string; onClose: () => void }) {
  const toast = useToast();
  const canStart = useCan("start_task");
  const q = useQuery({ queryKey: ["intake", id], queryFn: () => api.get<IntakeDetail>(`/intake/${id}`), refetchInterval: 6000 });
  const link = useMutation({
    mutationFn: () => api.post<{ url: string }>(`/intake/${id}/link`),
    onSuccess: async (r) => {
      try {
        await navigator.clipboard.writeText(r.url);
        toast("New link copied; the old one no longer works");
      } catch {
        toast(r.url);
      }
    },
    onError: (e) => toast(e instanceof ApiError ? e.message : "Something went wrong"),
  });
  const d = q.data;
  return (
    <Sheet
      wide
      title={d ? `${d.case_ref} · Registration` : "Registration"}
      subtitle={d ? [d.subject, d.case.status_reason].filter(Boolean).join(" · ") : undefined}
      onClose={onClose}
      footer={
        d &&
        canStart &&
        !d.submitted_at && (
          <button className="btn btn-secondary" disabled={link.isPending} onClick={() => link.mutate()}>
            <Copy size={14} aria-hidden /> New link
          </button>
        )
      }
    >
      {q.isPending && <ListSkeleton rows={3} />}
      {q.error && <ErrorState error={q.error} onRetry={() => q.refetch()} />}
      {d && (
        <div className="stack" style={{ gap: 16 }}>
          {d.case.next_step && <Banner tone="orange" title="Next step">{d.case.next_step}</Banner>}
          <p className="small muted" style={{ margin: 0 }}>
            Link {d.form ? `sent ${timeAgo(d.form.sent_at)}${d.form.opened_at ? `, opened ${timeAgo(d.form.opened_at)}` : ""}${d.form.submitted_at ? `, submitted ${timeAgo(d.form.submitted_at)}` : ""}` : "not sent"}
          </p>
          {Object.keys(d.answers).length > 0 && (
            <div className="grid-2 small" style={{ gap: 6 }}>
              {Object.entries(d.answers)
                .filter(([, v]) => v !== null && v !== false)
                .map(([k, v]) => (
                  <div key={k}>
                    <span className="muted">{k.replace(/_/g, " ")}:</span> {String(v)}
                  </div>
                ))}
            </div>
          )}
          {(d.card.front || d.card.back) && (
            <div className="row" style={{ gap: 12, flexWrap: "wrap" }}>
              {[d.card.front, d.card.back].filter(Boolean).map((src, i) => (
                <figure key={i} style={{ margin: 0 }}>
                  <img src={src!} alt={i === 0 ? "Insurance card, front" : "Insurance card, back"} style={{ maxWidth: 260, borderRadius: 8, border: "1px solid var(--separator)" }} />
                  <figcaption className="small muted">
                    <IdCard size={12} aria-hidden /> {i === 0 ? "Front" : "Back"}
                  </figcaption>
                </figure>
              ))}
            </div>
          )}
          {d.writes.length > 0 && (
            <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
              {d.writes.map((w) => (
                <li key={w.id}>
                  {w.kind.replace(/_/g, " ")}: <b>{w.state}</b>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </Sheet>
  );
}
