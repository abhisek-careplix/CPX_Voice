import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Circle, CircleDashed, MessageSquare, Phone, Play, RotateCcw, Stethoscope } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type CaseDetail, type ContactAttempt, type ContactState } from "../api";
import { useCan } from "../App";
import { Banner, Pill, timeAgo, useToast } from "../ui";

/** One mutation shape for every case action: post, then show the case the server returns. */
function useCaseAction(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ method, path, body }: { method: "post" | "put"; path: string; body?: unknown }) =>
      method === "post" ? api.post<CaseDetail>(path, body) : api.put<CaseDetail>(path, body),
    onSuccess: (c) => {
      qc.setQueryData(["case", id], c);
      qc.invalidateQueries({ queryKey: ["cases"] });
      qc.invalidateQueries({ queryKey: ["inbox"] });
    },
  });
}

const errorText = (e: unknown) => (e instanceof ApiError ? e.message : e ? "Something went wrong." : null);

/** The completion contract (§2): what's proven, and what's still missing before "done". */
export function ContractSection({ c }: { c: CaseDetail }) {
  const canStart = useCan("start_task");
  const canReview = useCan("review");
  const act = useCaseAction(c.id);
  const [ack, setAck] = useState({ system: "", ack: "" });
  const [told, setTold] = useState({ audience: c.contract_spec.informed[0] ?? "staff", channel: "call", label: "" });
  const open = !["done", "failed", "closed"].includes(c.status);
  return (
    <section className="stack" style={{ gap: 8 }}>
      <h3 className="field-label">Completion contract</h3>
      <ul className="list card" style={{ boxShadow: "none" }}>
        {c.contract.parts.map((p) => (
          <li key={p.key} className="row" style={{ gap: 10, padding: "8px 12px", alignItems: "flex-start" }}>
            {p.met ? <CheckCircle2 size={16} aria-label="Met" style={{ color: "var(--green)", flex: "none", marginTop: 2 }} /> : <Circle size={16} aria-label="Not yet" style={{ color: "var(--label-3)", flex: "none", marginTop: 2 }} />}
            <div style={{ minWidth: 0 }}>
              <div>{p.label}</div>
              <div className="small muted" style={{ overflowWrap: "anywhere" }}>
                {p.detail}
              </div>
            </div>
          </li>
        ))}
      </ul>
      {c.links.length > 0 && (
        <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
          {c.links.map((l) => (
            <li key={l.id}>
              <span className="muted">{l.part === "evidence" ? "Evidence" : `Told ${l.audience}`}:</span> {l.label}
              {l.channel && <span className="muted"> · {l.channel}</span>}
              {l.delivery && l.delivery !== "delivered" && <span className="muted"> · {l.delivery}</span>}
              {l.kind === "task" && l.ref && (
                <>
                  {" "}
                  · <Link to={`/results/${l.ref}`}>result</Link>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
      {open && canStart && !c.contract.holds && (
        <div className="stack" style={{ gap: 10 }}>
          {c.contract.missing.includes("system_of_record") && (
            <form
              className="row"
              style={{ gap: 6, flexWrap: "wrap" }}
              onSubmit={(e) => {
                e.preventDefault();
                act.mutate({ method: "post", path: `/cases/${c.id}/ack`, body: ack });
              }}
            >
              <input className="input" style={{ flex: "1 1 120px" }} placeholder="System (e.g. nexhealth, payer)" value={ack.system} onChange={(e) => setAck({ ...ack, system: e.target.value })} aria-label="System of record" />
              <input className="input" style={{ flex: "2 1 160px" }} placeholder="Acknowledgement or reference id" value={ack.ack} onChange={(e) => setAck({ ...ack, ack: e.target.value })} aria-label="Acknowledgement id" />
              <button className="btn btn-secondary" disabled={!ack.system.trim() || !ack.ack.trim() || act.isPending}>
                Record update
              </button>
            </form>
          )}
          {c.contract.missing.includes("informed") && (
            <form
              className="row"
              style={{ gap: 6, flexWrap: "wrap" }}
              onSubmit={(e) => {
                e.preventDefault();
                act.mutate({ method: "post", path: `/cases/${c.id}/informed`, body: { ...told, label: told.label || `Told the ${told.audience}` } });
              }}
            >
              <select className="select" style={{ width: "auto" }} value={told.audience} onChange={(e) => setTold({ ...told, audience: e.target.value })} aria-label="Who was told">
                {["patient", "caregiver", "staff", "payer"].map((a) => (
                  <option key={a}>{a}</option>
                ))}
              </select>
              <select className="select" style={{ width: "auto" }} value={told.channel} onChange={(e) => setTold({ ...told, channel: e.target.value })} aria-label="How">
                {["call", "text", "email", "fax", "portal", "studio", "in_person"].map((ch) => (
                  <option key={ch}>{ch}</option>
                ))}
              </select>
              <input className="input" style={{ flex: "1 1 160px" }} placeholder="What they were told (no patient details)" value={told.label} onChange={(e) => setTold({ ...told, label: e.target.value })} aria-label="Message" />
              <button className="btn btn-secondary" disabled={act.isPending}>
                Record message
              </button>
            </form>
          )}
          <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
            {c.contract.missing.includes("next_action") && (
              <button className="btn btn-secondary" disabled={act.isPending} onClick={() => act.mutate({ method: "put", path: `/cases/${c.id}/next-action`, body: { nothing_left: true } })}>
                Nothing left to do
              </button>
            )}
            {c.contract.missing.includes("clinician") && canReview && (
              <button className="btn btn-secondary" disabled={act.isPending} onClick={() => act.mutate({ method: "post", path: `/cases/${c.id}/clinician-signoff` })}>
                <Stethoscope size={14} aria-hidden /> Sign off as clinician
              </button>
            )}
          </div>
        </div>
      )}
      {act.error && <Banner tone="red" title={errorText(act.error) ?? ""} />}
    </section>
  );
}

const CONTACT_STATE: Record<ContactState, { label: string; tone: "blue" | "orange" | "green" | "gray" | "red" | "purple" }> = {
  queued: { label: "Queued", tone: "gray" },
  sent: { label: "Waiting", tone: "blue" },
  confirmed: { label: "Said yes", tone: "green" },
  declined: { label: "Said no", tone: "gray" },
  no_answer: { label: "No answer", tone: "orange" },
  failed: { label: "Couldn't send", tone: "red" },
  superseded: { label: "Told it's filled", tone: "gray" },
  too_late: { label: "Yes, too late", tone: "purple" },
  cancelled: { label: "Cancelled", tone: "gray" },
};

/** The playbook working this case, and the people it contacted. */
export function PlaybookSection({ c, hasPlaybook }: { c: CaseDetail; hasPlaybook: boolean }) {
  const canStart = useCan("start_task");
  const act = useCaseAction(c.id);
  const toast = useToast();
  const run = c.playbook_run;
  if (!run) {
    if (!hasPlaybook || !canStart || ["done", "failed", "closed"].includes(c.status)) return null;
    return (
      <section className="stack" style={{ gap: 6 }}>
        <h3 className="field-label">Playbook</h3>
        <p className="small muted" style={{ margin: 0 }}>
          A playbook can work this case for you: contact, wait for the answer, update the system of record, tell staff and check the completion contract.
        </p>
        <div>
          <button className="btn btn-secondary" disabled={act.isPending} onClick={() => act.mutate({ method: "post", path: `/cases/${c.id}/playbook` }, { onSuccess: () => toast("Playbook started") })}>
            <Play size={14} aria-hidden /> Run the playbook
          </button>
        </div>
        {act.error && <Banner tone="red" title={errorText(act.error) ?? ""} />}
      </section>
    );
  }
  const stopped = ["blocked", "handed_off", "failed"].includes(run.state);
  return (
    <section className="stack" style={{ gap: 8 }}>
      <div className="row" style={{ gap: 8 }}>
        <h3 className="field-label" style={{ margin: 0 }}>
          Playbook: {run.name}
        </h3>
        <Pill tone={run.state === "done" ? "green" : stopped ? "orange" : "blue"}>{run.state.replace("_", " ")}</Pill>
        <span className="spacer" />
        {stopped && canStart && (
          <button className="btn btn-secondary" disabled={act.isPending} onClick={() => act.mutate({ method: "post", path: `/cases/${c.id}/playbook/resume` })}>
            <RotateCcw size={14} aria-hidden /> Resume
          </button>
        )}
      </div>
      <ol className="small" style={{ margin: 0, paddingLeft: 0, listStyle: "none" }}>
        {run.steps.map((s, i) => (
          <li key={i} className="row" style={{ gap: 8, padding: "3px 0" }}>
            {s.state === "done" ? <CheckCircle2 size={14} aria-hidden style={{ color: "var(--green)" }} /> : s.state === "current" ? <CircleDashed size={14} aria-hidden style={{ color: "var(--accent-text)" }} /> : <Circle size={14} aria-hidden style={{ color: "var(--label-3)" }} />}
            <span className={s.state === "todo" ? "muted" : undefined}>{s.label}</span>
          </li>
        ))}
      </ol>
      {c.attempts.length > 0 && <Attempts c={c} />}
      {act.error && <Banner tone="red" title={errorText(act.error) ?? ""} />}
    </section>
  );
}

function Attempts({ c }: { c: CaseDetail }) {
  const canStart = useCan("start_task");
  const act = useCaseAction(c.id);
  const reply = (a: ContactAttempt, answer: "yes" | "no" | "no_answer") => act.mutate({ method: "post", path: `/attempts/${a.id}/reply`, body: { answer } });
  return (
    <div className="stack" style={{ gap: 4 }}>
      <div className="small muted">
        {c.attempts.length > 1 ? "Contacted at once. The first confirmed yes wins; everyone else is told it's filled." : "Contact attempts"}
      </div>
      <ul className="list card" style={{ boxShadow: "none" }}>
        {c.attempts.map((a) => (
          <li key={a.id} className="row" style={{ gap: 8, padding: "8px 12px", flexWrap: "wrap" }}>
            {a.channel === "call" ? <Phone size={14} aria-hidden /> : <MessageSquare size={14} aria-hidden />}
            <div style={{ flex: "1 1 160px", minWidth: 0 }}>
              <div>
                {a.person} <span className="muted">· {a.channel}</span>
                {a.address && <span className="mono muted"> {a.address}</span>}
                {c.winner_attempt_id === a.id && <span className="muted"> · winner</span>}
              </div>
              <div className="small muted">
                {[a.round > 1 && `try ${a.round}`, a.detail, a.notice && `notice ${a.notice}`, timeAgo(a.answered_at ?? a.created_at)].filter(Boolean).join(" · ")}
                {a.task_id && (
                  <>
                    {" "}
                    · <Link to={`/results/${a.task_id}`}>call</Link>
                  </>
                )}
              </div>
            </div>
            <Pill tone={CONTACT_STATE[a.state].tone}>{CONTACT_STATE[a.state].label}</Pill>
            {canStart && ["sent", "queued", "no_answer", "superseded"].includes(a.state) && (
              <span className="row" style={{ gap: 4 }}>
                <button className="btn btn-plain" disabled={act.isPending} onClick={() => reply(a, "yes")} title="Record that they said yes">
                  Yes
                </button>
                <button className="btn btn-plain" disabled={act.isPending} onClick={() => reply(a, "no")} title="Record that they said no">
                  No
                </button>
              </span>
            )}
          </li>
        ))}
      </ul>
      {act.error && <Banner tone="red" title={errorText(act.error) ?? ""} />}
    </div>
  );
}

/** Owner, due date and next step; and moving the case to done, handed off, blocked or failed. */
export function CaseActions({ c }: { c: CaseDetail }) {
  const canStart = useCan("start_task");
  const act = useCaseAction(c.id);
  const toast = useToast();
  const [mode, setMode] = useState<"handed_off" | "blocked" | "failed" | null>(null);
  const [text, setText] = useState("");
  const open = !["done", "failed", "closed"].includes(c.status);
  const move = (status: string, extra: Record<string, unknown> = {}) =>
    act.mutate(
      { method: "post", path: `/cases/${c.id}/status`, body: { status, ...extra } },
      {
        onSuccess: () => {
          setMode(null);
          setText("");
          toast(status === "done" ? "Case done" : "Case updated");
        },
      },
    );
  return (
    <section className="stack" style={{ gap: 8 }}>
      <h3 className="field-label">Next action</h3>
      <div className="small">
        {c.nothing_left ? (
          "Nothing left to do."
        ) : c.next_step ? (
          <>
            {c.next_step}
            <span className="muted">
              {" "}
              · {c.owner ? c.owner.name : "no owner"}
              {c.due_at && ` · due ${new Date(c.due_at).toLocaleString()}`}
            </span>
          </>
        ) : (
          <span className="muted">
            {c.owner ? `Owner: ${c.owner.name}` : "No owner yet"}
            {c.due_at && ` · due ${new Date(c.due_at).toLocaleString()}`}
          </span>
        )}
        {c.overdue && <span style={{ color: "var(--red)" }}> · overdue{c.escalated ? ", escalated to the owner" : ""}</span>}
      </div>
      {c.status_reason && <div className="small muted">Why: {c.status_reason}</div>}
      {open && canStart && (
        <>
          <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
            <button className="btn btn-primary" disabled={act.isPending} onClick={() => move("done")} title={c.contract.holds ? "Close it as done" : "Done needs the whole completion contract"}>
              Mark done
            </button>
            <button className="btn btn-secondary" onClick={() => setMode(mode === "handed_off" ? null : "handed_off")}>
              Hand off
            </button>
            <button className="btn btn-secondary" onClick={() => setMode(mode === "blocked" ? null : "blocked")}>
              Blocked
            </button>
            <button className="btn btn-secondary" onClick={() => setMode(mode === "failed" ? null : "failed")}>
              Failed
            </button>
          </div>
          {mode && (
            <form
              className="row"
              style={{ gap: 6 }}
              onSubmit={(e) => {
                e.preventDefault();
                move(mode, mode === "handed_off" ? { next_step: text } : { reason: text });
              }}
            >
              <input className="input" autoFocus placeholder={mode === "handed_off" ? "The next step for the owner (you, unless you change it)" : "Why"} value={text} onChange={(e) => setText(e.target.value)} aria-label={mode === "handed_off" ? "Next step" : "Reason"} />
              <button className="btn btn-secondary" disabled={!text.trim() || act.isPending}>
                Save
              </button>
            </form>
          )}
        </>
      )}
      {act.error && <Banner tone="red" title={errorText(act.error) ?? ""} />}
    </section>
  );
}
