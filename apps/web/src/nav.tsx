// The Studio's menu as data. Adding a page is one row here plus its route in App.tsx.
//
// `section` is the pack section key from the API (`packs.py`), so a product line's pack decides
// what shows; `caseType` also needs the pack to allow that case type; `staff` is CarePlix staff
// only; `overview` rows show only when another row in the same group is visible.
import {
  Banknote,
  BadgeCheck,
  BookCheck,
  Bot,
  Briefcase,
  Building,
  Building2,
  CalendarClock,
  CalendarPlus,
  ClipboardCheck,
  ClipboardList,
  FileWarning,
  GitCompareArrows,
  Globe,
  HeartPulse,
  Hospital as HospitalIcon,
  IdCard,
  Inbox,
  LayoutGrid,
  ListChecks,
  MessageSquareText,
  MessagesSquare,
  Network,
  Phone,
  Pill,
  PlugZap,
  Receipt,
  ScrollText,
  Settings2,
  ShieldCheck,
  Sparkles,
  Stethoscope,
  UserRound,
  Users,
  type LucideIcon,
} from "lucide-react";
import type { Me } from "./api";

export const GROUPS = ["Work", "Front Office", "Back Office", "Clinical", "Agents & Studio", "Data & connections", "Admin"] as const;
export type Group = (typeof GROUPS)[number];

export type Badge = "inbox" | "calls" | "results";

export interface NavItem {
  group: Group;
  path: string;
  label: string;
  icon: LucideIcon;
  section?: string;
  /** Hidden when the pack shows this section instead (a fuller page for the same work). */
  unless?: string;
  caseType?: string;
  overview?: boolean;
  staff?: boolean;
  admin?: boolean;
  badge?: Badge;
  tag?: string;
}

export const NAV: NavItem[] = [
  // Work: everything waiting on someone, whatever area it came from.
  { group: "Work", path: "/inbox", label: "Review inbox", icon: Inbox, section: "inbox", badge: "inbox" },
  { group: "Work", path: "/cases", label: "Cases", icon: Briefcase, section: "cases" },
  { group: "Work", path: "/tasks", label: "Tasks", icon: ListChecks, section: "tasks" },
  { group: "Work", path: "/calls", label: "Calls", icon: Phone, section: "calls", badge: "calls" },
  { group: "Work", path: "/results", label: "Results", icon: ClipboardCheck, section: "results", badge: "results" },
  { group: "Work", path: "/texts", label: "Messages", icon: MessageSquareText, section: "texts" },
  { group: "Work", path: "/people", label: "People", icon: Users, section: "people" },

  // Front Office: the schedule, the front desk and keeping slots full.
  { group: "Front Office", path: "/front-office", label: "Overview", icon: LayoutGrid, overview: true },
  { group: "Front Office", path: "/practice", label: "Schedule", icon: CalendarClock, section: "practice" },
  { group: "Front Office", path: "/practice?tab=inbox", label: "Front-desk messages", icon: MessagesSquare, section: "practice" },
  { group: "Front Office", path: "/backfill", label: "Waitlist & backfill", icon: CalendarPlus, section: "backfill" },
  { group: "Front Office", path: "/cases?type=appointment_reminder", label: "Reminders", icon: CalendarClock, section: "cases", caseType: "appointment_reminder" },
  { group: "Front Office", path: "/intake", label: "Intake & registration", icon: IdCard, section: "intake", caseType: "patient_intake" },

  // Back Office: coverage, authorizations and getting paid.
  { group: "Back Office", path: "/back-office", label: "Overview", icon: LayoutGrid, overview: true },
  { group: "Back Office", path: "/eligibility", label: "Eligibility & benefits", icon: BadgeCheck, section: "practice" },
  { group: "Back Office", path: "/prior-auth", label: "Prior auth", icon: ShieldCheck, section: "prior_auth" },
  // Lines without the prior-auth work list still see their prior-auth cases.
  { group: "Back Office", path: "/cases?type=prior_authorization", label: "Prior auth", icon: ShieldCheck, section: "cases", caseType: "prior_authorization", unless: "prior_auth" },
  { group: "Back Office", path: "/claims", label: "Claims", icon: Receipt, section: "claims" },
  { group: "Back Office", path: "/claims?tab=work", label: "Rejections & follow-up", icon: FileWarning, section: "claims" },
  { group: "Back Office", path: "/claims?tab=era", label: "Payments (ERA)", icon: Banknote, section: "claims" },
  { group: "Back Office", path: "/claims?tab=appeals", label: "Denials & appeals", icon: ScrollText, section: "claims" },

  // Clinical: clinician-approved outreach and sign-offs. The agent never decides anything clinical.
  { group: "Clinical", path: "/clinical", label: "Overview", icon: LayoutGrid, overview: true },
  { group: "Clinical", path: "/care", label: "Care opportunities", icon: HeartPulse, section: "care" },
  { group: "Clinical", path: "/inbox?kind=clinician_signoff", label: "Clinician review", icon: Stethoscope, section: "inbox" },
  { group: "Clinical", path: "/clinical/refills", label: "Refills", icon: Pill, section: "clinical", caseType: "refill_request" },
  { group: "Clinical", path: "/clinical/discharge", label: "Discharge follow-up", icon: HospitalIcon, section: "clinical", caseType: "discharge_followup" },
  { group: "Clinical", path: "/clinical/previsit", label: "Pre-visit", icon: ClipboardList, section: "clinical", caseType: "previsit_prep" },
  { group: "Clinical", path: "/clinical/instructions", label: "Approved instructions", icon: BookCheck, section: "clinical" },
  { group: "Clinical", path: "/cases?type=care_outreach", label: "Care outreach", icon: Users, section: "cases", caseType: "care_outreach" },

  // Agents & Studio: build, test and supervise the agents.
  { group: "Agents & Studio", path: "/agents", label: "Agents", icon: Bot, section: "agents" },
  { group: "Agents & Studio", path: "/studio", label: "Voice Studio", icon: Sparkles, section: "studio" },
  { group: "Agents & Studio", path: "/personas", label: "Personas", icon: UserRound, section: "personas" },
  { group: "Agents & Studio", path: "/shadow", label: "Shadow mode", icon: GitCompareArrows, section: "shadow" },
  { group: "Agents & Studio", path: "/browser", label: "Browser agent", icon: Globe, section: "browser", tag: "Preview" },
  { group: "Agents & Studio", path: "/approvals", label: "Browser approvals", icon: Inbox, section: "approvals" },

  // Data & connections: the systems CarePlix reads and writes.
  { group: "Data & connections", path: "/practice?tab=setup", label: "Practice setup", icon: Settings2, section: "practice", admin: true },
  { group: "Data & connections", path: "/hub", label: "EHR hub", icon: Network, section: "hub" },
  { group: "Data & connections", path: "/claims?tab=clearinghouse", label: "Clearinghouse", icon: Receipt, section: "claims" },
  { group: "Data & connections", path: "/integrations", label: "Payer APIs", icon: Building2, section: "integrations" },
  { group: "Data & connections", path: "/connections", label: "Phone lines", icon: PlugZap, section: "connections" },

  // Admin.
  { group: "Admin", path: "/members", label: "Members", icon: Users },
  { group: "Admin", path: "/platform", label: "Customers", icon: Building, staff: true, tag: "Staff" },
];

/** Whether the person's pack, role and staff status show this row (overview rows aside). */
export function allowed(item: NavItem, me: Me): boolean {
  if (item.staff) return me.platform;
  if (item.admin && !me.permissions.includes("admin")) return false;
  if (item.section && !me.pack.sections.includes(item.section)) return false;
  if (item.unless && me.pack.sections.includes(item.unless)) return false;
  if (item.caseType && !me.pack.case_types.includes(item.caseType)) return false;
  return true;
}

/** The rows of a group this person sees; an overview shows only when the group has other rows. */
export function visibleRows(group: Group, me: Me): NavItem[] {
  const rows = NAV.filter((i) => i.group === group && !i.overview && allowed(i, me));
  if (!rows.length) return [];
  const overview = NAV.find((i) => i.group === group && i.overview);
  return overview ? [overview, ...rows] : rows;
}

/** Whether a group's overview page should render for this person. */
export const hasOverview = (group: Group, me: Me) => visibleRows(group, me).some((i) => i.overview);

// The query keys that pick a view of a page: a row is active only when these match exactly, so
// "/claims" and "/claims?tab=era" light up separately.
const VIEW_KEYS = ["tab", "type", "kind"];

export function isActive(item: NavItem, pathname: string, search: string): boolean {
  const [path, query = ""] = item.path.split("?");
  if (pathname !== path && !(path !== "/" && pathname.startsWith(`${path}/`))) return false;
  const want = new URLSearchParams(query);
  const have = new URLSearchParams(search);
  return VIEW_KEYS.every((k) => (want.get(k) ?? "") === (have.get(k) ?? ""));
}
