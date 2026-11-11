import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, CircleDashed, Clock, FileUp, HeartPulse, RefreshCw, Users, XCircle } from "lucide-react";
import { useState } from "react";
import { api, ApiError, type CareOpportunity, type CareQueue, type CareStatus } from "../api";
import { useCan } from "../App";
import { Banner, Card, Empty, ErrorState, ListSkeleton, Pill, Segmented, Toolbar, useToast } from "../ui";

type View = "proposed" | "approved" | "contacted" | "closed";
const CLOSED: CareStatus[] = ["enrolled", "done", "not_interested", "declined", "resolved"];

const STATUS: Record<CareStatus, ["green" | "orange" | "red" | "blue" | "gray", string]> = {
  proposed: ["orange", "To review"],
  approved: ["blue", "Approved"],
  contacted: ["blue", "Contacted"],
  enrolled: ["green", "Enrolled"],
  done: ["green", "Done"],
  not_interested: ["gray", "Not interested"],
  declined: ["gray", "Declined"],
  resolved: ["gray", "No longer applies"],
};

const COVERAGE: Record<string, string> = {
  medicare: "Medicare",
  medicare_advantage: "Medicare Advantage",
  medicaid: "Medicaid",
  commercial: "Commercial",
};

export default function CareOpportunities() {
  const qc = useQueryClient();
  const toast = useToast();
  const isAdmin = useCan("admin");
  const queue = useQuery({ queryKey: ["care"], queryFn: () => api.get<CareQueue>("/care-opportunities") });
  const [view, setView] = useState<View>("proposed");
  const set = (data: CareQueue) => qc.setQueryData(["care"], data);
  const refresh = () => qc.invalidateQueries({ queryKey: ["care"] });
  const onError = (e: unknown) => toast(e instanceof ApiError ? e.message : "Something went wrong");

  const sample = useMutation({
    mutationFn: () => api.post<{ scan: { created: number } }>("/care-opportunities/sample"),
    onSuccess: (r) => {
      toast(`Sample patients loaded · ${r.scan.created} opportunities found`);
      refresh();
    },
    onError,
  });
  const rescan = useMutation({
    mutationFn: () => api.post<{ created: number; resolved: number }>("/care-opportunities/scan"),
    onSuccess: (r) => {
      toast(`${r.created} new, ${r.resolved} no longer apply`);
      refresh();
    },
    onError,
  });
  const importFile = useMutation({
    mutationFn: async (file: File) => api.post<{ imported: { patients: number }; scan: { created: number } }>("/care-opportunities/facts", { csv: await file.text() }),
    onSuccess: (r) => {
      toast(`${r.imported.patients} patients imported · ${r.scan.created} opportunities found`);
      refresh();
    },
    onError,
  });

  const q = queue.data;
  const items = (q?.items ?? []).filter((o) => (view === "closed" ? CLOSED.includes(o.status) : o.status === view));
  const count = (s: CareStatus[]) => s.reduce((n, k) => n + (q?.counts[k] ?? 0), 0);

  return (
    <>
      <Toolbar
        eyebrow="Operate"
        title="Care opportunities"
        description="Services each patient qualifies for and hasn’t had: care management, monitoring, transitions, behavioral health and preventive care. A clinician approves each one before anyone contacts the patient."
        actions={
          isAdmin && (
            <>
              <label className="btn btn-secondary" style={{ cursor: "pointer" }}>
                <FileUp size={15} aria-hidden /> Import facts
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
              <button className="btn btn-primary" onClick={() => rescan.mutate()} disabled={rescan.isPending}>
                <RefreshCw size={15} className={rescan.isPending ? "spin" : ""} aria-hidden /> Scan patients
              </button>
            </>
          )
        }
      />
      <div className="content">
        <Banner tone="blue" title="Suggestions, not orders">
          Every suggestion names the facts behind it so a clinician can check the chart. Care management, monitoring and behavioral health are billed on clinical staff time; the voice agent explains, records consent and books, but its own minutes never count.
        </Banner>

        {queue.isPending && (
          <Card>
            <ListSkeleton rows={4} />
          </Card>
        )}
        {queue.error && (
          <Card>
            <ErrorState error={queue.error} onRetry={() => queue.refetch()} title="Couldn't load care opportunities" />
          </Card>
        )}
        {q && q.patients === 0 && (
          <Card>
            <Empty
              icon={HeartPulse}
              title="No patient facts yet"
              action={
                isAdmin && (
                  <button className="btn btn-primary" onClick={() => sample.mutate()} disabled={sample.isPending}>
                    <Users size={15} aria-hidden /> Load synthetic patients
                  </button>
                )
              }
            >
              Import a CSV export from your EHR (diagnoses, services already given, discharges, screening scores), or load synthetic patients to see how the queue works.
            </Empty>
          </Card>
        )}

        {q && q.patients > 0 && (
          <>
            <div className="grid-3" style={{ gridTemplateColumns: "repeat(4, minmax(0, 1fr))" }}>
              <Stat value={q.patients} label="Patients scanned" />
              <Stat value={count(["proposed"])} label="To review" />
              <Stat value={count(["approved", "contacted"])} label="In outreach" />
              <Stat value={count(["enrolled", "done"])} label="Enrolled or done" />
            </div>
            {q.open_by_program.length > 0 && (
              <Card title="Open by program">
                <div className="card-pad row" style={{ flexWrap: "wrap", gap: 8, paddingTop: 0 }}>
                  {q.open_by_program.map((p) => (
                    <span className="chip" key={p.program}>
                      {p.title} · <b>{p.count}</b>
                    </span>
                  ))}
                </div>
              </Card>
            )}
            <Card
              title="Queue"
              actions={
                <Segmented
                  label="Queue view"
                  value={view}
                  onChange={setView}
                  options={[
                    { value: "proposed", label: `To review ${count(["proposed"])}` },
                    { value: "approved", label: `Approved ${count(["approved"])}` },
                    { value: "contacted", label: `Contacted ${count(["contacted"])}` },
                    { value: "closed", label: "Closed" },
                  ]}
                />
              }
            >
              {items.length === 0 ? (
                <Empty icon={CheckCircle2} title="Nothing here" />
              ) : (
                <ul className="list">
                  {items.map((o) => (
                    <Row key={o.id} o={o} onDone={set} onError={onError} />
                  ))}
                </ul>
              )}
            </Card>
          </>
        )}
      </div>
    </>
  );
}

function Stat({ value, label }: { value: number; label: string }) {
  return (
    <div className="card stat">
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

function Row({ o, onDone, onError }: { o: CareOpportunity; onDone: (q: CareQueue) => void; onError: (e: unknown) => void }) {
  const canReview = useCan("review");
  const canOutreach = useCan("start_task");
  const [declining, setDeclining] = useState(false);
  const [note, setNote] = useState("");
  const decide = useMutation({
    mutationFn: (decision: "approve" | "decline") => api.post<CareQueue>(`/care-opportunities/${o.id}/decide`, { decision, note }),
    onSuccess: onDone,
    onError,
  });
  const outcome = useMutation({
    mutationFn: (value: string) => api.post<CareQueue>(`/care-opportunities/${o.id}/outcome`, { outcome: value }),
    onSuccess: onDone,
    onError,
  });
  const [tone, label] = STATUS[o.status];
  return (
    <li className="list-row" style={{ alignItems: "flex-start", gap: 16 }}>
      <div className="avatar" aria-hidden style={{ background: "var(--tile-navy)" }}>
        {o.patient.replace(/\W/g, "").slice(0, 2) || "P"}
      </div>
      <div className="row-main stack" style={{ gap: 6 }}>
        <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
          <span className="row-title">{o.title}</span>
          <span className="small muted mono">{o.codes}</span>
          {o.priority === 1 && (
            <Pill tone="red" icon={AlertTriangle}>
              Time-sensitive
            </Pill>
          )}
          {o.due_by && (
            <Pill tone="orange" icon={Clock}>
              Due {o.due_by}
            </Pill>
          )}
        </div>
        <div className="small muted">
          {o.patient} · {o.patient_ref}
          {o.coverage ? ` · ${COVERAGE[o.coverage] ?? o.coverage}` : ""}
        </div>
        <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
          {o.why.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
        <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
          {o.consent_required && <span className="chip">Consent needed</span>}
          {o.staff_time && <span className="chip">Clinical staff time</span>}
          <span className="small muted">Agent can: {o.outreach}</span>
        </div>
        {o.note && <div className="small muted">Note: {o.note}</div>}
        {declining && (
          <div className="row" style={{ gap: 6 }}>
            <input className="input" autoFocus value={note} maxLength={300} placeholder="Why it doesn't apply" aria-label="Reason for declining" onChange={(e) => setNote(e.target.value)} />
            <button className="btn btn-secondary" disabled={!note.trim() || decide.isPending} onClick={() => decide.mutate("decline")}>
              Decline
            </button>
            <button className="btn btn-plain" onClick={() => setDeclining(false)}>
              Cancel
            </button>
          </div>
        )}
      </div>
      <div className="stack" style={{ gap: 6, justifyItems: "end", flex: "none" }}>
        <Pill tone={tone} icon={o.status === "proposed" ? CircleDashed : o.status === "declined" ? XCircle : CheckCircle2}>
          {label}
        </Pill>
        {o.status === "proposed" && canReview && !declining && (
          <div className="row" style={{ gap: 6 }}>
            <button className="btn btn-secondary" onClick={() => setDeclining(true)}>
              Decline
            </button>
            <button className="btn btn-primary" disabled={decide.isPending} onClick={() => decide.mutate("approve")}>
              Approve
            </button>
          </div>
        )}
        {(o.status === "approved" || o.status === "contacted") && canOutreach && (
          <select className="select" style={{ width: "auto" }} value="" aria-label={`Record outcome for ${o.title}`} onChange={(e) => e.target.value && outcome.mutate(e.target.value)}>
            <option value="">Record outcome…</option>
            {o.status === "approved" && <option value="contacted">Contacted</option>}
            <option value="enrolled">Enrolled / booked</option>
            <option value="done">Service done</option>
            <option value="not_interested">Not interested</option>
          </select>
        )}
      </div>
    </li>
  );
}
