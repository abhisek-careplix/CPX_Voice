import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  BadgeCheck,
  CalendarClock,
  CalendarX2,
  CheckCircle2,
  CircleDashed,
  Database,
  FilePenLine,
  MessageSquareText,
  PhoneCall,
  RefreshCw,
  ExternalLink,
  ShieldCheck,
  Stethoscope,
  XCircle,
} from "lucide-react";
import { useState } from "react";
import { api, ApiError, type EligibilityView, type FrontDeskMessage, type Huddle, type HuddleRow, type PracticeConnection, type PracticeOverview, type WriteIntentRow } from "../api";
import { useCan } from "../App";
import { PRACTICE_LABEL, usePractice, withPractice } from "../practice";
import { Banner, Card, Empty, Pill, Segmented, Sheet, Toolbar, timeAgo, useToast } from "../ui";

type Range = "today" | "tomorrow" | "week";

const FLAG_LABEL: Record<string, string> = {
  asap_list: "Wants sooner",
  no_insurance_on_file: "No insurance on file",
  not_checked: "Not checked",
  check_failed: "Check failed",
  coverage_inactive: "Coverage inactive",
  auth_required: "Auth required",
};

const RESOURCE_LABEL: Record<string, string> = {
  location: "Location",
  provider: "Providers",
  appointment_type: "Appointment types",
  insurance_plan: "Insurance plans",
  patient: "Patients",
  coverage: "Coverages",
  appointment: "Appointments (−1 to +60 days)",
};

function rangeParams(range: Range, tz: string | undefined): { date?: string; days: number } {
  if (range === "week") return { days: 7 };
  if (range === "today") return { days: 1 };
  const now = new Date(Date.now() + 86_400_000);
  const date = new Intl.DateTimeFormat("en-CA", { timeZone: tz ?? "UTC" }).format(now); // YYYY-MM-DD
  return { date, days: 1 };
}

export default function Practice() {
  const isAdmin = useCan("admin");
  const canRun = useCan("start_task");
  const qc = useQueryClient();
  const toast = useToast();
  const [range, setRange] = useState<Range>("week");
  const [alertFor, setAlertFor] = useState<HuddleRow | null>(null);
  const [practice, setPractice] = usePractice();
  const at = (path: string) => withPractice(path, practice);
  const overview = useQuery({ queryKey: ["practice", practice], queryFn: () => api.get<PracticeOverview>(at("/practices")), refetchInterval: 5000 });
  const tz = useQuery({ queryKey: ["practice-huddle", practice, "today"], queryFn: () => api.get<Huddle>(at("/practices/huddle?days=1")), enabled: !!overview.data?.ehr }).data?.timezone;
  const params = rangeParams(range, tz);
  const query = new URLSearchParams({ days: String(params.days), ...(params.date ? { date: params.date } : {}) });
  const huddle = useQuery({
    queryKey: ["practice-huddle", practice, range, params.date],
    queryFn: () => api.get<Huddle>(at(`/practices/huddle?${query}`)),
    enabled: !!overview.data?.ehr,
    refetchInterval: 5000,
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ["practice"] }).then(() => qc.invalidateQueries({ queryKey: ["practice-huddle"] }));
  const sync = useMutation({
    mutationFn: (full: boolean) => api.post<{ api_calls: number; eligibility_queued: number }>(at(`/practices/sync?full=${full}`)),
    onSuccess: (r) => {
      toast(`Synced · ${r.api_calls} EHR API calls · ${r.eligibility_queued} eligibility checks queued`);
      refresh();
    },
  });
  const verify = useMutation({
    mutationFn: (kind: "nexhealth" | "openemr" | "stedi") => api.post<{ ok: boolean; error?: string }>(at(`/practices/verify?kind=${kind}`)),
    onSuccess: (r) => {
      toast(r.ok ? "Verified with a live check" : `Verification failed: ${r.error ?? ""}`);
      refresh();
    },
  });
  const check = useMutation({
    mutationFn: (id: string) => api.post<EligibilityView>(at(`/practices/appointments/${id}/eligibility`)),
    onSuccess: (r) => {
      toast(r.state === "done" ? `Coverage ${r.summary?.coverage}${r.mock ? " (mock payer)" : ""}` : r.reason ?? r.state);
      refresh();
    },
  });

  const d = overview.data;
  const err = (sync.error ?? verify.error ?? check.error) as ApiError | null;

  if (overview.isSuccess && !d?.ehr) {
    return (
      <>
        <Toolbar eyebrow="Front office" title="Practice" />
        <div className="content">
          <Empty icon={Stethoscope} title="No practice connected">
            Run <code className="mono">make openemr-setup</code> or <code className="mono">make connect-nexhealth</code> (and{" "}
            <code className="mono">make connect-stedi</code>), then restart the API.
          </Empty>
        </div>
      </>
    );
  }

  const h = huddle.data;
  return (
    <>
      <Toolbar
        eyebrow="Front office"
        title={d?.location?.name ?? "Practice"}
        actions={
          <>
            {d && d.practices.length > 1 && (
              <Segmented
                label="Practice"
                value={d.ehr?.id ?? ""}
                onChange={setPractice}
                options={d.practices.map((p) => ({ value: p.id, label: PRACTICE_LABEL[p.kind] ?? p.name }))}
              />
            )}
            {d?.links && (
              <a className="btn btn-secondary" href={d.links.home} target="_blank" rel="noreferrer">
                <ExternalLink size={15} aria-hidden /> Open OpenEMR
              </a>
            )}
            <button className="btn btn-secondary" onClick={refresh}>
              <RefreshCw size={15} aria-hidden /> Refresh
            </button>
            {isAdmin && (
              <button className="btn btn-primary" onClick={() => sync.mutate(false)} disabled={sync.isPending}>
                {sync.isPending ? <span className="spinner" aria-label="Syncing" /> : <Database size={15} aria-hidden />} Sync now
              </button>
            )}
          </>
        }
      />
      <div className="content">
        {d?.ehr?.kind === "openemr" && (
          <Banner tone="green" title="Connected directly to OpenEMR." icon={ShieldCheck}>
            {" "}
            A real EHR running locally with a synthetic clinic. Every write lands in the chart immediately, and changes staff make in OpenEMR appear here within
            about 30 seconds.
            {d.clearinghouse?.env === "test" && " Eligibility uses Stedi test mode, labelled Mock."}
          </Banner>
        )}
        {d?.ehr?.env === "sandbox" && (
          <Banner tone="purple" title="NexHealth sandbox: demo practice data." icon={ShieldCheck}>
            {" "}
            Writes are accepted but never reach an EHR, because the demo location has no sync.
            {d.clearinghouse?.env === "test" && " Eligibility uses Stedi test mode: each check is sent as Stedi's mock member and labelled Mock."}
          </Banner>
        )}
        {err && <Banner tone="red" title={err.message} />}

        <div className="grid-3" style={{ gridTemplateColumns: "repeat(4, minmax(0, 1fr))" }}>
          <Stat icon={CalendarClock} tint="#0071e3" value={h?.counts.scheduled} label="Visits scheduled" />
          <Stat icon={BadgeCheck} tint="#34c759" value={h?.counts.verified_active} label="Coverage verified active" />
          <Stat icon={AlertTriangle} tint="#ff9500" value={h?.counts.needs_attention} label="Need attention" />
          <Stat icon={CalendarX2} tint="#8e8e93" value={h?.counts.cancelled} label="Cancelled (backfill next)" />
        </div>

        <div className="split">
          <Card
            title="Front-desk huddle"
            subtitle={h ? `Insurance and eligibility for each visit · times in ${h.timezone}` : "Loading the schedule…"}
            actions={
              <Segmented
                label="Range"
                value={range}
                onChange={setRange}
                options={[
                  { value: "today", label: "Today" },
                  { value: "tomorrow", label: "Tomorrow" },
                  { value: "week", label: "7 days" },
                ]}
              />
            }
          >
            {h && h.appointments.length === 0 ? (
              <Empty icon={CalendarClock} title="No visits in this range">
                Sync the practice, or pick a longer range.
              </Empty>
            ) : (
              <ul className="list">
                {h?.appointments.map((a) => (
                  <HuddleItem
                    key={a.id}
                    a={a}
                    tz={h.timezone}
                    chart={d?.links && a.patient_id ? d.links.patient.replace("{pid}", a.patient_id) : null}
                    canRun={canRun}
                    checking={check.isPending && check.variables === a.id}
                    onCheck={() => check.mutate(a.id)}
                    onAlert={() => setAlertFor(a)}
                  />
                ))}
              </ul>
            )}
          </Card>

          <div style={{ display: "grid", gap: 20 }}>
            <ConnectionsCard d={d} isAdmin={isAdmin} verifying={verify.isPending} onVerify={(k) => verify.mutate(k)} />
            <FrontDeskMessages canRun={canRun} />
            <WriteQueue intents={d?.intents ?? []} isAdmin={isAdmin} onDone={refresh} />
            <Card title="Practice mirror" subtitle={`Allow-listed fields only; patient identifiers encrypted. ${d?.api_calls_total ?? 0} EHR API calls so far.`}>
              <ul className="list">
                {Object.entries(RESOURCE_LABEL).map(([key, label]) => {
                  const cursor = d?.cursors.find((c) => c.resource === key);
                  return (
                    <li className="list-row" key={key} style={{ minHeight: 40 }}>
                      <div className="row-main">
                        <div className="row-title">{label}</div>
                        {cursor?.last_error && <div className="row-sub field-error">{cursor.last_error}</div>}
                      </div>
                      <span className="small muted tabular">{cursor?.last_run_at ? timeAgo(cursor.last_run_at) : ""}</span>
                      <span className="tabular" style={{ fontWeight: 600, minWidth: 32, textAlign: "right" }}>
                        {d?.mirror[key] ?? 0}
                      </span>
                    </li>
                  );
                })}
              </ul>
            </Card>
          </div>
        </div>
      </div>
      {alertFor && <AlertSheet row={alertFor} onClose={() => setAlertFor(null)} onDone={refresh} />}
    </>
  );
}

function Stat({ icon: Icon, tint, value, label }: { icon: typeof CalendarClock; tint: string; value: number | undefined; label: string }) {
  return (
    <div className="card stat row" style={{ gap: 14 }}>
      <div className="row-icon" style={{ background: tint, width: 40, height: 40, borderRadius: 11 }}>
        <Icon size={20} aria-hidden />
      </div>
      <div>
        <div className="stat-value">{value ?? <span className="skeleton" style={{ display: "inline-block", width: 40, height: 26 }} />}</div>
        <div className="stat-label">{label}</div>
      </div>
    </div>
  );
}

function EligibilityPill({ e, status }: { e: EligibilityView | null; status: string }) {
  if (status === "cancelled") return <Pill tone="gray">Cancelled</Pill>;
  if (status === "deleted") return <Pill tone="gray">Deleted</Pill>;
  if (!e) return <Pill tone="gray" icon={CircleDashed}>Not checked</Pill>;
  if (e.state === "pending") return <Pill tone="blue" icon={CircleDashed}>Checking</Pill>;
  if (e.state === "failed") return <Pill tone="red" icon={XCircle}>Check failed</Pill>;
  if (e.state === "skipped") return <Pill tone="orange" icon={AlertTriangle}>Skipped</Pill>;
  const s = e.summary;
  if (s?.coverage === "active")
    return (
      <Pill tone="green" icon={CheckCircle2}>
        Active{s.office_copay ? ` · $${s.office_copay} copay` : ""}
      </Pill>
    );
  if (s?.coverage === "inactive") return <Pill tone="red" icon={XCircle}>Inactive</Pill>;
  return <Pill tone="orange" icon={AlertTriangle}>{s?.coverage ?? "Unknown"}</Pill>;
}

function HuddleItem({ a, tz, chart, canRun, checking, onCheck, onAlert }: { a: HuddleRow; tz: string; chart?: string | null; canRun: boolean; checking: boolean; onCheck: () => void; onAlert: () => void }) {
  const time = new Intl.DateTimeFormat(undefined, { timeZone: tz, weekday: "short", hour: "numeric", minute: "2-digit" }).format(new Date(a.start));
  const s = a.eligibility?.summary;
  const muted = a.status === "cancelled" || a.status === "deleted";
  return (
    <li className="list-row" style={{ alignItems: "flex-start", paddingTop: 14, paddingBottom: 14, opacity: muted ? 0.6 : 1 }}>
      <div className="tabular" style={{ width: 92, fontWeight: 600, paddingTop: 1 }}>
        {time}
      </div>
      <div className="row-main">
        <div className="row-title">
          {a.patient} · {a.type ?? "Visit"}
        </div>
        <div className="row-sub">
          {a.provider ?? "Provider"} · {a.coverage?.plan ?? "No insurance on file"}
          {a.coverage?.payer_id ? ` (payer ${a.coverage.payer_id})` : ""}
        </div>
        {s && a.eligibility?.state === "done" && (
          <div className="small muted" style={{ marginTop: 4 }}>
            {s.plan ?? s.ehr_plan}
            {s.deductible?.remaining ? ` · deductible left $${s.deductible.remaining} of $${s.deductible.total}` : ""}
            {s.office_coinsurance_pct ? ` · coinsurance ${s.office_coinsurance_pct}%` : ""}
            {a.eligibility.reused ? " · reused from today's check" : ""}
          </div>
        )}
        {a.eligibility?.state === "skipped" && <div className="small muted" style={{ marginTop: 4 }}>{a.eligibility.reason}</div>}
        <div className="row" style={{ flexWrap: "wrap", gap: 6, marginTop: 6 }}>
          <EligibilityPill e={a.eligibility} status={a.status} />
          {a.eligibility?.mock && <span className="sample-label">Mock payer</span>}
          {a.flags
            .filter((f) => f !== "not_checked")
            .map((f) => (
              <span className="chip" key={f}>
                {FLAG_LABEL[f] ?? f}
              </span>
            ))}
        </div>
      </div>
      {canRun && !muted && (
        <div className="row" style={{ gap: 6 }}>
          <button className="btn btn-tinted" onClick={onCheck} disabled={checking} title="Run an eligibility check now">
            {checking ? <span className="spinner" aria-label="Checking" /> : <ShieldCheck size={15} aria-hidden />} Check
          </button>
          {a.patient_id && (
            <button className="btn btn-plain btn-icon" onClick={onAlert} aria-label="Add chart alert" title="Add chart alert">
              <FilePenLine size={16} />
            </button>
          )}
          {chart && (
            <a className="btn btn-plain btn-icon" href={chart} target="_blank" rel="noreferrer" aria-label="Open chart in OpenEMR" title="Open chart in OpenEMR">
              <ExternalLink size={16} />
            </a>
          )}
        </div>
      )}
    </li>
  );
}

function ConnectionsCard({ d, isAdmin, verifying, onVerify }: { d?: PracticeOverview; isAdmin: boolean; verifying: boolean; onVerify: (k: "nexhealth" | "openemr" | "stedi") => void }) {
  return (
    <Card title="Connections" subtitle="Each is trusted only after a live check.">
      <ul className="list">
        {[d?.ehr, d?.clearinghouse].filter(Boolean).map((c) => (
          <ConnectionRow key={c!.id} c={c!} isAdmin={isAdmin} verifying={verifying} onVerify={() => onVerify(c!.kind)} />
        ))}
        {!d?.clearinghouse && (
          <li className="list-row">
            <div className="row-main">
              <div className="row-title">Stedi</div>
              <div className="row-sub">
                Not connected · run <code className="mono">make connect-stedi</code>
              </div>
            </div>
          </li>
        )}
      </ul>
    </Card>
  );
}

function ConnectionRow({ c, isAdmin, verifying, onVerify }: { c: PracticeConnection; isAdmin: boolean; verifying: boolean; onVerify: () => void }) {
  const cap = c.capability ?? {};
  const reads = Object.values(cap.reads ?? {});
  const sync = cap.sync ?? [];
  return (
    <li className="list-row" style={{ alignItems: "flex-start", paddingTop: 12, paddingBottom: 12 }}>
      <div className="row-main">
        <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
          <span style={{ fontWeight: 600 }}>{{ nexhealth: "NexHealth", openemr: "OpenEMR", stedi: "Stedi" }[c.kind]}</span>
          <span className="chip">{c.env}</span>
          {c.verified_at ? (
            <Pill tone="green" icon={ShieldCheck}>
              Verified {timeAgo(c.verified_at)}
            </Pill>
          ) : (
            <Pill tone="orange" icon={CircleDashed}>
              Not verified
            </Pill>
          )}
        </div>
        <div className="row-sub" style={{ marginTop: 4 }}>
          {c.kind !== "stedi" ? (
            <>
              {cap.emrs?.length ? `EHR ${cap.emrs.join(", ")} · ` : ""}
              {reads.length ? `${reads.filter((r) => r.ok).length}/${reads.length} reads answer · ` : ""}
              {sync.length ? `sync read ${sync[0].read_status}, write ${sync[0].write_status}` : "no live sync (writes unconfirmable)"}
              {cap.last_sync ? ` · synced ${timeAgo(cap.last_sync.at)}` : ""}
              {` · webhooks ${c.webhook.registered ? "on" : "off"}`}
            </>
          ) : (
            <>Eligibility 270/271 · payer directory</>
          )}
        </div>
        {c.last_error && <div className="field-error" style={{ marginTop: 4 }}>{c.last_error}</div>}
      </div>
      {isAdmin && (
        <button className="btn btn-tinted" onClick={onVerify} disabled={verifying}>
          Verify
        </button>
      )}
    </li>
  );
}

const INTENT_TONE: Record<WriteIntentRow["state"], "green" | "orange" | "red" | "blue" | "gray"> = {
  proposed: "orange",
  approved: "blue",
  sending: "blue",
  accepted: "green",
  written: "green",
  needs_review: "red",
  manual: "orange",
  rejected: "gray",
  failed: "red",
};

const KIND_LABEL: Record<string, string> = {
  patient_alert: "Chart alert",
  appointment_book: "Book appointment",
  appointment_cancel: "Cancel appointment",
};

const TOPIC_LABEL: Record<FrontDeskMessage["topic"], string> = {
  refill: "Refill request",
  records: "Records request",
  billing: "Billing question",
  scheduling: "Scheduling",
  new_patient: "New patient",
  insurance: "Insurance",
  other: "Message",
};

function FrontDeskMessages({ canRun }: { canRun: boolean }) {
  const toast = useToast();
  const qc = useQueryClient();
  const messages = useQuery({ queryKey: ["front-desk-messages"], queryFn: () => api.get<FrontDeskMessage[]>("/practices/messages"), refetchInterval: 15000 });
  const [open, setOpen] = useState<FrontDeskMessage | null>(null);
  const reveal = useMutation({
    mutationFn: (id: string) => api.get<FrontDeskMessage>(`/practices/messages/${id}`),
    onSuccess: setOpen,
    onError: (e) => toast(e instanceof ApiError ? e.message : "Could not open the message"),
  });
  const done = useMutation({
    mutationFn: (id: string) => api.post<FrontDeskMessage>(`/practices/messages/${id}/done`),
    onSuccess: () => {
      setOpen(null);
      toast("Marked as handled");
      qc.invalidateQueries({ queryKey: ["front-desk-messages"] });
    },
  });
  const rows = messages.data ?? [];
  return (
    <Card title="Front desk messages" subtitle="Taken by the receptionist on calls. Opening a message is logged.">
      {rows.length === 0 ? (
        <div className="card-pad small muted">No open messages.</div>
      ) : (
        <ul className="list">
          {rows.slice(0, 8).map((m) => (
            <li className="list-row" key={m.id}>
              <div className="row-icon" style={{ background: m.callback_requested ? "#ff9500" : "#8e8e93" }}>
                {m.callback_requested ? <PhoneCall size={16} /> : <MessageSquareText size={16} />}
              </div>
              <div className="row-main">
                <div className="row-title">{TOPIC_LABEL[m.topic]}</div>
                <div className="row-sub">
                  {m.verified ? `Verified patient ${m.patient ?? ""}` : "Caller not verified"} · {m.callback_requested ? "wants a callback" : "no callback"} · {timeAgo(m.created_at)}
                </div>
              </div>
              {canRun && (
                <button className="btn btn-secondary" onClick={() => reveal.mutate(m.id)} disabled={reveal.isPending}>
                  Open
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {open && (
        <Sheet
          title={TOPIC_LABEL[open.topic]}
          subtitle={`Reference ${open.reference} · ${timeAgo(open.created_at)}`}
          onClose={() => setOpen(null)}
          footer={
            <button className="btn btn-primary" onClick={() => done.mutate(open.id)} disabled={done.isPending || open.state === "done"}>
              {open.state === "done" ? "Handled" : "Mark handled"}
            </button>
          }
        >
          <div style={{ display: "grid", gap: 12 }}>
            <p style={{ margin: 0, whiteSpace: "pre-wrap" }}>{open.message}</p>
            <div className="small muted">
              {open.verified ? `Verified patient ${open.patient ?? ""} (ID ${open.patient_id})` : `Not verified${open.caller_name ? ` · says they are ${open.caller_name}` : ""}`}
              {open.caller_phone ? ` · called from ${open.caller_phone}` : ""}
              {open.callback_requested ? " · wants a callback" : ""}
            </div>
            {!open.verified && <Banner tone="orange" title="Unverified caller">Confirm who they are before discussing anything about a patient.</Banner>}
          </div>
        </Sheet>
      )}
    </Card>
  );
}

function WriteQueue({ intents, isAdmin, onDone }: { intents: WriteIntentRow[]; isAdmin: boolean; onDone: () => void }) {
  const toast = useToast();
  const decide = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: "approve" | "reject" }) => api.post<WriteIntentRow>(`/practices/intents/${id}/${decision}`),
    onSuccess: (r) => {
      toast(r.state === "accepted" ? "Accepted by the EHR" : r.state === "manual" ? "This EHR can't take that write: staff task created" : r.state === "written" ? "Written to the EHR" : `Write ${r.state.replace("_", " ")}`);
      onDone();
    },
  });
  return (
    <Card title="EHR write queue" subtitle="Every write is approved, sent once, and tracked until the EHR confirms it.">
      {intents.length === 0 ? (
        <div className="card-pad small muted">No writes yet.</div>
      ) : (
        <ul className="list">
          {intents.slice(0, 8).map((i) => (
            <li className="list-row" key={i.id} style={{ alignItems: "flex-start", paddingTop: 12, paddingBottom: 12 }}>
              <div className="row-main">
                <div className="row-title">
                  {KIND_LABEL[i.kind] ?? i.kind}
                  {i.source === "receptionist" && (
                    <span className="small muted" style={{ fontWeight: 400 }}>
                      {" "}
                      · from a call
                    </span>
                  )}
                </div>
                <div className="row-sub">
                  {i.reason} · {timeAgo(i.created_at)}
                  {i.confirmation === "not_reported" ? " · EHR confirmation not reported for this kind" : ""}
                </div>
                {i.last_error && <div className="field-error">{i.last_error}</div>}
              </div>
              {i.state === "proposed" && isAdmin ? (
                <div className="row" style={{ gap: 6 }}>
                  <button className="btn btn-primary" onClick={() => decide.mutate({ id: i.id, decision: "approve" })} disabled={decide.isPending}>
                    Approve
                  </button>
                  <button className="btn btn-secondary" onClick={() => decide.mutate({ id: i.id, decision: "reject" })} disabled={decide.isPending}>
                    Reject
                  </button>
                </div>
              ) : (
                <Pill tone={INTENT_TONE[i.state]}>{i.state.replace("_", " ")}</Pill>
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function AlertSheet({ row, onClose, onDone }: { row: HuddleRow; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [practice] = usePractice();
  const [note, setNote] = useState("");
  const save = useMutation({
    mutationFn: () => api.post<WriteIntentRow>(withPractice("/practices/intents", practice), { kind: "patient_alert", patient_id: row.patient_id, note }),
    onSuccess: () => {
      toast("Chart alert proposed. Approve it in the EHR write queue.");
      onDone();
      onClose();
    },
  });
  return (
    <Sheet
      title="Add chart alert"
      subtitle={`For ${row.patient}. Proposed first; it is written only after approval.`}
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" onClick={() => save.mutate()} disabled={!note.trim() || save.isPending}>
            Propose alert
          </button>
        </>
      }
    >
      <label className="field">
        <span className="field-label">Alert text</span>
        <textarea className="input" rows={3} value={note} onChange={(e) => setNote(e.target.value)} placeholder="Please bring the new insurance card." autoFocus />
        <span className="field-help">Synthetic data only. Do not enter real patient information.</span>
      </label>
      {save.error && <Banner tone="red" title={(save.error as ApiError).message} />}
    </Sheet>
  );
}
