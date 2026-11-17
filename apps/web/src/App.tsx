import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Building2, ChevronsUpDown, Home, LogOut, Menu, OctagonX, ShieldCheck, X } from "lucide-react";
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { Link, NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { api, ApiError, type Inbox as InboxData, type Me, type MyWorkspace, type WorkspaceSummary } from "./api";
import { BrandLockup } from "./Brand";
import { GROUPS, hasOverview, isActive, visibleRows, type Badge, type NavItem } from "./nav";
import { BackOfficeOverview, ClinicalOverview, FrontOfficeOverview } from "./pages/Overview";
import { SignIn } from "./SignIn";
import AgentEditor from "./pages/AgentEditor";
import Agents from "./pages/Agents";
import Approvals from "./pages/Approvals";
import Backfill from "./pages/Backfill";
import BrowserAgent from "./pages/BrowserAgent";
import Hub from "./pages/Hub";
import ReviewInbox from "./pages/Inbox";
import CallDetail from "./pages/CallDetail";
import Claims from "./pages/Claims";
import PriorAuth from "./pages/PriorAuth";
import Cases from "./pages/Cases";
import CareOpportunities from "./pages/CareOpportunities";
import Calls from "./pages/Calls";
import Connections from "./pages/Connections";
import Integrations from "./pages/Integrations";
import People from "./pages/People";
import Practice from "./pages/Practice";
import ResultDetail from "./pages/ResultDetail";
import Personas from "./pages/Personas";
import Results from "./pages/Results";
import ShadowMode from "./pages/ShadowMode";
import TaskComposer from "./pages/TaskComposer";
import TextMessages from "./pages/TextMessages";
import Tasks from "./pages/Tasks";
import VoiceStudio from "./pages/VoiceStudio";
import Members from "./pages/Members";
import { Discharges, Instructions, Previsit, Refills } from "./pages/Clinical";
import Intake from "./pages/Intake";
import PublicForm from "./pages/PublicForm";
import Platform from "./pages/Platform";
import Workspace from "./pages/Workspace";

const MeCtx = createContext<Me | null>(null);
export const useMe = () => useContext(MeCtx)!;
export const useCan = (permission: string) => useMe().permissions.includes(permission);

export default function App() {
  // A patient's secure form link opens without a Studio session.
  if (window.location.pathname.startsWith("/f/"))
    return (
      <Routes>
        <Route path="/f/:token" element={<PublicForm />} />
      </Routes>
    );
  return <Studio />;
}

function Studio() {
  const me = useQuery({ queryKey: ["me"], queryFn: () => api.get<Me>("/me"), retry: false });
  if (me.isPending) return null;
  if (me.error instanceof ApiError && me.error.status === 401) return <SignIn />;
  if (me.error) return <SignIn error={me.error.message} />;
  return (
    <MeCtx.Provider value={me.data}>
      <Shell />
    </MeCtx.Provider>
  );
}

const initials = (name: string) =>
  name
    .split(" ")
    .map((p) => p[0])
    .join("")
    .slice(0, 2);

function Shell() {
  const me = useMe();
  const show = (section: string) => me.pack.sections.includes(section);
  const { pathname, search } = useLocation();
  useEffect(() => {
    window.scrollTo(0, 0);
  }, [pathname]);
  const qc = useQueryClient();
  const summary = useQuery({
    queryKey: ["workspace-summary"],
    queryFn: () => api.get<WorkspaceSummary>("/workspace/summary"),
    refetchInterval: 4000,
  });
  const logout = useMutation({
    mutationFn: () => api.post("/auth/logout"),
    onSuccess: () => {
      qc.clear();
      window.location.assign("/");
    },
  });
  const inbox = useQuery({ queryKey: ["inbox"], queryFn: () => api.get<InboxData>("/inbox"), refetchInterval: 8000, enabled: show("inbox") });
  const badges: Record<Badge, number> = {
    inbox: inbox.data?.total ?? 0,
    calls: summary.data?.active_calls.length ?? 0,
    results: summary.data?.needs_attention.length ?? 0,
  };
  // On a phone the menu folds behind a button and closes again once a page is picked.
  const [menuOpen, setMenuOpen] = useState(false);
  useEffect(() => setMenuOpen(false), [pathname, search]);

  return (
    <div className="app">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <nav className="sidebar" aria-label="Primary">
        <div className="brand">
          <NavLink to="/" className="brand-home" aria-label="CarePlix Voice home">
            <BrandLockup />
          </NavLink>
          <WorkspaceSwitcher />
        </div>
        <button type="button" className="btn btn-secondary nav-toggle" aria-expanded={menuOpen} aria-controls="nav-menu" onClick={() => setMenuOpen((o) => !o)}>
          {menuOpen ? <X size={16} aria-hidden /> : <Menu size={16} aria-hidden />} Menu
        </button>
        <div className="nav-menu" id="nav-menu" data-open={menuOpen}>
          <ul className="nav-group">
            <li>
              <NavLink to="/" end className="nav-link">
                <Home size={16} aria-hidden /> Home
              </NavLink>
            </li>
          </ul>
          {GROUPS.map((g) => (
            <NavGroup key={g} label={g} rows={visibleRows(g, me)} badges={badges} />
          ))}
        </div>
        <div className="sidebar-foot">
          {me.emergency_stop && (
            <span className="pill tone-red" style={{ justifySelf: "start" }}>
              <OctagonX size={13} aria-hidden /> Calling stopped
            </span>
          )}
          <span className="sample-label">
            <ShieldCheck size={13} aria-hidden /> Synthetic data only
          </span>
          <div className="user-chip">
            <div className="avatar" aria-hidden>
              {initials(me.display_name)}
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 600, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{me.display_name}</div>
              <div className="small muted" style={{ textTransform: "capitalize" }}>
                {me.role}
              </div>
            </div>
            <button className="btn btn-plain btn-icon" onClick={() => logout.mutate()} aria-label="Sign out" title="Sign out" style={{ color: "var(--label-2)" }}>
              <LogOut size={16} aria-hidden />
            </button>
          </div>
        </div>
      </nav>
      <main className="main" id="main" tabIndex={-1}>
        <Routes>
          <Route path="/" element={<Workspace />} />
          <Route path="/front-office" element={<Gate show={hasOverview("Front Office", me)}><FrontOfficeOverview /></Gate>} />
          <Route path="/back-office" element={<Gate show={hasOverview("Back Office", me)}><BackOfficeOverview /></Gate>} />
          <Route path="/clinical" element={<Gate show={hasOverview("Clinical", me)}><ClinicalOverview /></Gate>} />
          <Route path="/eligibility" element={<Gate show={show("practice")}><Practice view="eligibility" /></Gate>} />
          <Route path="/studio" element={<Gate show={show("studio")}><VoiceStudio /></Gate>} />
          <Route path="/personas" element={<Gate show={show("personas")}><Personas /></Gate>} />
          <Route path="/shadow" element={<Gate show={show("shadow")}><ShadowMode /></Gate>} />
          <Route path="/shadow/:id" element={<Gate show={show("shadow")}><ShadowMode /></Gate>} />
          <Route path="/browser" element={<Gate show={show("browser")}><BrowserAgent /></Gate>} />
          <Route path="/browser/:id" element={<Gate show={show("browser")}><BrowserAgent /></Gate>} />
          <Route path="/approvals" element={<Gate show={show("approvals")}><Approvals /></Gate>} />
          <Route path="/inbox" element={<Gate show={show("inbox")}><ReviewInbox /></Gate>} />
          <Route path="/agents" element={<Gate show={show("agents")}><Agents /></Gate>} />
          <Route path="/agents/:id" element={<Gate show={show("agents")}><AgentEditor /></Gate>} />
          <Route path="/cases" element={<Gate show={show("cases")}><Cases /></Gate>} />
          <Route path="/people" element={<Gate show={show("people")}><People /></Gate>} />
          <Route path="/tasks" element={<Gate show={show("tasks")}><Tasks /></Gate>} />
          <Route path="/tasks/new" element={<Gate show={show("tasks")}><TaskComposer /></Gate>} />
          <Route path="/calls" element={<Gate show={show("calls")}><Calls /></Gate>} />
          <Route path="/calls/:id" element={<Gate show={show("calls")}><CallDetail /></Gate>} />
          <Route path="/results" element={<Gate show={show("results")}><Results /></Gate>} />
          <Route path="/care" element={<Gate show={show("care")}><CareOpportunities /></Gate>} />
          <Route path="/results/:id" element={<Gate show={show("results")}><ResultDetail /></Gate>} />
          <Route path="/connections" element={<Gate show={show("connections")}><Connections /></Gate>} />
          <Route path="/integrations" element={<Gate show={show("integrations")}><Integrations /></Gate>} />
          <Route path="/practice" element={<Gate show={show("practice")}><Practice /></Gate>} />
          <Route path="/backfill" element={<Gate show={show("backfill")}><Backfill /></Gate>} />
          <Route path="/texts" element={<Gate show={show("texts")}><TextMessages /></Gate>} />
          <Route path="/claims" element={<Gate show={show("claims")}><Claims /></Gate>} />
          <Route path="/prior-auth" element={<Gate show={show("prior_auth")}><PriorAuth /></Gate>} />
          <Route path="/clinical/refills" element={<Gate show={show("clinical")}><Refills /></Gate>} />
          <Route path="/clinical/discharge" element={<Gate show={show("clinical")}><Discharges /></Gate>} />
          <Route path="/clinical/previsit" element={<Gate show={show("clinical")}><Previsit /></Gate>} />
          <Route path="/clinical/instructions" element={<Gate show={show("clinical")}><Instructions /></Gate>} />
          <Route path="/intake" element={<Gate show={show("intake")}><Intake /></Gate>} />
          <Route path="/hub" element={<Gate show={show("hub")}><Hub /></Gate>} />
          <Route path="/members" element={<Members />} />
          <Route path="/platform" element={<Gate show={me.platform}><Platform /></Gate>} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

function NavGroup({ label, rows, badges }: { label: string; rows: NavItem[]; badges: Record<Badge, number> }) {
  const { pathname, search } = useLocation();
  if (!rows.length) return null;
  const id = `nav-${label.replace(/\W+/g, "-").toLowerCase()}`;
  return (
    <>
      <div className="nav-section" id={id}>
        {label}
      </div>
      <ul className="nav-group" aria-labelledby={id}>
        {rows.map((r) => {
          const active = isActive(r, pathname, search);
          const n = r.badge ? badges[r.badge] : 0;
          return (
            <li key={r.path}>
              <Link to={r.path} className={`nav-link${active ? " active" : ""}`} aria-current={active ? "page" : undefined}>
                <r.icon size={16} aria-hidden /> {r.label}
                {r.tag && <span className="nav-tag">{r.tag}</span>}
                {n > 0 &&
                  (r.badge === "calls" ? (
                    <span className="count" aria-label={`${n} live`}>
                      <span className="pill tone-green" style={{ padding: "0 7px" }}>
                        <span className="live-dot" aria-hidden />
                        {n}
                      </span>
                    </span>
                  ) : (
                    <span className="count" aria-label={`${n} ${r.badge === "inbox" ? "waiting" : "to review"}`}>
                      {n}
                    </span>
                  ))}
              </Link>
            </li>
          );
        })}
      </ul>
    </>
  );
}

/** A hidden section's address goes home instead of rendering a page the pack doesn't offer. */
function Gate({ show, children }: { show: boolean; children: ReactNode }) {
  return show ? <>{children}</> : <Navigate to="/" replace />;
}

/** The current workspace, and a switcher when the person belongs to more than one. */
function WorkspaceSwitcher() {
  const me = useMe();
  const qc = useQueryClient();
  const mine = useQuery({ queryKey: ["my-workspaces"], queryFn: () => api.get<MyWorkspace[]>("/auth/workspaces") });
  const swap = useMutation({
    mutationFn: (workspace_id: string) => api.post<Me>("/auth/workspace", { workspace_id }),
    onSuccess: () => {
      qc.clear();
      window.location.assign("/");
    },
  });
  const list = mine.data ?? [];
  const label = (
    <>
      <Building2 size={14} aria-hidden style={{ flex: "none" }} />
      <strong style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{me.workspace_name}</strong>
    </>
  );
  if (list.length < 2) {
    return (
      <div className="brand-workspace" title={`${me.workspace_name} · ${me.pack.label}`}>
        {label}
      </div>
    );
  }
  return (
    <label className="brand-workspace" title={me.pack.label} style={{ position: "relative", cursor: "pointer" }}>
      {label}
      <ChevronsUpDown size={13} aria-hidden style={{ flex: "none", marginLeft: "auto" }} />
      <select
        aria-label="Switch workspace"
        value={me.workspace_id}
        disabled={swap.isPending}
        onChange={(e) => swap.mutate(e.target.value)}
        style={{ position: "absolute", inset: 0, opacity: 0, cursor: "pointer" }}
      >
        {list.map((w) => (
          <option key={w.workspace_id} value={w.workspace_id}>
            {w.name}
          </option>
        ))}
      </select>
    </label>
  );
}
