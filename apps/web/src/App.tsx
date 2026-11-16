import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, Briefcase, HeartPulse, Building2, UserRound, CalendarPlus, ClipboardCheck, FileCheck, GitCompareArrows, Globe, Home, Inbox, ListChecks, LogOut, Network, OctagonX, Phone, PlugZap, Receipt, ShieldCheck, Sparkles, Stethoscope, Building, Users, ChevronsUpDown } from "lucide-react";
import { createContext, useContext, useEffect, type ReactNode } from "react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { api, ApiError, type Inbox as InboxData, type Me, type MyWorkspace, type WorkspaceSummary } from "./api";
import { BrandLockup } from "./Brand";
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
import Tasks from "./pages/Tasks";
import VoiceStudio from "./pages/VoiceStudio";
import Members from "./pages/Members";
import Platform from "./pages/Platform";
import Workspace from "./pages/Workspace";

const MeCtx = createContext<Me | null>(null);
export const useMe = () => useContext(MeCtx)!;
export const useCan = (permission: string) => useMe().permissions.includes(permission);

export default function App() {
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
  const { pathname } = useLocation();
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
  const waiting = inbox.data?.total ?? 0;
  const live = summary.data?.active_calls.length ?? 0;
  const review = summary.data?.needs_attention.length ?? 0;

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
        <ul className="nav-group">
          <li>
            <NavLink to="/" end className="nav-link">
              <Home size={16} aria-hidden /> Home
            </NavLink>
          </li>
        </ul>
        <NavGroup label="Build">
          {show("agents") && (
            <NavLink to="/agents" className="nav-link">
              <Bot size={16} aria-hidden /> Agents
            </NavLink>
          )}
          {show("studio") && (
            <NavLink to="/studio" className="nav-link">
              <Sparkles size={16} aria-hidden /> Voice Studio
            </NavLink>
          )}
          {show("personas") && (
            <NavLink to="/personas" className="nav-link">
              <UserRound size={16} aria-hidden /> Personas
            </NavLink>
          )}
          {show("shadow") && (
            <NavLink to="/shadow" className="nav-link">
              <GitCompareArrows size={16} aria-hidden /> Shadow mode
            </NavLink>
          )}
        </NavGroup>
        <NavGroup label="Operate">
          {show("inbox") && (
            <NavLink to="/inbox" className="nav-link">
              <Inbox size={16} aria-hidden /> Review inbox
              {waiting > 0 && (
                <span className="count" aria-label={`${waiting} waiting`}>
                  {waiting}
                </span>
              )}
            </NavLink>
          )}
          {show("cases") && (
            <NavLink to="/cases" className="nav-link">
              <Briefcase size={16} aria-hidden /> Cases
            </NavLink>
          )}
          {show("people") && (
            <NavLink to="/people" className="nav-link">
              <Users size={16} aria-hidden /> People
            </NavLink>
          )}
          {show("tasks") && (
            <NavLink to="/tasks" className="nav-link">
              <ListChecks size={16} aria-hidden /> Tasks
            </NavLink>
          )}
          {show("calls") && (
            <NavLink to="/calls" className="nav-link">
              <Phone size={16} aria-hidden /> Calls
              {live > 0 && (
                <span className="count" aria-label={`${live} live`}>
                  <span className="pill tone-green" style={{ padding: "0 7px" }}>
                    <span className="live-dot" aria-hidden />
                    {live}
                  </span>
                </span>
              )}
            </NavLink>
          )}
          {show("care") && (
            <NavLink to="/care" className="nav-link">
              <HeartPulse size={16} aria-hidden /> Care opportunities
            </NavLink>
          )}
          {show("results") && (
            <NavLink to="/results" className="nav-link">
              <ClipboardCheck size={16} aria-hidden /> Results
              {review > 0 && (
                <span className="count" aria-label={`${review} to review`}>
                  {review}
                </span>
              )}
            </NavLink>
          )}
        </NavGroup>
        <NavGroup label="Front office">
          {show("practice") && (
            <NavLink to="/practice" className="nav-link">
              <Stethoscope size={16} aria-hidden /> Practice
            </NavLink>
          )}
          {show("backfill") && (
            <NavLink to="/backfill" className="nav-link">
              <CalendarPlus size={16} aria-hidden /> Backfill
            </NavLink>
          )}
        </NavGroup>
        <NavGroup label="Back office">
          {[
            show("claims") && (
              <NavLink key="claims" to="/claims" className="nav-link">
                <Receipt size={16} aria-hidden /> Claims
              </NavLink>
            ),
            show("prior_auth") && (
              <NavLink key="prior_auth" to="/prior-auth" className="nav-link">
                <FileCheck size={16} aria-hidden /> Prior auth
              </NavLink>
            ),
          ]}
        </NavGroup>
        <NavGroup label="Settings">
          {show("connections") && (
            <NavLink to="/connections" className="nav-link">
              <PlugZap size={16} aria-hidden /> Connections
            </NavLink>
          )}
          {show("integrations") && (
            <NavLink to="/integrations" className="nav-link">
              <Building2 size={16} aria-hidden /> Integrations
            </NavLink>
          )}
          {show("hub") && (
            <NavLink to="/hub" className="nav-link">
              <Network size={16} aria-hidden /> EHR hub
            </NavLink>
          )}
        </NavGroup>
        <NavGroup label="Team">
          <NavLink to="/members" className="nav-link">
            <Users size={16} aria-hidden /> Members
          </NavLink>
          {me.platform && (
            <NavLink to="/platform" className="nav-link">
              <Building size={16} aria-hidden /> Customers <span className="nav-tag">Staff</span>
            </NavLink>
          )}
        </NavGroup>
        <NavGroup label="Labs">
          {show("browser") && (
            <NavLink to="/browser" className="nav-link">
              <Globe size={16} aria-hidden /> Browser agent <span className="nav-tag">Preview</span>
            </NavLink>
          )}
          {show("approvals") && (
            <NavLink to="/approvals" className="nav-link">
              <Inbox size={16} aria-hidden /> Approvals
            </NavLink>
          )}
        </NavGroup>
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
          <Route path="/claims" element={<Gate show={show("claims")}><Claims /></Gate>} />
          <Route path="/prior-auth" element={<Gate show={show("prior_auth")}><PriorAuth /></Gate>} />
          <Route path="/hub" element={<Gate show={show("hub")}><Hub /></Gate>} />
          <Route path="/members" element={<Members />} />
          <Route path="/platform" element={<Gate show={me.platform}><Platform /></Gate>} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

function NavGroup({ label, children }: { label: string; children: ReactNode[] }) {
  children = children.filter(Boolean);
  if (!children.length) return null;
  return (
    <>
      <div className="nav-section" id={`nav-${label}`}>
        {label}
      </div>
      <ul className="nav-group" aria-labelledby={`nav-${label}`}>
        {children.map((c, i) => (
          <li key={i}>{c}</li>
        ))}
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
