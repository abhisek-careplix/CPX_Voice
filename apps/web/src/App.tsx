import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AudioLines, Bot, Building2, ClipboardCheck, LayoutGrid, ListChecks, LogOut, Phone, PlugZap, ShieldCheck } from "lucide-react";
import { createContext, useContext, useEffect } from "react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { api, ApiError, type Me, type WorkspaceSummary } from "./api";
import AgentEditor from "./pages/AgentEditor";
import Agents from "./pages/Agents";
import CallDetail from "./pages/CallDetail";
import Calls from "./pages/Calls";
import Connections from "./pages/Connections";
import Integrations from "./pages/Integrations";
import ResultDetail from "./pages/ResultDetail";
import Results from "./pages/Results";
import TaskComposer from "./pages/TaskComposer";
import Tasks from "./pages/Tasks";
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
        <div className="signin-mark">
          <AudioLines size={34} aria-hidden />
        </div>
        <div>
          <h1 className="large-title" style={{ fontSize: 28 }}>
            CarePlix Voice
          </h1>
          <p className="muted" style={{ margin: "6px 0 0" }}>
            Design, test and run voice agents.
          </p>
        </div>
        <button className="btn btn-primary btn-lg" style={{ width: "100%" }} onClick={() => login.mutate()} disabled={login.isPending}>
          {login.isPending ? <span className="spinner" aria-label="Signing in" /> : sso ? "Continue with CarePlix account" : "Continue as Demo Administrator"}
        </button>
        {(login.error || error) && (
          <p className="field-error" role="alert">
            {(login.error as Error)?.message ?? error}
          </p>
        )}
        <p className="small muted" style={{ margin: 0 }}>
          {sso ? "Signed in through CarePlix single sign-on." : "Development sign-in."} Synthetic data only — no patient information may be used.
        </p>
      </div>
    </main>
  );
}

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
      <nav className="sidebar" aria-label="Primary">
        <div className="brand">
          <div className="brand-mark">
            <AudioLines size={18} aria-hidden />
          </div>
          <div>
            <div className="brand-name">CarePlix Voice</div>
            <div className="brand-sub">{me.workspace_name}</div>
          </div>
        </div>
        <NavLink to="/" end className="nav-link">
          <LayoutGrid size={18} aria-hidden /> Workspace
        </NavLink>
        <NavLink to="/agents" className="nav-link">
          <Bot size={18} aria-hidden /> Agents
        </NavLink>
        <NavLink to="/tasks" className="nav-link">
          <ListChecks size={18} aria-hidden /> Tasks
        </NavLink>
        <NavLink to="/calls" className="nav-link">
          <Phone size={18} aria-hidden /> Calls
          {live > 0 && (
            <span className="count" aria-label={`${live} live`}>
              <span className="pill tone-green" style={{ padding: "0 7px" }}>
                <span className="live-dot" aria-hidden />
                {live}
              </span>
            </span>
          )}
        </NavLink>
        <NavLink to="/results" className="nav-link">
          <ClipboardCheck size={18} aria-hidden /> Results
          {review > 0 && <span className="count">{review}</span>}
        </NavLink>
        <div className="nav-section">Administration</div>
        <NavLink to="/connections" className="nav-link">
          <PlugZap size={18} aria-hidden /> Connections
        </NavLink>
        <NavLink to="/integrations" className="nav-link">
          <Building2 size={18} aria-hidden /> Integrations
        </NavLink>
        <div className="sidebar-foot">
          <span className="sample-label">
            <ShieldCheck size={13} aria-hidden /> Synthetic data only
          </span>
          {me.emergency_stop && <span className="pill tone-red">Calling stopped</span>}
          <div className="user-chip">
            <div className="avatar" aria-hidden>
              {me.display_name
                .split(" ")
                .map((p) => p[0])
                .join("")
                .slice(0, 2)}
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 500 }}>{me.display_name}</div>
              <div className="small muted" style={{ textTransform: "capitalize" }}>
                {me.role}
              </div>
            </div>
            <button className="btn btn-plain btn-icon" onClick={() => logout.mutate()} aria-label="Sign out" title="Sign out">
              <LogOut size={16} />
            </button>
          </div>
        </div>
      </nav>
      <main className="main">
        <Routes>
          <Route path="/" element={<Workspace />} />
          <Route path="/agents" element={<Agents />} />
          <Route path="/agents/:id" element={<AgentEditor />} />
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/tasks/new" element={<TaskComposer />} />
          <Route path="/calls" element={<Calls />} />
          <Route path="/calls/:id" element={<CallDetail />} />
          <Route path="/results" element={<Results />} />
          <Route path="/results/:id" element={<ResultDetail />} />
          <Route path="/connections" element={<Connections />} />
          <Route path="/integrations" element={<Integrations />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}
