import { useQuery } from "@tanstack/react-query";
import { AudioLines, Bot, Check, ChevronRight, ClipboardCheck, Inbox, Phone, PhoneOutgoing, Plus, Radio, Sparkles } from "lucide-react";
import { Link } from "react-router-dom";
import { api, type Attempt, type Readiness, type WorkspaceSummary } from "../api";
import { useCan, useMe } from "../App";
import { AttemptPill, Card, CheckPill, DIRECTION_LABEL, Empty, ErrorState, ListSkeleton, TaskPill, Toolbar, duration, timeAgo, useNow } from "../ui";

const CHECK_LABEL: Record<string, string> = {
  database: "Database",
  livekit_api: "Voice platform",
  outbound_route: "Outbound phone route",
  inbound_route: "Inbound number",
  voice_model: "Voice model",
  agent_worker: "Agent runtime",
};

export default function Workspace() {
  const me = useMe();
  const canStart = useCan("start_task");
  const summary = useQuery({ queryKey: ["workspace-summary"], queryFn: () => api.get<WorkspaceSummary>("/workspace/summary"), refetchInterval: 4000 });
  const readiness = useQuery({ queryKey: ["readiness"], queryFn: () => api.get<Readiness>("/readiness"), refetchInterval: 30000 });
  useNow(1000, (summary.data?.active_calls.length ?? 0) > 0);
  const s = summary.data;
  const blockers = readiness.data?.checks.filter((c) => c.status === "blocked") ?? [];
  const hour = new Date().getHours();
  const greeting = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";

  return (
    <>
      <Toolbar
        eyebrow={new Date().toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" })}
        title={`${greeting}, ${me.display_name.split(" ")[0]}`}
        description="What's live, what needs a reviewer, and whether your workspace is ready to call."
        actions={
          canStart && (
            <Link to="/tasks/new" className="btn btn-primary">
              <Plus size={16} aria-hidden /> New task
            </Link>
          )
        }
      />
      <div className="content">
        <GettingStarted summary={s} readiness={readiness.data} />
        {summary.error && (
          <Card>
            <ErrorState error={summary.error} onRetry={() => summary.refetch()} title="Couldn't load your workspace" />
          </Card>
        )}
        <div className="grid-3">
          <Stat icon={Radio} tint="var(--tile-green)" value={s?.active_calls.length} label="Live calls" />
          <Stat icon={ClipboardCheck} tint="var(--tile-orange)" value={s?.needs_attention.length} label="Results needing review" />
          <Stat icon={Bot} tint="var(--tile-navy)" value={s ? `${s.agents.published}/${s.agents.total}` : undefined} label="Agents published" />
        </div>

        <div className="split">
          <div className="stack" style={{ gap: 20 }}>
            <Card title="Live now" subtitle="Calls in progress across browser and phone.">
              {!s && summary.isPending && <ListSkeleton rows={1} />}
              {s && s.active_calls.length === 0 ? (
                <Empty icon={Phone} title="No calls in progress">
                  Start a task to place an outbound call, or open an agent and run a Browser Test.
                </Empty>
              ) : (
                <ul className="list">
                  {s?.active_calls.map((a) => (
                    <li key={a.id}>
                      <Link to={`/calls/${a.id}`} className="list-row">
                        <div className="row-icon" style={{ background: a.direction === "browser" ? "var(--tile-purple)" : "var(--tile-green)" }}>
                          {a.direction === "browser" ? <AudioLines size={18} /> : <PhoneOutgoing size={18} />}
                        </div>
                        <div className="row-main">
                          <div className="row-title">
                            {a.agent.name} · {DIRECTION_LABEL[a.direction]}
                          </div>
                          <div className="row-sub tabular">
                            {a.case_ref ?? "No case"} · {duration(a.connected_at ?? a.created_at)}
                          </div>
                        </div>
                        <AttemptPill state={a.state} substate={a.substate} />
                        <ChevronRight size={16} className="chevron" aria-hidden />
                      </Link>
                    </li>
                  ))}
                </ul>
              )}
            </Card>

            <Card title="Needs your attention" subtitle="Calls have finished; a reviewer records the business outcome.">
              {!s && summary.isPending && <ListSkeleton rows={1} icon={false} />}
              {s && s.needs_attention.length === 0 ? (
                <Empty icon={Inbox} title="You're all caught up">
                  Results that need review will appear here.
                </Empty>
              ) : (
                <ul className="list">
                  {s?.needs_attention.map((t) => (
                    <li key={t.id}>
                      <Link to={`/results/${t.id}`} className="list-row">
                        <div className="row-main">
                          <div className="row-title">
                            {t.case?.case_ref ?? "Unlinked call"} · {t.agent.name}
                          </div>
                          <div className="row-sub">{t.next_action ?? t.objective}</div>
                        </div>
                        <TaskPill state={t.state} />
                        <ChevronRight size={16} className="chevron" aria-hidden />
                      </Link>
                    </li>
                  ))}
                </ul>
              )}
            </Card>

            <Card title="Recent outcomes" subtitle="Tasks a reviewer closed most recently.">
              {!s && summary.isPending && <ListSkeleton rows={2} icon={false} />}
              {s && s.recent_outcomes.length === 0 ? (
                <Empty icon={Sparkles} title="No outcomes yet">
                  Outcomes appear once a reviewer closes a task. Nothing here is sample data.
                </Empty>
              ) : (
                <ul className="list">
                  {s?.recent_outcomes.map((t) => (
                    <li key={t.id}>
                      <Link to={`/results/${t.id}`} className="list-row">
                        <div className="row-main">
                          <div className="row-title">{t.case?.case_ref ?? "Unlinked call"}</div>
                          <div className="row-sub">{t.outcome_reason}</div>
                        </div>
                        <span className="small muted">{timeAgo(t.updated_at)}</span>
                        <TaskPill state={t.state} />
                      </Link>
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          </div>

          <Card
            title="Readiness"
            subtitle="Each dependency is checked separately. A healthy app does not imply phone or model health."
          >
            <ul className="list">
              {(readiness.data?.checks ?? []).map((c) => (
                <li className="list-row" key={c.name} style={{ alignItems: "flex-start" }}>
                  <div className="row-main">
                    <div className="row-title">{CHECK_LABEL[c.name] ?? c.name}</div>
                    <div className="small muted" style={{ whiteSpace: "normal" }}>
                      {c.message}
                    </div>
                  </div>
                  <CheckPill status={c.status} />
                </li>
              ))}
              {readiness.isPending &&
                [0, 1, 2, 3].map((i) => (
                  <li className="list-row" key={i}>
                    <div className="skeleton" style={{ height: 14, width: "70%" }} />
                  </li>
                ))}
            </ul>
            <div className="card-pad stack" style={{ borderTop: "1px solid var(--separator)", gap: 10 }}>
              {blockers.length > 0 && (
                <span className="small muted">
                  {blockers.length} blocker{blockers.length > 1 ? "s" : ""} to resolve before real calls.
                </span>
              )}
              <Link to="/connections" className={`btn ${blockers.length ? "btn-primary" : "btn-secondary"}`} style={{ justifySelf: "start" }}>
                {blockers.length ? "Resolve in Connections" : "Open Connections"}
              </Link>
            </div>
          </Card>
        </div>
      </div>
    </>
  );
}

function Stat({ icon: Icon, tint, value, label }: { icon: typeof Phone; tint: string; value: number | string | undefined; label: string }) {
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

/** The self-serve path for a new workspace, in order. Each step links to where it's done and
 * ticks itself off from data the workspace already has. Hidden once every step is done. */
function GettingStarted({ summary, readiness }: { summary?: WorkspaceSummary; readiness?: Readiness }) {
  const scenarios = useQuery({ queryKey: ["scenarios"], queryFn: () => api.get<{ scenarios: unknown[] }>("/scenarios") });
  const calls = useQuery({ queryKey: ["calls"], queryFn: () => api.get<Attempt[]>("/calls") });
  if (!summary || !readiness || !scenarios.data || !calls.data) return null;
  const ready = (name: string) => readiness.checks.find((c) => c.name === name)?.status === "ready";
  const steps = [
    { to: "/connections", title: "Connect voice and phone", text: "Check the voice platform, model and outbound line.", done: ready("livekit_api") && ready("voice_model") && ready("agent_worker") },
    { to: "/agents", title: "Create an agent", text: "Start from a template; set its voice, tools and limits.", done: summary.agents.total > 0 },
    { to: "/studio", title: "Write test scenarios", text: "Describe who it calls, the goal and what counts as done.", done: scenarios.data.scenarios.length > 0 },
    { to: "/studio", title: "Rehearse or simulate", text: "Talk to it yourself, or listen while an AI plays the other side.", done: calls.data.some((c) => c.direction === "browser") },
    { to: "/agents", title: "Pass the suite and publish", text: "Run the regression suite on the draft, then publish it.", done: summary.agents.published > 0 },
    { to: "/calls", title: "Place a live call", text: "Call an approved number and review the result.", done: calls.data.some((c) => c.direction !== "browser") },
  ];
  const done = steps.filter((x) => x.done).length;
  if (done === steps.length) return null;
  const next = steps.findIndex((x) => !x.done);
  return (
    <Card title="Get set up" subtitle={`${done} of ${steps.length} done. Each step opens the page where you do it.`}>
      <ol className="setup-steps">
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
