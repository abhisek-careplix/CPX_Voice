import { useQuery } from "@tanstack/react-query";
import { Inbox } from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Card, Empty, ErrorState, ListSkeleton, Pill, Toolbar } from "../ui";
import { RISK } from "./BrowserAgent";

interface Item {
  type: "plan" | "step";
  run_id: string;
  step_id?: string;
  task: string;
  site: string;
  title: string;
  risk: keyof typeof RISK | null;
  waiting_s: number;
  mine: boolean;
}

const waited = (s: number) => (s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)} min` : `${Math.floor(s / 3600)} h`);

export default function Approvals() {
  const q = useQuery({ queryKey: ["approvals"], queryFn: () => api.get<{ items: Item[] }>("/browser/approvals"), refetchInterval: 3000 });
  const items = q.data?.items ?? [];
  return (
    <>
      <Toolbar eyebrow="Labs" title="Approvals" description="Plans and steps the browser agent is waiting on, longest wait first. Open a run to approve from its live view." />
      <div className="content">
      <Card title="Waiting for a person">
        {q.isPending && <ListSkeleton rows={2} icon={false} />}
        {q.error && <ErrorState error={q.error} onRetry={() => q.refetch()} title="Couldn't load approvals" />}
        {q.data && items.length === 0 ? (
          <Empty icon={Inbox} title="Nothing is waiting">When an agent needs a decision it appears here.</Empty>
        ) : (
          <ul className="list">
            {items.map((i) => (
              <li key={i.step_id ?? `plan-${i.run_id}`}>
                <Link className="list-row" to={`/browser/${i.run_id}`}>
                  <div className="row-main">
                    <div className="row-title">{i.title}</div>
                    <div className="row-sub">
                      {i.site} · {i.task} · waiting {waited(i.waiting_s)}
                      {i.mine ? "" : " · another supervisor's run"}
                    </div>
                  </div>
                  {i.risk ? <Pill tone={RISK[i.risk].tone}>{RISK[i.risk].label}</Pill> : <Pill tone="orange">Plan</Pill>}
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
