import { useQuery } from "@tanstack/react-query";
import { ChevronRight, ClipboardCheck } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type Task } from "../api";
import { Card, Empty, Segmented, TaskPill, Toolbar, timeAgo } from "../ui";

export default function Results() {
  const tasks = useQuery({ queryKey: ["tasks"], queryFn: () => api.get<Task[]>("/tasks"), refetchInterval: 5000 });
  const [filter, setFilter] = useState<"review" | "closed">("review");
  const rows = (tasks.data ?? []).filter((t) => (filter === "review" ? t.state === "Review" : ["Completed", "Partial", "Unsuccessful", "Cancelled"].includes(t.state)));
  return (
    <>
      <Toolbar title="Results" eyebrow="Evidence-backed business outcomes" />
      <div className="content">
        <Segmented
          label="Filter results"
          value={filter}
          onChange={setFilter}
          options={[
            { value: "review", label: "Needs review" },
            { value: "closed", label: "Closed" },
          ]}
        />
        <Card>
          {rows.length === 0 ? (
            <Empty icon={ClipboardCheck} title={filter === "review" ? "Nothing to review" : "No closed results"}>
              A finished call is not a finished task: results wait here until a reviewer records the outcome.
            </Empty>
          ) : (
            <ul className="list">
              {rows.map((t) => (
                <li key={t.id}>
                  <Link to={`/results/${t.id}`} className="list-row">
                    <div className="row-main">
                      <div className="row-title">
                        {t.case?.case_ref ?? "Unlinked inbound call"} · {t.agent.name}
                      </div>
                      <div className="row-sub">{t.state === "Review" ? t.next_action : t.outcome_reason}</div>
                    </div>
                    <span className="small muted">{timeAgo(t.updated_at)}</span>
                    <TaskPill state={t.state} />
                    <ChevronRight size={16} className="chevron" aria-hidden />
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </>
  );
}
