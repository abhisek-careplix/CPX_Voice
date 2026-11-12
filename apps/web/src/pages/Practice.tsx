import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import {
  AlertTriangle,
  BadgeCheck,
  CalendarClock,
  CalendarX2,
  CheckCircle2,
  CircleDashed,
  Database,
  FilePenLine,
  HeartPulse,
  MessageSquareText,
  PhoneCall,
  ExternalLink,
  ShieldCheck,
  Stethoscope,
  XCircle,
} from "lucide-react";
import { useState } from "react";
import { api, ApiError, type EligibilitySettings, type EligibilityView, type FrontDeskMessage, type PayerMappingRow, type Huddle, type HuddleRow, type NpiLookup, type PracticeConnection, type PracticeOverview, type Program, type WriteIntentRow } from "../api";
import { useCan } from "../App";
import { PRACTICE_LABEL, usePractice, withPractice } from "../practice";
import { Banner, Card, Empty, ListSkeleton, Pill, Segmented, Sheet, Toolbar, timeAgo, useToast } from "../ui";
import MedicareBenefitsSheet from "./MedicareBenefits";

type Range = "today" | "tomorrow" | "week";

const FLAG_LABEL: Record<string, string> = {
  asap_list: "Wants sooner",
  no_insurance_on_file: "No insurance on file",
  not_checked: "Not checked",
  check_failed: "Check failed",
  coverage_inactive: "Coverage inactive",
  auth_required: "Auth required",
  check_pending: "Retrying",
  payer_unmapped: "Confirm payer",
  coverage_redirect: "Other plan responsible",
  card_reported_stolen: "Card reported stolen",
};

const REDIRECT_LABEL: Record<string, string> = {
  medicare_advantage: "Medicare Advantage",
  managed_care: "Medicaid managed care",
  contact_payer: "Contact payer",
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

type Tab = "schedule" | "inbox" | "setup";

const PROGRAM_LABEL: Record<Program, string> = {
  medicare: "Original Medicare",
  railroad_medicare: "Railroad Medicare",
  medicare_advantage: "Medicare Advantage",
  medigap: "Medicare supplement",
  part_d: "Medicare Part D",
  medicaid: "Medicaid",
  commercial: "Commercial",
};

/** Programs whose Part A/B benefits CMS answers for: Original and Railroad Medicare. Anyone else
 * can still be checked with Medicare (test mode always sends Stedi's CMS mock member). */
const CMS_PROGRAMS: Program[] = ["medicare", "railroad_medicare"];

export default function Practice() {
  const isAdmin = useCan("admin");
  const canRun = useCan("start_task");
  const qc = useQueryClient();
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const tab = (["schedule", "inbox", "setup"].includes(params.get("tab") ?? "") ? params.get("tab") : "schedule") as Tab;
  const setTab = (t: Tab) => setParams(t === "schedule" ? {} : { tab: t }, { replace: true });
  const [range, setRange] = useState<Range>("week");
  const [alertFor, setAlertFor] = useState<HuddleRow | null>(null);
  const [benefitsFor, setBenefitsFor] = useState<HuddleRow | null>(null);
  const [practice, setPractice] = usePractice();
  const at = (path: string) => withPractice(path, practice);
  const overview = useQuery({ queryKey: ["practice", practice], queryFn: () => api.get<PracticeOverview>(at("/practices")), refetchInterval: 5000 });
  const d = overview.data;
  const tz = useQuery({ queryKey: ["practice-huddle", practice, "today"], queryFn: () => api.get<Huddle>(at("/practices/huddle?days=1")), enabled: !!d?.ehr }).data?.timezone;
  const rp = rangeParams(range, tz);
  const query = new URLSearchParams({ days: String(rp.days), ...(rp.date ? { date: rp.date } : {}) });
  const huddle = useQuery({
    queryKey: ["practice-huddle", practice, range, rp.date],
    queryFn: () => api.get<Huddle>(at(`/practices/huddle?${query}`)),
    enabled: !!d?.ehr,
    refetchInterval: 5000,
  });
  const settings = useQuery({ queryKey: ["eligibility-settings"], queryFn: () => api.get<EligibilitySettings>("/practices/eligibility/settings"), enabled: !!d?.clearinghouse });
  const mappings = useQuery({ queryKey: ["payer-mappings"], queryFn: () => api.get<{ payers: PayerMappingRow[] }>("/practices/payers"), enabled: !!d?.clearinghouse, refetchInterval: 15000 });
  const messages = useQuery({ queryKey: ["front-desk-messages"], queryFn: () => api.get<FrontDeskMessage[]>("/practices/messages"), refetchInterval: 15000 });
  const refresh = () => qc.invalidateQueries({ queryKey: ["practice"] }).then(() => qc.invalidateQueries({ queryKey: ["practice-huddle"] }));
  const sync = useMutation({
    mutationFn: (full: boolean) => api.post<{ api_calls: number; eligibility_queued: number }>(at(`/practices/sync?full=${full}`)),
    onSuccess: (r) => {
      toast(`Synced · ${r.api_calls} EHR API calls · ${r.eligibility_queued} eligibility checks queued`);
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

  const err = (sync.error ?? check.error) as ApiError | null;
  const todo = setupTodo(d, settings.data, mappings.data?.payers);
  const inbox = (messages.data?.length ?? 0) + (d?.intents.filter((i) => i.state === "proposed").length ?? 0);

  if (overview.isSuccess && !d?.ehr) {
    return (
      <>
        <Toolbar eyebrow="Front office" title="Practice" description="Today's schedule, coverage checks and front-desk messages from your connected practice system." />
        <div className="content">
          <Card>
            <Empty icon={Stethoscope} title="No practice connected" action={<Link to="/hub" className="btn btn-secondary">Open EHR hub</Link>}>
              Connect a practice management system or EHR (and a clearinghouse for eligibility) to see the day here. Your CarePlix team sets up the connection.
            </Empty>
          </Card>
        </div>
      </>
    );
  }

  const h = huddle.data;
  const demo = d?.ehr?.env === "sandbox" || d?.ehr?.kind === "openemr" || d?.clearinghouse?.env === "test";
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
            {isAdmin && (
              <button className="btn btn-primary" onClick={() => sync.mutate(false)} disabled={sync.isPending}>
                {sync.isPending ? <span className="spinner" aria-label="Syncing" /> : <Database size={15} aria-hidden />} Sync now
              </button>
            )}
          </>
        }
      />
      <div className="content">
        {demo && (
          <div className="row small muted" style={{ gap: 8, flexWrap: "wrap" }}>
            <ShieldCheck size={15} aria-hidden />
            <span>
              Demo data:{" "}
              {[
                d?.ehr?.env === "sandbox" && "NexHealth sandbox (writes never reach an EHR)",
                d?.ehr?.kind === "openemr" && "OpenEMR with a synthetic clinic",
                d?.clearinghouse?.env === "test" && "Stedi test mode (checks go out as Stedi's mock members, labelled Mock)",
              ]
                .filter(Boolean)
                .join(" · ")}
            </span>
          </div>
        )}
        {err && <Banner tone="red" title={err.message} />}
        {isAdmin && todo.count > 0 && tab !== "setup" && (
          <div className="banner tone-orange" role="status" style={{ alignItems: "center", justifyContent: "space-between" }}>
            <span style={{ color: "var(--label)" }}>
              <span style={{ fontWeight: 600 }}>Finish setup:</span> {todo.items.join(" · ")}
            </span>
            <button className="btn btn-secondary" onClick={() => setTab("setup")}>
              Open setup
            </button>
          </div>
        )}

        <div className="grid-3" style={{ gridTemplateColumns: "repeat(4, minmax(0, 1fr))" }}>
          <Stat icon={CalendarClock} tint="var(--tile-blue)" value={h?.counts.scheduled} label="Visits scheduled" />
          <Stat icon={BadgeCheck} tint="var(--tile-green)" value={h?.counts.verified_active} label="Coverage verified active" />
          <Stat icon={AlertTriangle} tint="var(--tile-orange)" value={h?.counts.needs_attention} label="Need attention" />
          <Stat icon={CalendarX2} tint="var(--tile-gray)" value={h?.counts.cancelled} label="Cancelled (backfill next)" />
        </div>

        <Segmented
          label="Section"
          value={tab}
          onChange={setTab}
          options={[
            { value: "schedule", label: "Schedule" },
            { value: "inbox", label: inbox ? `Inbox (${inbox})` : "Inbox" },
            ...(isAdmin ? [{ value: "setup" as Tab, label: todo.count ? `Setup (${todo.count})` : "Setup" }] : []),
          ]}
        />

        {tab === "schedule" && (
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
            {!h ? (
              <ListSkeleton rows={4} />
            ) : h.appointments.length === 0 ? (
              <Empty icon={CalendarClock} title="No visits in this range">
                Sync the practice, or pick a longer range.
              </Empty>
            ) : (
              <ul className="list">
                {h.appointments.map((a) => (
                  <HuddleItem
                    key={a.id}
                    a={a}
                    tz={h.timezone}
                    chart={d?.links && a.patient_id ? d.links.patient.replace("{pid}", a.patient_id) : null}
                    canRun={canRun}
                    checking={check.isPending && check.variables === a.id}
                    onCheck={() => check.mutate(a.id)}
                    onAlert={() => setAlertFor(a)}
                    onBenefits={d?.clearinghouse && (settings.data?.mode === "test" || CMS_PROGRAMS.includes(a.coverage?.program as Program)) ? () => setBenefitsFor(a) : undefined}
                  />
                ))}
              </ul>
            )}
          </Card>
        )}

        {tab === "inbox" && (
          <div className="split">
            <FrontDeskMessages canRun={canRun} rows={messages.data} />
            <WriteQueue intents={d?.intents ?? []} isAdmin={isAdmin} onDone={refresh} />
          </div>
        )}

        {tab === "setup" && isAdmin && (
          <Setup d={d} settings={settings.data} payers={mappings.data?.payers} onChanged={refresh} />
        )}
      </div>
      {alertFor && <AlertSheet row={alertFor} onClose={() => setAlertFor(null)} onDone={refresh} />}
      {benefitsFor?.patient_id && <MedicareBenefitsSheet patientId={benefitsFor.patient_id} patient={benefitsFor.patient} appointmentId={benefitsFor.id} onClose={() => setBenefitsFor(null)} />}
    </>
  );
}

/** What still needs a person before checks run cleanly, in the fewest words. */
function setupTodo(d: PracticeOverview | undefined, s: EligibilitySettings | undefined, payers: PayerMappingRow[] | undefined): { items: string[]; count: number } {
  const out: string[] = [];
  const unverified = [d?.ehr, d?.clearinghouse].filter((c) => c && !c.verified_at).length;
  if (unverified) out.push(unverified === 1 ? "verify a connection" : `verify ${unverified} connections`);
  if (s && s.mode === "production" && !s.effective_npi) out.push("add the billing NPI");
  const choose = payers?.filter((p) => p.state === "ambiguous" || p.state === "not_found" || p.state === "error").length ?? 0;
  if (choose) out.push(choose === 1 ? "confirm 1 payer" : `confirm ${choose} payers`);
  return { items: out, count: unverified + (out.includes("add the billing NPI") ? 1 : 0) + choose };
}

function Stat({ icon: Icon, tint, value, label }: { icon: typeof CalendarClock; tint: string; value: number | undefined; label: string }) {
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

function EligibilityPill({ e, status }: { e: EligibilityView | null; status: string }) {
  if (status === "cancelled") return <Pill tone="gray">Cancelled</Pill>;
  if (status === "deleted") return <Pill tone="gray">Deleted</Pill>;
  if (!e) return <Pill tone="gray" icon={CircleDashed}>Not checked</Pill>;
  if (e.state === "pending") return <Pill tone="blue" icon={CircleDashed}>{e.attempts ? `Retrying (${e.attempts})` : "Checking"}</Pill>;
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

function HuddleItem({
  a,
  tz,
  chart,
  canRun,
  checking,
  onCheck,
  onAlert,
  onBenefits,
}: {
  a: HuddleRow;
  tz: string;
  chart?: string | null;
  canRun: boolean;
  checking: boolean;
  onCheck: () => void;
  onAlert: () => void;
  onBenefits?: () => void;
}) {
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
            {s.deductible?.remaining ? ` · deductible left $${s.deductible.remaining}${s.deductible.total ? ` of $${s.deductible.total}` : ""}` : ""}
            {s.deductible_family?.remaining ? ` · family deductible left $${s.deductible_family.remaining}` : ""}
            {s.office_coinsurance_pct ? ` · coinsurance ${s.office_coinsurance_pct}%` : ""}
            {s.medicare?.qmb ? " · QMB: no Medicare cost sharing" : ""}
            {s.medicaid?.spend_down ? ` · spend down $${s.medicaid.spend_down}` : ""}
            {a.eligibility.reused ? " · reused from today's check" : ""}
          </div>
        )}
        {s?.redirect && a.eligibility?.state === "done" && (
          <div className="small" style={{ marginTop: 4 }}>
            {REDIRECT_LABEL[s.redirect.kind] ?? "Other plan"}: {s.redirect.name ?? "another plan"}
            {s.redirect.payer_id ? ` (payer ${s.redirect.payer_id})` : ""}
            {s.redirect.phone ? ` · ${s.redirect.phone}` : ""} · re-check with that plan
          </div>
        )}
        {(a.eligibility?.state === "skipped" || a.eligibility?.state === "failed" || (a.eligibility?.state === "pending" && a.eligibility.attempts > 0)) && (
          <div className="small muted" style={{ marginTop: 4 }}>{a.eligibility.reason}</div>
        )}
        <div className="row" style={{ flexWrap: "wrap", gap: 6, marginTop: 6 }}>
          <EligibilityPill e={a.eligibility} status={a.status} />
          {a.coverage?.program && a.coverage.program !== "commercial" && <span className="chip">{PROGRAM_LABEL[a.coverage.program]}</span>}
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
          {a.patient_id && onBenefits && (
            <button className="btn btn-secondary" onClick={onBenefits} title="Medicare benefits: coverage, what's used and left, preventive dates">
              <HeartPulse size={15} aria-hidden /> Benefits
            </button>
          )}
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
                Not connected · ask your CarePlix team to connect a clearinghouse
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
        {c.webhook.health && !["ok", "not_registered"].includes(c.webhook.health.status) && (
          <div className="field-error" style={{ marginTop: 4 }}>
            Webhooks: {c.webhook.health.message ?? c.webhook.health.status}
          </div>
        )}
      </div>
      {isAdmin && (
        <button className="btn btn-tinted" onClick={onVerify} disabled={verifying}>
          Verify
        </button>
      )}
    </li>
  );
}

const MAPPING_TONE: Record<PayerMappingRow["state"], "green" | "orange" | "red" | "blue" | "gray"> = {
  resolved: "green",
  confirmed: "green",
  ambiguous: "orange",
  not_found: "red",
  error: "red",
  new: "gray",
};

function Setup({ d, settings, payers, onChanged }: { d?: PracticeOverview; settings?: EligibilitySettings; payers?: PayerMappingRow[]; onChanged: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [practice] = usePractice();
  const verify = useMutation({
    mutationFn: (kind: "nexhealth" | "openemr" | "stedi") => api.post<{ ok: boolean; error?: string }>(withPractice(`/practices/verify?kind=${kind}`, practice)),
    onSuccess: (r) => {
      toast(r.ok ? "Verified with a live check" : `Verification failed: ${r.error ?? ""}`);
      onChanged();
    },
  });
  const changed = () => {
    qc.invalidateQueries({ queryKey: ["eligibility-settings"] });
    qc.invalidateQueries({ queryKey: ["payer-mappings"] });
  };
  return (
    <div className="split">
      <div style={{ display: "grid", gap: 20, alignContent: "start" }}>
        {d?.clearinghouse && <NpiCard s={settings} onSaved={changed} />}
        {d?.clearinghouse && <PayersCard rows={payers} onChanged={changed} />}
      </div>
      <div style={{ display: "grid", gap: 20, alignContent: "start" }}>
        <ConnectionsCard d={d} isAdmin verifying={verify.isPending} onVerify={(k) => verify.mutate(k)} />
        {verify.error && <Banner tone="red" title={(verify.error as ApiError).message} />}
        {settings?.mode === "test" && <TestScenarioCard s={settings} onSaved={changed} />}
        <SyncCard d={d} />
      </div>
    </div>
  );
}

/** The billing NPI: pick a provider the practice system already knows, or type one and see who
 * NPPES says it belongs to before saving. */
function NpiCard({ s, onSaved }: { s?: EligibilitySettings; onSaved: () => void }) {
  const toast = useToast();
  const [draft, setDraft] = useState("");
  const typed = draft.length === 10 ? draft : null;
  const lookup = useQuery({
    queryKey: ["npi-lookup", typed],
    queryFn: () => api.get<NpiLookup>(`/practices/eligibility/npi/${typed}`),
    enabled: !!typed,
    retry: false,
    staleTime: 3_600_000,
  });
  const current = useQuery({
    queryKey: ["npi-lookup", s?.effective_npi],
    queryFn: () => api.get<NpiLookup>(`/practices/eligibility/npi/${s!.effective_npi}`),
    enabled: !!s?.effective_npi,
    retry: false,
    staleTime: 3_600_000,
  });
  const save = useMutation({
    mutationFn: (billing_npi: string | null) => api.put<EligibilitySettings>("/practices/eligibility/settings", { billing_npi }),
    onSuccess: (r) => {
      toast(r.billing_npi ? `Billing NPI set to ${r.billing_npi}` : "Billing NPI cleared");
      setDraft("");
      onSaved();
    },
  });
  const test = s?.mode === "test";
  const err = (save.error ?? lookup.error) as ApiError | null;
  const others = (s?.providers ?? []).filter((p) => p.npi !== s?.effective_npi);
  return (
    <Card title="Billing NPI" subtitle="Sent on every eligibility check, including Medicare. Payers and CMS look the provider up by this number.">
      <div style={{ display: "grid", gap: 12 }}>
        {s?.effective_npi ? (
          <div className="row" style={{ gap: 10, alignItems: "flex-start" }}>
            <CheckCircle2 size={18} style={{ color: "var(--green)", flex: "none", marginTop: 2 }} aria-hidden />
            <div className="row-main">
              <div style={{ fontWeight: 600 }}>
                {s.effective_npi}
                {current.data?.name ? ` · ${current.data.name}` : ""}
              </div>
              <div className="small muted">
                <NpiFacts r={current.data} />
                {s.npi_source === "deployment" ? " · set by your CarePlix team" : ""}
              </div>
            </div>
            {s.npi_source === "workspace" && (
              <button className="btn btn-plain" onClick={() => save.mutate(null)} disabled={save.isPending}>
                Clear
              </button>
            )}
          </div>
        ) : (
          <div className="small muted">{test ? "Not needed in test mode (Stedi's mock provider is sent). Required before live checks." : "Not set: live eligibility and Medicare checks are skipped until you add one."}</div>
        )}

        {others.length > 0 && (
          <div>
            <div className="field-label" style={{ marginBottom: 6 }}>
              From your practice system
            </div>
            <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
              {others.map((p) => (
                <button key={p.npi} className="btn btn-tinted" disabled={save.isPending} onClick={() => save.mutate(p.npi)} title={`Use ${p.npi}`}>
                  {p.name} · {p.npi}
                </button>
              ))}
            </div>
          </div>
        )}

        <label className="field">
          <span className="field-label">{s?.effective_npi ? "Use a different NPI" : "Enter the NPI"}</span>
          <div className="row" style={{ gap: 6 }}>
            <input className="input" inputMode="numeric" maxLength={10} value={draft} placeholder="10-digit NPI" onChange={(e) => setDraft(e.target.value.replace(/\D/g, ""))} />
            <button className="btn btn-primary" disabled={!typed || save.isPending || lookup.data?.status === "invalid"} onClick={() => save.mutate(typed)}>
              Save
            </button>
          </div>
          <span className="field-help">
            {typed && lookup.isLoading ? (
              "Looking it up in the NPI Registry…"
            ) : typed && lookup.data ? (
              lookup.data.status === "confirmed" ? (
                <>
                  <strong>{lookup.data.name}</strong> · <NpiFacts r={lookup.data} />
                </>
              ) : lookup.data.status === "invalid" ? (
                `Not usable: ${lookup.data.reason ?? "not active in the NPI Registry"}.`
              ) : lookup.data.status === "unavailable" ? (
                "The NPI Registry didn't answer; you can still save it."
              ) : (
                "Synthetic NPI (test range): not looked up."
              )
            ) : (
              "Use the practice's organization NPI (Type 2) if it has one, else the billing provider's own NPI. Find it at npiregistry.cms.hhs.gov."
            )}
          </span>
        </label>
        {err && <div className="field-error">{err.message}</div>}
      </div>
    </Card>
  );
}

function NpiFacts({ r }: { r?: NpiLookup }) {
  if (!r || r.status !== "confirmed") return null;
  return <>{[r.kind === "organization" ? "Organization (Type 2)" : r.kind === "individual" ? "Individual (Type 1)" : null, r.taxonomy, [r.city, r.state].filter(Boolean).join(", ")].filter(Boolean).join(" · ")}</>;
}

function TestScenarioCard({ s, onSaved }: { s: EligibilitySettings; onSaved: () => void }) {
  const save = useMutation({
    mutationFn: (test_scenario: string) => api.put<EligibilitySettings>("/practices/eligibility/settings", { test_scenario }),
    onSuccess: onSaved,
  });
  const groups = Array.from(new Set(s.scenarios.map((o) => o.group ?? "")));
  return (
    <Card title="Test mode" subtitle="Stedi only answers for its published mock members. Pick which one every check sends.">
      <label className="field">
        <span className="field-label">Mock member</span>
        <select className="select" disabled={save.isPending} value={s.test_scenario} onChange={(e) => save.mutate(e.target.value)}>
          {groups.map((g) =>
            g ? (
              <optgroup key={g} label={g}>
                {s.scenarios.filter((o) => o.group === g).map((o) => (
                  <option key={o.key} value={o.key}>
                    {o.label}
                  </option>
                ))}
              </optgroup>
            ) : (
              s.scenarios.filter((o) => !o.group).map((o) => (
                <option key={o.key} value={o.key}>
                  {o.label}
                </option>
              ))
            ),
          )}
        </select>
        <span className="field-help">No patient data leaves CarePlix in test mode.</span>
      </label>
    </Card>
  );
}

function PayersCard({ rows, onChanged }: { rows?: PayerMappingRow[]; onChanged: () => void }) {
  const toast = useToast();
  const [manual, setManual] = useState<Record<string, string>>({});
  const [pins, setPins] = useState<Record<string, string>>({});
  const confirm = useMutation({
    mutationFn: ({ id, payer_id }: { id: string; payer_id: string }) => api.post<PayerMappingRow>(`/practices/payers/${id}/confirm`, { payer_id }),
    onSuccess: (r) => {
      toast(`Payer set to ${r.stedi_payer_id}`);
      onChanged();
    },
  });
  const pin = useMutation({
    mutationFn: ({ id, value }: { id: string; value: string | null }) => api.put<PayerMappingRow>(`/practices/payers/${id}/pin`, { pin: value }),
    onSuccess: (r) => {
      toast(r.has_pin ? "Provider PIN saved (encrypted)" : "Provider PIN removed");
      setPins({});
      onChanged();
    },
  });
  const all = rows ?? [];
  const needs = all.filter((m) => !["resolved", "confirmed", "new"].includes(m.state) || m.enrollment?.detail);
  const fine = all.filter((m) => !needs.includes(m));
  const err = (confirm.error ?? pin.error) as ApiError | null;
  const row = (m: PayerMappingRow) => (
    <li className="list-row" key={m.id} style={{ alignItems: "flex-start", paddingTop: 10, paddingBottom: 10 }}>
      <div className="row-main">
        <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
          <span style={{ fontWeight: 600 }}>{m.ehr_name ?? m.ehr_payer_id ?? "Payer"}</span>
          <Pill tone={MAPPING_TONE[m.state] ?? "gray"}>{m.state === "ambiguous" ? "Needs a choice" : m.state.replace("_", " ")}</Pill>
          {m.plan_kind && m.plan_kind !== "commercial" && <span className="chip">{PROGRAM_LABEL[m.plan_kind as Program] ?? m.plan_kind.replace("_", " ")}</span>}
        </div>
        <div className="row-sub" style={{ marginTop: 4 }}>
          {m.stedi_payer_id ? `Stedi ${m.stedi_payer_id}${m.stedi_name ? ` · ${m.stedi_name}` : ""}` : "No Stedi payer yet"}
          {m.enrollment_required ? " · enrollment required" : ""}
          {m.enrollment ? ` · NPI ${m.enrollment.status.replace("_", " ")}` : ""}
          {m.has_pin ? " · PIN set" : ""}
        </div>
        {m.enrollment?.detail && <div className="field-error" style={{ marginTop: 4 }}>{m.enrollment.detail}</div>}
        {m.last_error && <div className="field-error" style={{ marginTop: 4 }}>{m.last_error}</div>}
        {m.state === "ambiguous" && m.candidates.length > 0 && (
          <div className="row" style={{ flexWrap: "wrap", gap: 6, marginTop: 6 }}>
            {m.candidates.map((c) => (
              <button key={c.payer_id} className="btn btn-tinted" disabled={confirm.isPending} onClick={() => confirm.mutate({ id: m.id, payer_id: c.payer_id })}>
                {c.name ?? c.payer_id} ({c.payer_id})
              </button>
            ))}
          </div>
        )}
        {(m.state === "ambiguous" || m.state === "not_found" || m.state === "error") && (
          <div className="row" style={{ gap: 6, marginTop: 6 }}>
            <input className="input" style={{ maxWidth: 160 }} placeholder="Stedi payer ID" value={manual[m.id] ?? ""} onChange={(e) => setManual({ ...manual, [m.id]: e.target.value })} />
            <button className="btn btn-secondary" disabled={!manual[m.id] || confirm.isPending} onClick={() => confirm.mutate({ id: m.id, payer_id: manual[m.id] })}>
              Use this ID
            </button>
          </div>
        )}
        {m.programs.includes("MEDICAID") && (
          <div className="row" style={{ gap: 6, marginTop: 6 }}>
            <input
              className="input"
              type="password"
              autoComplete="off"
              style={{ maxWidth: 160 }}
              placeholder={m.has_pin ? "PIN set (hidden)" : "Medicaid provider PIN"}
              value={pins[m.id] ?? ""}
              onChange={(e) => setPins({ ...pins, [m.id]: e.target.value })}
            />
            <button className="btn btn-secondary" disabled={!pins[m.id] || pin.isPending} onClick={() => pin.mutate({ id: m.id, value: pins[m.id] })}>
              Save PIN
            </button>
            {m.has_pin && (
              <button className="btn btn-plain" disabled={pin.isPending} onClick={() => pin.mutate({ id: m.id, value: null })}>
                Remove
              </button>
            )}
          </div>
        )}
      </div>
    </li>
  );
  return (
    <Card title="Payers" subtitle="Each insurance plan in your practice system, matched to the payer Stedi sends the check to. Medicare goes to CMS; Medicare Advantage to the plan.">
      {rows === undefined ? (
        <ListSkeleton rows={2} icon={false} />
      ) : all.length === 0 ? (
        <div className="card-pad small muted">Payers appear here after the first eligibility check.</div>
      ) : (
        <>
          {needs.length > 0 ? <ul className="list">{needs.map(row)}</ul> : <div className="card-pad small muted">All {fine.length} payers are matched.</div>}
          {needs.length > 0 && fine.length > 0 && (
            <details className="card-pad">
              <summary className="small" style={{ cursor: "pointer" }}>
                {fine.length} matched payer{fine.length === 1 ? "" : "s"}
              </summary>
              <ul className="list">{fine.map(row)}</ul>
            </details>
          )}
          {needs.length === 0 && fine.length > 0 && (
            <details className="card-pad" style={{ paddingTop: 0 }}>
              <summary className="small" style={{ cursor: "pointer" }}>
                Show them
              </summary>
              <ul className="list">{fine.map(row)}</ul>
            </details>
          )}
        </>
      )}
      {err && <div className="field-error card-pad">{err.message}</div>}
    </Card>
  );
}

function SyncCard({ d }: { d?: PracticeOverview }) {
  const errors = d?.cursors.filter((c) => c.last_error) ?? [];
  const last = d?.cursors.map((c) => c.last_run_at).filter(Boolean).sort().pop();
  return (
    <Card title="Sync" subtitle={`${last ? `Last synced ${timeAgo(last)}` : "Not synced yet"} · ${d?.api_calls_total ?? 0} EHR API calls so far.`}>
      {errors.length > 0 && (
        <ul className="list">
          {errors.map((c) => (
            <li className="list-row" key={c.resource}>
              <div className="row-main">
                <div className="row-title">{RESOURCE_LABEL[c.resource] ?? c.resource}</div>
                <div className="row-sub field-error">{c.last_error}</div>
              </div>
            </li>
          ))}
        </ul>
      )}
      <details className="card-pad">
        <summary className="small" style={{ cursor: "pointer" }}>
          What's mirrored (allow-listed fields only; patient identifiers encrypted)
        </summary>
        <ul className="list">
          {Object.entries(RESOURCE_LABEL).map(([key, label]) => (
            <li className="list-row" key={key} style={{ minHeight: 36 }}>
              <div className="row-main">
                <div className="row-title">{label}</div>
              </div>
              <span className="tabular" style={{ fontWeight: 600 }}>
                {d?.mirror[key] ?? 0}
              </span>
            </li>
          ))}
        </ul>
      </details>
    </Card>
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

function FrontDeskMessages({ canRun, rows: data }: { canRun: boolean; rows?: FrontDeskMessage[] }) {
  const toast = useToast();
  const qc = useQueryClient();
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
  const rows = data ?? [];
  return (
    <Card title="Front desk messages" subtitle="Taken by the receptionist on calls. Opening a message is logged.">
      {rows.length === 0 ? (
        <div className="card-pad small muted">No open messages.</div>
      ) : (
        <ul className="list">
          {rows.slice(0, 8).map((m) => (
            <li className="list-row" key={m.id}>
              <div className="row-icon" style={{ background: m.callback_requested ? "var(--tile-orange)" : "var(--tile-gray)" }}>
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
    <Card title="Waiting to go to the EHR" subtitle="Every change is approved, sent once, and tracked until the EHR confirms it.">
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
