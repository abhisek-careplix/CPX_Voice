import { useQuery } from "@tanstack/react-query";
import { ChevronRight, ListChecks, Plus } from "lucide-react";
import { Link } from "react-router-dom";
import { api, type Task } from "../api";
import { useCan } from "../App";
import { Card, Empty, ErrorState, ListSkeleton, TaskPill, Toolbar, timeAgo } from "../ui";

export default function Tasks() {
  const tasks = useQuery({ queryKey: ["tasks"], queryFn: () => api.get<Task[]>("/tasks"), refetchInterval: 5000 });
  const canStart = useCan("start_task");
  return (
    <>
      <Toolbar
        eyebrow="Operate"
        title="Tasks"
        description="A task is a business objective, such as an authorization status for one case. One task can take several calls; its result waits under Results for a reviewer."
        actions={
          canStart && (
            <Link to="/tasks/new" className="btn btn-primary">
              <Plus size={16} aria-hidden /> New task
            </Link>
          )
        }
      />
      <div className="content">
        <Card>
          {tasks.isPending && <ListSkeleton rows={4} icon={false} />}
          {tasks.error && <ErrorState error={tasks.error} onRetry={() => tasks.refetch()} title="Couldn't load tasks" />}
          {tasks.data?.length === 0 ? (
            <Empty icon={ListChecks} title="No tasks yet" action={canStart && <Link to="/tasks/new" className="btn btn-primary"><Plus size={16} aria-hidden /> Create a task</Link>}>
              A task names a case, a published agent and an approved destination. Inbound calls create tasks automatically.
            </Empty>
          ) : (
            <ul className="list">
              {tasks.data?.map((t) => (
                <li key={t.id}>
                  <Link to={`/results/${t.id}`} className="list-row">
                    <div className="row-main">
                      <div className="row-title">
                        {t.case?.case_ref ?? "Unlinked inbound call"} · {t.kind === "inbound" ? "Inbound enquiry" : "Authorization status"}
                      </div>
                      <div className="row-sub">
                        {t.agent.name} v{t.agent.version} · {t.destination ? `to ${t.destination}` : "inbound"} · {timeAgo(t.created_at)}
                      </div>
                    </div>
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
