import { useQuery } from "@tanstack/react-query";
import { AudioLines, ChevronRight, Phone, PhoneIncoming, PhoneOutgoing } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type Attempt } from "../api";
import { AttemptPill, Card, DIRECTION_LABEL, Empty, ErrorState, ListSkeleton, Segmented, Toolbar, duration, timeAgo, useNow } from "../ui";

const ICON = { browser: AudioLines, outbound: PhoneOutgoing, inbound: PhoneIncoming };
const TINT = { browser: "var(--tile-purple)", outbound: "var(--tile-blue)", inbound: "var(--tile-teal)" };
const ACTIVE = ["Created", "Queued", "Dialing", "Connected", "Ending"];

export default function Calls() {
  const calls = useQuery({ queryKey: ["calls"], queryFn: () => api.get<Attempt[]>("/calls"), refetchInterval: 3000 });
  const [filter, setFilter] = useState<"all" | "live" | "phone" | "browser">("all");
  const rows = (calls.data ?? []).filter((c) =>
    filter === "live" ? ACTIVE.includes(c.state) : filter === "phone" ? c.direction !== "browser" : filter === "browser" ? c.direction === "browser" : true,
  );
  useNow(1000, rows.some((c) => ACTIVE.includes(c.state)));

  return (
    <>
      <Toolbar eyebrow="Operate" title="Calls" description="Every call attempt, live and finished: phone calls, browser rehearsals and simulations. Open one for its transcript, findings and what the agent was told." />
      <div className="content">
        <Segmented
          label="Filter calls"
          value={filter}
          onChange={setFilter}
          options={[
            { value: "all", label: "All" },
            { value: "live", label: "Live" },
            { value: "phone", label: "Phone" },
            { value: "browser", label: "Browser Tests" },
          ]}
        />
        <Card>
          {calls.isPending && <ListSkeleton rows={4} />}
          {calls.error && <ErrorState error={calls.error} onRetry={() => calls.refetch()} title="Couldn't load calls" />}
          {calls.data && rows.length === 0 ? (
            <Empty icon={Phone} title={filter === "all" ? "No calls yet" : "No calls match this filter"} action={filter !== "all" && <button className="btn btn-secondary" onClick={() => setFilter("all")}>Show all calls</button>}>
              {filter === "all" ? <>Calls appear here as soon as they start. Rehearse or simulate a scenario in <Link to="/studio">Voice Studio</Link> to make your first one.</> : null}
            </Empty>
          ) : (
            <ul className="list">
              {rows.map((c) => {
                const Icon = ICON[c.direction];
                return (
                  <li key={c.id}>
                    <Link to={`/calls/${c.id}`} className="list-row">
                      <div className="row-icon" style={{ background: TINT[c.direction] }}>
                        <Icon size={17} />
                      </div>
                      <div className="row-main">
                        <div className="row-title">
                          {DIRECTION_LABEL[c.direction]} · {c.agent.name} v{c.agent.version}
                          {c.agent.version_state === "draft" ? " (draft)" : ""}
                        </div>
                        <div className="row-sub tabular">
                          {c.case_ref ?? "No case"} · {c.destination ?? c.caller_reported ?? "browser"} · {timeAgo(c.created_at)}
                          {c.connected_at ? ` · ${duration(c.connected_at, c.ended_at)}` : ""}
                          {c.disposition && !ACTIVE.includes(c.state) ? ` · ${c.disposition.replace(/_/g, " ")}` : ""}
                        </div>
                      </div>
                      <AttemptPill state={c.state} substate={c.substate} />
                      <ChevronRight size={16} className="chevron" aria-hidden />
                    </Link>
                  </li>
                );
              })}
            </ul>
          )}
        </Card>
      </div>
    </>
  );
}
