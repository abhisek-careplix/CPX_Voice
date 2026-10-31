import { useQuery } from "@tanstack/react-query";
import { AudioLines, Bot, ChevronRight, ClipboardCheck, Inbox, Phone, PhoneOutgoing, Plus, Radio, Sparkles } from "lucide-react";
import { Link } from "react-router-dom";
import { api, type Readiness, type WorkspaceSummary } from "../api";
import { useCan, useMe } from "../App";
import { AttemptPill, Card, CheckPill, DIRECTION_LABEL, Empty, TaskPill, Toolbar, duration, timeAgo, useNow } from "../ui";

const CHECK_LABEL: Record<string, string> = {
  database: "Database",
  livekit_api: "Voice platform (LiveKit)",
  outbound_route: "Outbound phone route",
  inbound_route: "Inbound number",
  voice_model: "Voice model (Gemini Live)",
  agent_worker: "Agent worker",
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
        actions={
          canStart && (
            <Link to="/tasks/new" className="btn btn-primary">
              <Plus size={16} aria-hidden /> New Task
            </Link>
          )
        }
      />
      <div className="content">
        <div className="grid-3">
          <Stat icon={Radio} tint="#34c759" value={s?.active_calls.length} label="Live calls" />
          <Stat icon={ClipboardCheck} tint="#ff9f0a" value={s?.needs_attention.length} label="Results needing review" />
          <Stat icon={Bot} tint="#0071e3" value={s ? `${s.agents.published}/${s.agents.total}` : undefined} label="Agents published" />
        </div>

        <div className="split">
          <div className="stack" style={{ gap: 20 }}>
            <Card title="Live now" subtitle="Calls in progress across browser and phone.">
              {s && s.active_calls.length === 0 ? (
                <Empty icon={Phone} title="No calls in progress">
                  Start a task to place an outbound call, or open an agent and run a Browser Test.
                </Empty>
              ) : (
                <ul className="list">
                  {s?.active_calls.map((a) => (
                    <li key={a.id}>
                      <Link to={`/calls/${a.id}`} className="list-row">
                        <div className="row-icon" style={{ background: a.direction === "browser" ? "#af52de" : "#34c759" }}>
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

            <Card title="Recent outcomes">
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
            actions={
              <Link to="/connections" className="btn btn-plain small">
                Details
              </Link>
            }
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
            {blockers.length > 0 && (
              <div className="card-pad small muted" style={{ borderTop: "1px solid var(--separator)" }}>
                {blockers.length} blocker{blockers.length > 1 ? "s" : ""} to resolve before real calls. Configure them in <code className="mono">.env</code> and the LiveKit project.
              </div>
            )}
          </Card>
        </div>
      </div>
    </>
  );
}

function Stat({ icon: Icon, tint, value, label }: { icon: typeof Phone; tint: string; value: number | string | undefined; label: string }) {
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
