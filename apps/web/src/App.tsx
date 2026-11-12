import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, Briefcase, HeartPulse, Building2, UserRound, CalendarPlus, ClipboardCheck, GitCompareArrows, Globe, Home, Inbox, ListChecks, LogOut, Network, OctagonX, Phone, PlugZap, ShieldCheck, Sparkles, Stethoscope } from "lucide-react";
import { createContext, useContext, useEffect, type ReactNode } from "react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { api, ApiError, type Me, type WorkspaceSummary } from "./api";
import logoDark from "./assets/careplix-logo-dark.png";
import logo from "./assets/careplix-logo.png";
import AgentEditor from "./pages/AgentEditor";
import Agents from "./pages/Agents";
import Approvals from "./pages/Approvals";
import Backfill from "./pages/Backfill";
import BrowserAgent from "./pages/BrowserAgent";
import Hub from "./pages/Hub";
import CallDetail from "./pages/CallDetail";
import Cases from "./pages/Cases";
import CareOpportunities from "./pages/CareOpportunities";
import Calls from "./pages/Calls";
import Connections from "./pages/Connections";
import Integrations from "./pages/Integrations";
import Practice from "./pages/Practice";
import ResultDetail from "./pages/ResultDetail";
import Personas from "./pages/Personas";
import Results from "./pages/Results";
import ShadowMode from "./pages/ShadowMode";
import TaskComposer from "./pages/TaskComposer";
import Tasks from "./pages/Tasks";
import VoiceStudio from "./pages/VoiceStudio";
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

function SignIn({ error }: { error?: string }) {
  const qc = useQueryClient();
  const mode = useQuery({ queryKey: ["auth-mode"], queryFn: () => api.get<{ mode: "dev" | "sso" }>("/auth/mode") });
  const sso = mode.data?.mode === "sso";
  const login = useMutation({
    // In the cloud, IAP has already authenticated the Google account; this exchanges it for a session.
    mutationFn: () => api.post<Me>(sso ? "/auth/iap-login" : "/auth/dev-login"),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["me"] }),
  });
  const { mutate, isIdle } = login;
  useEffect(() => {
    if (sso && isIdle && !error) mutate();
  }, [sso, isIdle, error, mutate]);
  return (
    <main className="signin">
      <div className="card signin-card">
        <BrandLockup size="lg" />
        <div>
          <h1 className="large-title">Sign in</h1>
          <p className="muted" style={{ margin: "6px 0 0" }}>
            Design, test and run voice agents for your revenue cycle team.
          </p>
        </div>
        <button className="btn btn-primary btn-lg" style={{ width: "100%" }} onClick={() => login.mutate()} disabled={login.isPending}>
          {login.isPending ? <span className="spinner" aria-label="Signing in" /> : sso ? "Continue with CarePlix account" : "Continue as Demo Administrator"}
        </button>
        {(login.error || error) && (
          <p className="field-error" role="alert" style={{ margin: 0 }}>
            {(login.error as Error)?.message ?? error}
          </p>
        )}
        <p className="small muted" style={{ margin: 0 }}>
          {sso ? "Signed in through CarePlix single sign-on." : "Demo workspace sign-in."} Synthetic data only. Never enter real patient information.
        </p>
      </div>
    </main>
  );
}

/** The CarePlix logo with the product name. The logo has a light-surface and a dark-surface file. */
function BrandLockup({ size = "md" }: { size?: "md" | "lg" }) {
  const height = size === "lg" ? 34 : 22;
  return (
    <span className="brand-lockup">
      <img className="brand-logo brand-logo-light" src={logo} alt="CarePlix" style={{ height }} />
      <img className="brand-logo brand-logo-dark" src={logoDark} alt="" aria-hidden style={{ height }} />
      <span className="brand-product" style={size === "lg" ? { fontSize: 20, lineHeight: "28px" } : undefined}>
        Voice
      </span>
    </span>
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
  const logout = useMutation({ mutationFn: () => api.post("/auth/logout"), onSuccess: () => qc.clear() });
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
          <div className="brand-workspace" title={me.workspace_name}>
            <Building2 size={14} aria-hidden style={{ flex: "none" }} />
            <strong>{me.workspace_name}</strong>
          </div>
        </div>
        <ul className="nav-group">
          <li>
            <NavLink to="/" end className="nav-link">
              <Home size={16} aria-hidden /> Home
            </NavLink>
          </li>
        </ul>
        <NavGroup label="Build">
          <NavLink to="/agents" className="nav-link">
            <Bot size={16} aria-hidden /> Agents
          </NavLink>
          <NavLink to="/studio" className="nav-link">
            <Sparkles size={16} aria-hidden /> Voice Studio
          </NavLink>
          <NavLink to="/personas" className="nav-link">
            <UserRound size={16} aria-hidden /> Personas
          </NavLink>
          <NavLink to="/shadow" className="nav-link">
            <GitCompareArrows size={16} aria-hidden /> Shadow mode
          </NavLink>
        </NavGroup>
        <NavGroup label="Operate">
          <NavLink to="/cases" className="nav-link">
            <Briefcase size={16} aria-hidden /> Cases
          </NavLink>
          <NavLink to="/tasks" className="nav-link">
            <ListChecks size={16} aria-hidden /> Tasks
          </NavLink>
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
          <NavLink to="/care" className="nav-link">
            <HeartPulse size={16} aria-hidden /> Care opportunities
          </NavLink>
          <NavLink to="/results" className="nav-link">
            <ClipboardCheck size={16} aria-hidden /> Results
            {review > 0 && (
              <span className="count" aria-label={`${review} to review`}>
                {review}
              </span>
            )}
          </NavLink>
        </NavGroup>
        <NavGroup label="Front office">
          <NavLink to="/practice" className="nav-link">
            <Stethoscope size={16} aria-hidden /> Practice
          </NavLink>
          <NavLink to="/backfill" className="nav-link">
            <CalendarPlus size={16} aria-hidden /> Backfill
          </NavLink>
        </NavGroup>
        <NavGroup label="Settings">
          <NavLink to="/connections" className="nav-link">
            <PlugZap size={16} aria-hidden /> Connections
          </NavLink>
          <NavLink to="/integrations" className="nav-link">
            <Building2 size={16} aria-hidden /> Integrations
          </NavLink>
          <NavLink to="/hub" className="nav-link">
            <Network size={16} aria-hidden /> EHR hub
          </NavLink>
        </NavGroup>
        <NavGroup label="Labs">
          <NavLink to="/browser" className="nav-link">
            <Globe size={16} aria-hidden /> Browser agent <span className="nav-tag">Preview</span>
          </NavLink>
          <NavLink to="/approvals" className="nav-link">
            <Inbox size={16} aria-hidden /> Approvals
          </NavLink>
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
          <Route path="/studio" element={<VoiceStudio />} />
          <Route path="/personas" element={<Personas />} />
          <Route path="/shadow" element={<ShadowMode />} />
          <Route path="/shadow/:id" element={<ShadowMode />} />
          <Route path="/browser" element={<BrowserAgent />} />
          <Route path="/browser/:id" element={<BrowserAgent />} />
          <Route path="/approvals" element={<Approvals />} />
          <Route path="/agents" element={<Agents />} />
          <Route path="/agents/:id" element={<AgentEditor />} />
          <Route path="/cases" element={<Cases />} />
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/tasks/new" element={<TaskComposer />} />
          <Route path="/calls" element={<Calls />} />
          <Route path="/calls/:id" element={<CallDetail />} />
          <Route path="/results" element={<Results />} />
          <Route path="/care" element={<CareOpportunities />} />
          <Route path="/results/:id" element={<ResultDetail />} />
          <Route path="/connections" element={<Connections />} />
          <Route path="/integrations" element={<Integrations />} />
          <Route path="/practice" element={<Practice />} />
          <Route path="/backfill" element={<Backfill />} />
          <Route path="/hub" element={<Hub />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

function NavGroup({ label, children }: { label: string; children: ReactNode[] }) {
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
