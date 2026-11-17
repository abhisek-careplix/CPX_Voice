// One overview per area of the clinic: the numbers that matter, the work waiting on a person, and
// what is left to set up. Each tile and queue appears only when the workspace's pack shows the page
// it opens, so the same layout serves CarePlix Practice and CarePlix Home Health.
import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  Banknote,
  BadgeCheck,
  CalendarClock,
  CalendarPlus,
  Check,
  FileWarning,
  Gauge,
  HeartPulse,
  Hourglass,
  MessagesSquare,
  PhoneForwarded,
  Receipt,
  ShieldCheck,
  Stethoscope,
  Users,
  type LucideIcon,
} from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { useMe } from "../App";
import {
  api,
  type BackfillBoard,
  type CareQueue,
  type CaseItem,
  type ChannelHandoff,
  type ChannelSettings,
  type FrontDeskMessage,
  type Huddle,
  type Inbox,
  type PracticeOverview,
} from "../api";
import { withPractice, usePractice } from "../practice";
import { Card, Pill, StatTile, Toolbar, WorkQueue, timeAgo, type QueueItem } from "../ui";
import { CaseStatusPill } from "./Cases";

// Which case types each area owns. A new case type joins an area by adding it here.
export const AREA_CASE_TYPES = {
  front: ["appointment_reminder", "slot_backfill", "call_back"],
  back: ["prior_authorization", "claim_status", "claim_denial", "eligibility_check"],
  clinical: ["care_outreach"],
} as const;

const LIVE: CaseItem["status"][] = ["open", "in_progress", "handed_off", "blocked"];

/** Live cases of these types, the ones needing a person first: overdue, then blocked or handed off. */
function waitingCases(cases: CaseItem[] | undefined, types: readonly string[]): CaseItem[] | undefined {
  if (!cases) return undefined;
  const weight = (c: CaseItem) => (c.overdue ? 0 : c.status === "blocked" ? 1 : c.status === "handed_off" ? 2 : 3);
  return cases.filter((c) => types.includes(c.case_type) && LIVE.includes(c.status)).sort((a, b) => weight(a) - weight(b));
}

const caseItem = (c: CaseItem): QueueItem => ({
  id: c.id,
  title: (
    <>
      <span className="mono">{c.case_ref}</span> · {c.type_name}
    </>
  ),
  meta: [c.subject, c.next_step ?? c.headline, c.due_at && `due ${new Date(c.due_at).toLocaleDateString(undefined, { month: "short", day: "numeric" })}`].filter(Boolean).join(" · "),
  badge: c.overdue ? (
    <Pill tone="red" icon={AlertTriangle}>
      Overdue
    </Pill>
  ) : (
    <CaseStatusPill status={c.status} />
  ),
  to: `/cases?case=${c.id}`,
});

function useShow() {
  const me = useMe();
  return {
    section: (s: string) => me.pack.sections.includes(s),
    caseType: (t: string) => me.pack.case_types.includes(t),
  };
}

function useCases(enabled: boolean) {
  return useQuery({ queryKey: ["cases", "overview"], queryFn: () => api.get<CaseItem[]>("/cases"), enabled, refetchInterval: 15000 });
}

function usePracticeData(enabled: boolean, days: number) {
  const [practice] = usePractice();
  const overview = useQuery({ queryKey: ["practice", practice], queryFn: () => api.get<PracticeOverview>(withPractice("/practices", practice)), enabled });
  const huddle = useQuery({
    queryKey: ["practice-huddle", practice, "overview", days],
    queryFn: () => api.get<Huddle>(withPractice(`/practices/huddle?days=${days}`, practice)),
    enabled: enabled && !!overview.data?.ehr,
    refetchInterval: 15000,
  });
  return { practice, overview, huddle };
}

// ---------- Layout ----------

function Overview({ area, title, description, tiles, queues, setup }: { area: string; title: string; description: string; tiles: ReactNode[]; queues: ReactNode[]; setup?: ReactNode }) {
  const shownTiles = tiles.filter(Boolean);
  const shownQueues = queues.filter(Boolean);
  return (
    <>
      <Toolbar eyebrow={area} title={title} description={description} />
      <div className="content">
        {setup}
        {shownTiles.length > 0 && <div className="overview-tiles">{shownTiles}</div>}
        {shownQueues.length > 0 && <div className="overview-queues">{shownQueues}</div>}
      </div>
    </>
  );
}

interface Step {
  title: string;
  text: string;
  to: string;
  done: boolean;
}

/** What is still to set up for this area, in order; hidden once everything is done. */
function SetupSteps({ steps }: { steps: Step[] }) {
  const done = steps.filter((s) => s.done).length;
  if (!steps.length || done === steps.length) return null;
  const next = steps.findIndex((s) => !s.done);
  return (
    <Card title="Finish setting up" subtitle={`${done} of ${steps.length} done. Each step opens the page where you do it.`}>
      <ol className="setup-steps fit" style={{ ["--steps" as string]: steps.length }}>
        {steps.map((step, i) => (
          <li key={step.title}>
            <Link to={step.to} className="setup-step" data-done={step.done} data-next={i === next} aria-current={i === next ? "step" : undefined}>
              <span className="setup-num" aria-hidden>
                {step.done ? <Check size={14} strokeWidth={3} /> : i + 1}
              </span>
              <span className="setup-title">
                {step.title}
                <span className="sr-only">{step.done ? " (done)" : i === next ? " (next)" : ""}</span>
              </span>
              <span className="setup-text">{step.text}</span>
            </Link>
          </li>
        ))}
      </ol>
    </Card>
  );
}

const tile = (key: string, show: boolean, icon: LucideIcon, tint: string, value: number | string | null | undefined, label: string, to?: string) =>
  show ? <StatTile key={key} icon={icon} tint={tint} value={value} label={label} to={to} /> : null;

// ---------- Front Office ----------

export function FrontOfficeOverview() {
  const show = useShow();
  const practice = show.section("practice");
  const { overview, huddle } = usePracticeData(practice, 1);
  const [pid] = usePractice();
  const backfill = useQuery({ queryKey: ["backfill", pid, "overview"], queryFn: () => api.get<BackfillBoard>(withPractice("/practices/backfill", pid)), enabled: show.section("backfill") && !!overview.data?.ehr });
  const messages = useQuery({ queryKey: ["front-desk-messages"], queryFn: () => api.get<FrontDeskMessage[]>("/practices/messages"), enabled: practice, refetchInterval: 15000 });
  const handoffs = useQuery({ queryKey: ["channels", "handoffs"], queryFn: () => api.get<ChannelHandoff[]>("/channels/handoffs"), enabled: show.section("texts"), refetchInterval: 15000 });
  const texting = useQuery({ queryKey: ["channels", "settings"], queryFn: () => api.get<ChannelSettings>("/channels/settings"), enabled: show.section("texts") });
  const cases = useCases(show.section("cases"));

  const h = huddle.data;
  const m = backfill.data?.metrics;
  const openMessages = messages.data?.filter((x) => x.state === "open");
  const openHandoffs = handoffs.data?.filter((x) => !x.resolved_at);
  const ehr = !!overview.data?.ehr;
  const noEhr = practice && overview.isSuccess && !ehr;

  return (
    <Overview
      area="Front Office"
      title="Front Office"
      description="The schedule, the front desk and keeping every slot full. Agents answer, remind and fill cancellations; anything they can't finish lands here for a person."
      setup={
        <SetupSteps
          steps={[
            ...(practice ? [{ title: "Connect the practice system", text: "Link the EHR so agents see the schedule and can book.", to: "/hub", done: ehr }] : []),
            ...(show.section("texts") ? [{ title: "Turn on text messages", text: "Reminders and slot offers go out by text with consent.", to: "/texts", done: !!texting.data?.configured }] : []),
          ]}
        />
      }
      tiles={[
        tile("today", practice, CalendarClock, "var(--tile-blue)", noEhr ? "–" : h?.counts.scheduled, "Visits today", "/practice"),
        tile("attention", practice, AlertTriangle, "var(--tile-orange)", noEhr ? "–" : h?.counts.needs_attention, "Visits needing attention", "/eligibility"),
        tile("slots", show.section("backfill"), CalendarPlus, "var(--tile-purple)", noEhr ? "–" : backfill.data ? backfill.data.active.length : undefined, "Open slots to fill", "/backfill"),
        tile("fill", show.section("backfill"), Gauge, "var(--tile-green)", noEhr ? "–" : m ? (m.fill_rate === null ? "–" : `${Math.round(m.fill_rate * 100)}%`) : undefined, "Slot fill rate", "/backfill"),
        tile("messages", practice, MessagesSquare, "var(--tile-navy)", openMessages?.length, "Messages for the desk", "/practice?tab=inbox"),
      ]}
      queues={[
        practice && (
          <WorkQueue
            key="messages"
            title="Front-desk messages"
            icon={MessagesSquare}
            items={openMessages?.map((x) => ({
              id: x.id,
              title: x.patient ?? x.caller_name ?? "Unknown caller",
              meta: [x.topic.replace("_", " "), x.callback_requested && "call back", timeAgo(x.created_at)].filter(Boolean).join(" · "),
              to: "/practice?tab=inbox",
            }))}
            to="/practice?tab=inbox"
            empty="No messages waiting. Calls the receptionist couldn't finish appear here."
            error={messages.error}
          />
        ),
        show.section("texts") && (
          <WorkQueue
            key="handoffs"
            title="Call-backs and transfers"
            icon={PhoneForwarded}
            items={openHandoffs?.map((x) => ({
              id: x.id,
              title: x.reason,
              meta: [x.summary, x.staff_label, timeAgo(x.created_at)].filter(Boolean).join(" · "),
              badge: <Pill tone={x.state === "callback_promised" ? "orange" : "blue"}>{x.state === "callback_promised" ? "Call-back promised" : x.state === "connected" ? "Connected" : "Dialing"}</Pill>,
              to: x.case_id ? `/cases?case=${x.case_id}` : "/texts",
            }))}
            to="/texts"
            empty="No call-backs owed. When an agent hands a caller to staff, it shows here."
            error={handoffs.error}
          />
        ),
        show.section("cases") && (
          <WorkQueue
            key="cases"
            title="Front-office cases"
            icon={CalendarClock}
            items={waitingCases(cases.data, AREA_CASE_TYPES.front)?.map(caseItem)}
            to="/cases?type=appointment_reminder"
            empty="No reminders, slot offers or call-backs waiting on a person."
            error={cases.error}
          />
        ),
      ]}
    />
  );
}

// ---------- Back Office ----------

interface ClaimLite {
  id: string;
  claim_number: string;
  payer_name: string;
  age_days: number;
  final: boolean;
  status: string | null;
  status_label: string;
  billed_amount: string;
}

interface ClearinghouseLite {
  connected: boolean;
  mode: "test" | "production" | null;
}

export function BackOfficeOverview() {
  const show = useShow();
  const practice = show.section("practice");
  const claimsOn = show.section("claims");
  const { overview, huddle } = usePracticeData(practice, 7);
  const claims = useQuery({ queryKey: ["claims"], queryFn: () => api.get<ClaimLite[]>("/claims"), enabled: claimsOn });
  const ch = useQuery({ queryKey: ["clearinghouse"], queryFn: () => api.get<ClearinghouseLite>("/clearinghouse"), enabled: claimsOn });
  const cases = useCases(show.section("cases"));

  const live = waitingCases(cases.data, AREA_CASE_TYPES.back);
  const auths = live?.filter((c) => c.case_type === "prior_authorization");
  const denials = live?.filter((c) => c.case_type === "claim_denial" || c.case_type === "claim_status");
  const open = claims.data?.filter((c) => !c.final);
  const aged = open?.filter((c) => c.age_days >= 30).sort((a, b) => b.age_days - a.age_days);
  const noEhr = practice && overview.isSuccess && !overview.data?.ehr;

  return (
    <Overview
      area="Back Office"
      title="Back Office"
      description="Coverage, authorizations and getting paid. Agents check eligibility, chase prior auths and follow claims to cash; denials and anything a payer pushes back on wait here for a person."
      setup={
        <SetupSteps
          steps={[
            ...(claimsOn ? [{ title: "Connect the clearinghouse", text: "Eligibility, claims and remittances run through it.", to: "/claims?tab=clearinghouse", done: !!ch.data?.connected }] : []),
            ...(practice ? [{ title: "Connect the practice system", text: "Visits and charges come from the EHR.", to: "/hub", done: !!overview.data?.ehr }] : []),
          ]}
        />
      }
      tiles={[
        tile("coverage", practice, BadgeCheck, "var(--tile-orange)", noEhr ? "–" : h7(huddle.data), "Visits with coverage issues (7 days)", "/eligibility"),
        tile("auths", show.caseType("prior_authorization") && show.section("cases"), ShieldCheck, "var(--tile-blue)", auths?.length, "Prior auths in progress", "/cases?type=prior_authorization"),
        tile("claims", claimsOn, Receipt, "var(--tile-navy)", open?.length, "Open claims", "/claims"),
        tile("aged", claimsOn, Hourglass, "var(--tile-purple)", aged?.length, "Claims over 30 days", "/claims?tab=work"),
        tile("paid", claimsOn, Banknote, "var(--tile-green)", claims.data?.filter((c) => c.status === "paid").length, "Claims paid", "/claims?tab=era"),
      ]}
      queues={[
        show.section("cases") && show.caseType("prior_authorization") && (
          <WorkQueue key="auths" title="Prior auths" icon={ShieldCheck} items={auths?.map(caseItem)} to="/cases?type=prior_authorization" empty="No prior auths waiting. They open on their own when a payer requires one." error={cases.error} />
        ),
        claimsOn && (
          <WorkQueue
            key="aged"
            title="Claims over 30 days"
            icon={Hourglass}
            items={aged?.map((c) => ({
              id: c.id,
              title: (
                <>
                  <span className="mono">{c.claim_number}</span> · {c.payer_name}
                </>
              ),
              meta: [`${c.age_days} days`, c.status_label, `$${Number(c.billed_amount).toFixed(2)}`].join(" · "),
              to: `/claims?claim=${c.id}`,
            }))}
            to="/claims?tab=work"
            empty="No open claim is older than 30 days."
            error={claims.error}
          />
        ),
        show.section("cases") && (
          <WorkQueue key="denials" title="Denials and claim follow-up" icon={FileWarning} items={denials?.map(caseItem)} to="/claims?tab=work" empty="No denied or stuck claims waiting on a person." error={cases.error} />
        ),
      ]}
    />
  );
}

const h7 = (h: Huddle | undefined) => h?.counts.needs_attention;

// ---------- Clinical ----------

export function ClinicalOverview() {
  const show = useShow();
  const care = useQuery({ queryKey: ["care"], queryFn: () => api.get<CareQueue>("/care-opportunities"), enabled: show.section("care") });
  const inbox = useQuery({ queryKey: ["inbox"], queryFn: () => api.get<Inbox>("/inbox"), enabled: show.section("inbox"), refetchInterval: 8000 });
  const cases = useCases(show.section("cases"));

  const c = care.data?.counts;
  const count = (...keys: (keyof NonNullable<typeof c>)[]) => (c ? keys.reduce((n, k) => n + (c[k] ?? 0), 0) : undefined);
  const signoffs = inbox.data?.items.filter((i) => i.kind === "clinician_signoff");

  return (
    <Overview
      area="Clinical"
      title="Clinical"
      description="Clinician-approved outreach and sign-offs. Agents find who qualifies, gather what the clinician needs and deliver approved words; they never make a clinical decision."
      tiles={[
        tile("signoffs", show.section("inbox"), Stethoscope, "var(--tile-orange)", inbox.data ? signoffs?.length : undefined, "Waiting for clinician sign-off", "/inbox?kind=clinician_signoff"),
        tile("review", show.section("care"), HeartPulse, "var(--tile-blue)", count("proposed"), "Care opportunities to review", "/care"),
        tile("outreach", show.section("care"), Users, "var(--tile-purple)", count("approved", "contacted"), "Patients in outreach", "/care"),
        tile("enrolled", show.section("care"), BadgeCheck, "var(--tile-green)", count("enrolled", "done"), "Enrolled or done", "/care"),
      ]}
      queues={[
        show.section("inbox") && (
          <WorkQueue
            key="signoffs"
            title="Clinician sign-offs"
            icon={Stethoscope}
            items={signoffs?.map((i) => ({ id: i.id, title: i.title, meta: i.detail, to: i.href }))}
            to="/inbox?kind=clinician_signoff"
            empty="Nothing waiting for a clinician."
            error={inbox.error}
          />
        ),
        show.section("care") && (
          <WorkQueue
            key="programs"
            title="Open opportunities by program"
            icon={HeartPulse}
            items={care.data?.open_by_program.map((p) => ({ id: p.program, title: p.title, badge: <span className="queue-count">{p.count}</span>, to: "/care" }))}
            to="/care"
            empty="No open care opportunities. Import clinical facts and scan to find who qualifies."
            error={care.error}
          />
        ),
        show.section("cases") && show.caseType("care_outreach") && (
          <WorkQueue key="outreach" title="Care outreach cases" icon={Users} items={waitingCases(cases.data, AREA_CASE_TYPES.clinical)?.map(caseItem)} to="/cases?type=care_outreach" empty="No outreach waiting on a person." error={cases.error} />
        ),
      ]}
    />
  );
}
