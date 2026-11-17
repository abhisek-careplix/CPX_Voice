import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, Inbox as InboxIcon } from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";
import { api, ApiError, type Inbox as InboxData, type InboxItem, type InboxKind } from "../api";
import { Banner, Card, Empty, ErrorState, ListSkeleton, Pill, Segmented, Toolbar, useToast } from "../ui";

const KIND_TONE: Record<InboxKind, "blue" | "orange" | "green" | "gray" | "red" | "purple"> = {
  result_review: "blue",
  ehr_write: "orange",
  browser_plan: "purple",
  browser_step: "purple",
  clinician_signoff: "green",
  hand_off: "orange",
  auth_packet: "orange",
  escalation: "red",
};

const APPROVER: Record<InboxItem["approver"], string> = {
  agent: "Agent decides",
  staff: "Staff approve",
  admin: "Administrator approves",
  supervisor: "Run supervisor approves",
  clinician: "Clinician signs",
};

const DECISION_LABEL: Record<string, string> = { approve: "Approve", reject: "Reject" };

const waited = (s: number) => (s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)} min` : s < 86400 ? `${Math.floor(s / 3600)} h` : `${Math.floor(s / 86400)} d`);

/** The one review inbox (D5): call results, EHR writes, browser approvals, sign-offs, overdue cases. */
export default function Inbox() {
  // The kind filter lives in the address, so the Clinical menu can open "/inbox?kind=clinician_signoff".
  const [params, setParams] = useSearchParams();
  const kind = (params.get("kind") as InboxKind | null) ?? "all";
  const setKind = (k: "all" | InboxKind) => setParams(k === "all" ? {} : { kind: k }, { replace: true });
  const signoffs = kind === "clinician_signoff";
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery({ queryKey: ["inbox"], queryFn: () => api.get<InboxData>("/inbox"), refetchInterval: 4000 });
  const decide = useMutation({
    mutationFn: (body: { kind: InboxKind; id: string; decision: string }) => api.post<InboxData>("/inbox/decide", body),
    onSuccess: (data, body) => {
      qc.setQueryData(["inbox"], data);
      qc.invalidateQueries({ queryKey: ["cases"] });
      toast(body.decision === "reject" ? "Rejected" : body.kind === "clinician_signoff" ? "Signed off" : "Approved");
    },
  });
  const data = q.data;
  const items = (data?.items ?? []).filter((i) => kind === "all" || i.kind === kind);
  const present = (data?.kinds ?? []).filter((k) => data?.counts[k.kind] || k.kind === kind);
  return (
    <>
      <Toolbar
        eyebrow={signoffs ? "Clinical" : "Work"}
        title={signoffs ? "Clinician review" : "Review inbox"}
        description={
          signoffs
            ? "What an agent prepared and only a clinician may approve. Nothing clinical reaches a patient until it is signed off here."
            : "Everything waiting for a person, longest wait first: call results to review, EHR writes to approve, browser-agent approvals, clinician sign-offs and overdue cases. The approval policy decides who signs each one."
        }
      />
      <div className="content">
        {present.length > 0 && (
          <Segmented
            label="Kind"
            value={kind}
            onChange={setKind}
            options={[{ value: "all", label: `All ${data?.total ?? 0}` }, ...present.map((k) => ({ value: k.kind, label: `${k.label} ${data?.counts[k.kind] ?? 0}` }))]}
          />
        )}
        <Card>
          {q.isPending && <ListSkeleton rows={4} />}
          {q.error && <ErrorState error={q.error} onRetry={() => q.refetch()} title="Couldn't load the inbox" />}
          {data && items.length === 0 && (
            <Empty icon={InboxIcon} title="Nothing is waiting">
              {signoffs ? "When an agent prepares something a clinician must approve, it appears here." : "When an agent or a playbook needs a decision, it appears here."}
            </Empty>
          )}
          {items.length > 0 && (
            <ul className="list">
              {items.map((i) => (
                <li key={`${i.kind}-${i.id}`} className="list-row" style={{ flexWrap: "wrap" }}>
                  <div className="row-main" style={{ flex: "1 1 240px" }}>
                    <div className="row-title">{i.title}</div>
                    <div className="row-sub">
                      {[i.detail, `waiting ${waited(i.waiting_s)}`, APPROVER[i.approver], i.mine === false && i.kind.startsWith("browser") ? "another supervisor's run" : null, i.mine && !i.kind.startsWith("browser") ? "yours" : null].filter(Boolean).join(" · ")}
                      {i.case && (
                        <>
                          {" · "}
                          <Link to={`/cases?case=${i.case.id}`}>{i.case.case_ref}</Link>
                        </>
                      )}
                    </div>
                  </div>
                  <Pill tone={KIND_TONE[i.kind]}>{i.kind_label}</Pill>
                  {i.decisions.length > 0 && i.can_decide ? (
                    i.decisions.map((d) => (
                      <button key={d} className={d === "approve" ? "btn btn-primary" : "btn btn-secondary"} disabled={decide.isPending} onClick={() => decide.mutate({ kind: i.kind, id: i.id, decision: d })}>
                        {i.kind === "clinician_signoff" && d === "approve" ? "Sign off" : DECISION_LABEL[d] ?? d}
                      </button>
                    ))
                  ) : (
                    <Link className="btn btn-secondary" to={i.href}>
                      Open <ChevronRight size={14} aria-hidden />
                    </Link>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Card>
        {decide.error && <Banner tone="red" title={decide.error instanceof ApiError ? decide.error.message : "Couldn't record that decision"} />}
        <p className="small muted" style={{ margin: 0 }}>
          The earlier pages still work: <Link to="/results">Results</Link>, <Link to="/approvals">browser Approvals</Link> and the write queue on <Link to="/practice">Practice</Link>.
        </p>
      </div>
    </>
  );
}
