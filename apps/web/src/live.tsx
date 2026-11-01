import { useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Circle, CircleDashed, ShieldAlert, Wrench } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { CallEvent, Finding } from "./api";
import { FindingPill, humanize } from "./ui";

/** Live call timeline over SSE. Replays from the start, so a refresh restores the same view (T11). */
export function useCallEvents(attemptId: string | undefined) {
  const [events, setEvents] = useState<CallEvent[]>([]);
  const [ended, setEnded] = useState(false);
  const [connected, setConnected] = useState(false);
  const qc = useQueryClient();

  useEffect(() => {
    if (!attemptId) return;
    setEvents([]);
    setEnded(false);
    const source = new EventSource(`/api/calls/${attemptId}/events?cursor=0`);
    const seen = new Set<number>();
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);
    source.addEventListener("call", (msg) => {
      const event = JSON.parse((msg as MessageEvent).data) as CallEvent;
      if (seen.has(event.seq)) return;
      seen.add(event.seq);
      setEvents((xs) => [...xs, event]);
      if (event.type.startsWith("finding.") || event.type.startsWith("tool.") || event.type.startsWith("attempt.") || event.type === "case.linked") {
        qc.invalidateQueries({ queryKey: ["call", attemptId] });
        qc.invalidateQueries({ queryKey: ["workspace-summary"] });
      }
    });
    source.addEventListener("end", () => {
      setEnded(true);
      source.close();
      qc.invalidateQueries({ queryKey: ["call", attemptId] });
    });
    return () => source.close();
  }, [attemptId, qc]);

  return { events, ended, connected };
}

const STATE_TEXT: Record<string, string> = {
  Queued: "Starting the agent",
  Dialing: "Dialing",
  Connected: "Connected",
  Ending: "Ending call",
  Ended: "Call ended",
  Failed: "Call could not connect",
};

const QUIET_TOOLS = new Set([
  "record_finding",
  "set_call_state",
  "find_patient",
  "book_appointment",
  "reschedule_appointment",
  "cancel_appointment",
  "leave_message",
]);

const APPOINTMENT_OUTCOME: Record<string, string> = {
  booked: "booked in the EHR",
  rescheduled: "moved in the EHR",
  cancelled: "cancelled in the EHR",
  requested: "sent to the front desk to approve",
  pending: "waiting for the EHR",
  slot_taken: "time was just taken",
  failed: "refused by the EHR",
  needs_staff: "needs the front desk",
  front_desk: "this EHR needs the front desk to do it",
};

function timelineText(e: CallEvent): string | null {
  const p = e.payload as Record<string, string>;
  switch (e.type) {
    case "attempt.state_changed":
      return STATE_TEXT[p.to] ?? null;
    case "attempt.substate_changed":
      return { IVR: "Phone menu", Hold: "On hold — agent is waiting silently", Conversation: "Talking with a person" }[p.to] ?? null;
    case "worker.claimed":
      return "Agent joined";
    case "tool.succeeded":
      // These have their own, clearer timeline entries.
      if (QUIET_TOOLS.has(p.tool)) return null;
      return p.tool === "send_dtmf" ? "Pressed keypad digits" : humanize(p.tool);
    case "tool.rejected":
      return `Blocked: ${humanize(p.tool)} (${p.rejection_code?.replace(/_/g, " ")})`;
    case "case.linked":
      return `Caller verified · ${p.case_ref}`;
    case "patient.verified":
      return "Caller verified as a patient";
    case "patient.verification_failed":
      return "Caller could not be verified";
    case "appointment.booking":
      return `Booking: ${APPOINTMENT_OUTCOME[p.outcome] ?? p.outcome}`;
    case "appointment.reschedule":
      return `Reschedule: ${APPOINTMENT_OUTCOME[p.outcome] ?? p.outcome}`;
    case "appointment.cancel":
      return `Cancellation: ${APPOINTMENT_OUTCOME[p.outcome] ?? p.outcome}`;
    case "message.left":
      return "Message left for the front desk";
    case "tool.unknown":
      return `${humanize(p.tool)}: outcome unknown, front desk to check`;
    case "budget.exceeded":
      return "Call time limit reached";
    case "model.error":
      return "Voice model error";
    default:
      return null;
  }
}

export function Transcript({ events, highlight, emptyText }: { events: CallEvent[]; highlight?: Set<string>; emptyText: string }) {
  const box = useRef<HTMLDivElement>(null);
  const items = useMemo(
    () =>
      events
        .map((e) => {
          if (e.type === "transcript.segment") return { kind: "seg" as const, e };
          const text = timelineText(e);
          return text ? { kind: "line" as const, e, text } : null;
        })
        .filter((x) => x !== null),
    [events],
  );
  const lastSegment = [...events].reverse().find((e) => e.type === "transcript.segment");

  // Follow new lines inside the transcript only; never move the page itself.
  useEffect(() => {
    if (box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [items.length]);

  if (!items.length) return <div className="empty small">{emptyText}</div>;
  return (
    <>
      {/* A controllable summary instead of announcing every token (doc 05 accessibility). */}
      <div className="sr-only" aria-live="polite">
        {lastSegment ? `${lastSegment.payload.speaker === "agent" ? "Agent" : "Other party"}: ${lastSegment.payload.text}` : ""}
      </div>
      <div className="transcript" ref={box}>
        {items.map((item) =>
          item.kind === "line" ? (
            <div key={item.e.seq} className="timeline-item">
              {item.text}
            </div>
          ) : (
            <Bubble key={item.e.seq} e={item.e} highlighted={highlight?.has(String(item.e.payload.segment_id))} />
          ),
        )}
      </div>
    </>
  );
}

function Bubble({ e, highlighted }: { e: CallEvent; highlighted?: boolean }) {
  const p = e.payload as { speaker: string; text: string; playout: string; segment_id: string };
  const side = p.speaker === "agent" ? "agent" : "counterpart";
  return (
    <div className={`bubble-row ${side}`} id={`seg-${p.segment_id}`}>
      <div>
        <div className={`bubble${p.playout === "interrupted" ? " interrupted" : ""}${highlighted ? " highlight" : ""}`}>{p.text}</div>
        <div className="bubble-meta" style={{ textAlign: side === "agent" ? "left" : "right" }}>
          {side === "agent" ? "Agent" : "Other party"}
          {p.playout === "interrupted" && " · interrupted, not fully spoken"}
        </div>
      </div>
    </div>
  );
}

export function FindingsChecklist({ expected, findings, onEvidence }: { expected: string[]; findings: Finding[]; onEvidence?: (ids: string[]) => void }) {
  const latest = new Map<string, Finding>();
  for (const f of findings) if (f.source !== "system") latest.set(f.field, f);
  return (
    <div>
      {expected.map((field) => {
        const f = latest.get(field);
        return (
          <div className="check-item" key={field}>
            <span className="check-icon" aria-hidden>
              {!f ? <Circle size={18} color="var(--label-3)" /> : f.status === "conflicting" ? <ShieldAlert size={18} color="var(--red)" /> : f.value === null ? <CircleDashed size={18} color="var(--orange)" /> : <CheckCircle2 size={18} color="var(--green)" />}
            </span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 500 }}>{humanize(field)}</div>
              {f ? (
                <div className="stack" style={{ gap: 4, marginTop: 4 }}>
                  <div className="row" style={{ flexWrap: "wrap" }}>
                    {f.value !== null ? <span className="finding-value">{f.value}</span> : <span className="small muted">Unresolved — {f.null_reason}</span>}
                    <FindingPill status={f.status} />
                  </div>
                  {f.original_phrasing && <div className="small muted">“{f.original_phrasing}”</div>}
                  {onEvidence && f.evidence_segment_ids.length > 0 && (
                    <button className="btn btn-plain small" style={{ height: 22, padding: 0, justifySelf: "start" }} onClick={() => onEvidence(f.evidence_segment_ids)}>
                      Show evidence
                    </button>
                  )}
                </div>
              ) : (
                <div className="small muted">Not yet captured</div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function ToolActivity({ tools }: { tools: { tool: string; state: string; rejection_code: string | null }[] }) {
  if (!tools.length) return <div className="empty small">No tool activity yet.</div>;
  return (
    <ul className="list">
      {tools.map((t, i) => (
        <li className="list-row" key={i} style={{ minHeight: 40, padding: "8px 20px" }}>
          <Wrench size={15} color="var(--label-3)" aria-hidden />
          <span className="row-main small">{humanize(t.tool)}</span>
          {t.state === "Succeeded" ? <span className="pill tone-green">Allowed</span> : <span className="pill tone-red">Blocked · {t.rejection_code?.replace(/_/g, " ")}</span>}
        </li>
      ))}
    </ul>
  );
}
