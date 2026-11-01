import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarCheck2, CalendarPlus, CheckCircle2, CircleDashed, Clock, Eye, Gauge, PhoneCall, RefreshCw, Timer, XCircle } from "lucide-react";
import { useState } from "react";
import { api, ApiError, type BackfillBoard, type BackfillOfferRow, type BackfillOpeningRow } from "../api";
import { useCan } from "../App";
import { Banner, Card, Empty, Pill, Toolbar, timeAgo, useToast } from "../ui";

const TIER: Record<BackfillOfferRow["tier"], string> = { asap: "Wants sooner", move_up: "Move-up", recent_cancel: "Recent cancel" };

const STATE: Record<BackfillOpeningRow["state"], { tone: "green" | "orange" | "red" | "blue" | "gray"; label: string }> = {
  open: { tone: "blue", label: "Open" },
  booking: { tone: "blue", label: "Booking" },
  filled: { tone: "green", label: "Filled" },
  needs_review: { tone: "red", label: "Needs review" },
  taken: { tone: "gray", label: "Taken" },
  expired: { tone: "gray", label: "Expired" },
  closed: { tone: "gray", label: "Closed" },
  no_candidates: { tone: "orange", label: "No candidates" },
};

const EVENT: Record<string, string> = {
  detected: "Cancellation detected",
  slot_confirmed_free: "NexHealth confirmed the slot is free",
  slot_not_free: "NexHealth shows the slot is no longer free",
  ranked: "Candidates ranked",
  offer_declined: "Patient declined",
  offer_no_answer: "No answer",
  offer_accepted: "Patient accepted",
  slot_recheck_failed: "Slot re-check failed",
  booked: "Booked in NexHealth",
  booking_refused: "Booking refused",
  booking_unknown: "Booking outcome unknown",
  released_later_appointment: "Later appointment released",
  expired: "Expired",
  closed: "Closed",
};

function when(iso: string | null, tz: string | null, withDay = true) {
  if (!iso) return "";
  return new Intl.DateTimeFormat(undefined, { timeZone: tz ?? undefined, weekday: withDay ? "short" : undefined, month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(new Date(iso));
}

export default function Backfill() {
  const isAdmin = useCan("admin");
  const qc = useQueryClient();
  const toast = useToast();
  const board = useQuery({ queryKey: ["backfill"], queryFn: () => api.get<BackfillBoard>("/practices/backfill"), refetchInterval: 5000 });
  const refresh = () => qc.invalidateQueries({ queryKey: ["backfill"] });
  const scan = useMutation({
    mutationFn: () => api.post<{ opened: number; expired: number }>("/practices/backfill/scan"),
    onSuccess: (r) => {
      toast(`${r.opened} new opening${r.opened === 1 ? "" : "s"} · ${r.expired} closed or expired`);
      refresh();
    },
  });
  const d = board.data;
  const m = d?.metrics;
  const open = m ? (m.states.open ?? 0) + (m.states.no_candidates ?? 0) : undefined;

  return (
    <>
      <Toolbar
        eyebrow="Front office"
        title="Cancellation backfill"
        actions={
          <>
            <button className="btn btn-secondary" onClick={refresh}>
              <RefreshCw size={15} aria-hidden /> Refresh
            </button>
            {isAdmin && (
              <button className="btn btn-primary" onClick={() => scan.mutate()} disabled={scan.isPending}>
                {scan.isPending ? <span className="spinner" aria-label="Scanning" /> : <CalendarPlus size={15} aria-hidden />} Scan now
              </button>
            )}
          </>
        }
      />
      <div className="content">
        <Banner tone="blue" title="Front desk mode." icon={PhoneCall}>
          {" "}
          Outbound calls and texts aren't connected yet, so call each patient in order and record their answer here. "Accepted" books the slot in NexHealth
          after re-checking that it's still free. Move-ups are booked first, then their later visit is cancelled. The voice agent takes over this list once a
          carrier is connected.
        </Banner>
        {board.error && <Banner tone="red" title={(board.error as ApiError).message} />}

        <div className="grid-3" style={{ gridTemplateColumns: "repeat(4, minmax(0, 1fr))" }}>
          <Stat icon={CalendarPlus} tint="#0071e3" value={open} label="Open slots" />
          <Stat icon={CalendarCheck2} tint="#34c759" value={m?.states.filled ?? (m ? 0 : undefined)} label="Filled" />
          <Stat icon={Gauge} tint="#af52de" value={m ? (m.fill_rate === null ? "–" : `${Math.round(m.fill_rate * 100)}%`) : undefined} label="Fill rate" />
          <Stat icon={Timer} tint="#ff9500" value={m?.visit_minutes_recovered} label="Visit minutes recovered" />
        </div>

        {d && d.active.length === 0 ? (
          <Empty icon={CalendarCheck2} title="No open slots">
            Cancellations from {Math.round(d.settings.lead_minutes / 60)} hours to {d.settings.horizon_days} days ahead appear here once NexHealth confirms the time is free.
          </Empty>
        ) : (
          d?.active.map((o) => <OpeningCard key={o.id} o={o} onDone={refresh} />)
        )}

        {d && d.recent.length > 0 && (
          <Card title="Recently resolved" subtitle="Filled, expired, taken or closed openings.">
            <ul className="list">
              {d.recent.map((o) => (
                <li className="list-row" key={o.id} style={{ alignItems: "flex-start", paddingTop: 12, paddingBottom: 12 }}>
                  <div className="row-main">
                    <div className="row-title">
                      {when(o.start, o.timezone)} · {o.type ?? "Visit"} · {o.provider ?? "Provider"}
                    </div>
                    <div className="row-sub">
                      {o.state === "filled" ? `Filled ${timeAgo(o.filled_at)} · NexHealth appointment ${o.filled_appointment_id}` : (o.state_reason ?? "")}
                    </div>
                  </div>
                  <Pill tone={STATE[o.state].tone}>{STATE[o.state].label}</Pill>
                </li>
              ))}
            </ul>
          </Card>
        )}
      </div>
    </>
  );
}

function Stat({ icon: Icon, tint, value, label }: { icon: typeof Clock; tint: string; value: number | string | undefined; label: string }) {
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

function OpeningCard({ o, onDone }: { o: BackfillOpeningRow; onDone: () => void }) {
  const canRun = useCan("start_task");
  const toast = useToast();
  const [showTimeline, setShowTimeline] = useState(false);
  const respond = useMutation({
    mutationFn: ({ id, outcome }: { id: string; outcome: "accepted" | "declined" | "no_answer" }) =>
      api.post<BackfillOpeningRow>(`/practices/backfill/offers/${id}`, { outcome }),
    onSuccess: (r) => {
      toast(r.state === "filled" ? "Booked in NexHealth" : r.state === "taken" ? "That time was just taken" : STATE[r.state].label);
      onDone();
    },
  });
  return (
    <Card
      title={`${when(o.start, o.timezone)} · ${o.minutes} min`}
      subtitle={`${o.type ?? "Visit"} with ${o.provider ?? "provider"} · cancelled appointment ${o.source_appointment_id} · found ${timeAgo(o.detected_at)}`}
      actions={
        <div className="row" style={{ gap: 6 }}>
          {!o.within_contact_hours && o.state === "open" && (
            <Pill tone="orange" icon={Clock}>
              Outside contact hours
            </Pill>
          )}
          <Pill tone={STATE[o.state].tone}>{STATE[o.state].label}</Pill>
        </div>
      }
    >
      {o.state_reason && <div className="card-pad small muted">{o.state_reason}</div>}
      {o.offers.length === 0 ? (
        <div className="card-pad small muted">No reachable patient is waiting for this visit type. It stays open in case one appears.</div>
      ) : (
        <ul className="list">
          {o.offers.map((offer) => (
            <OfferRow
              key={offer.id}
              offer={offer}
              tz={o.timezone}
              isNext={offer.id === o.next_offer_id}
              canRun={canRun && o.state === "open"}
              busy={respond.isPending}
              onRespond={(outcome) => respond.mutate({ id: offer.id, outcome })}
            />
          ))}
        </ul>
      )}
      {respond.error && <div className="card-pad field-error">{(respond.error as ApiError).message}</div>}
      <div className="card-pad" style={{ paddingTop: 0 }}>
        <button className="btn btn-plain" onClick={() => setShowTimeline((v) => !v)}>
          {showTimeline ? "Hide" : "Show"} timeline ({o.timeline.length})
        </button>
        {showTimeline && (
          <ol className="small muted" style={{ margin: "6px 0 0", paddingLeft: 18 }}>
            {o.timeline.map((e, i) => (
              <li key={i}>
                {when(e.at, o.timezone, false)} · {EVENT[e.event] ?? e.event}
              </li>
            ))}
          </ol>
        )}
      </div>
    </Card>
  );
}

function OfferRow({ offer, tz, isNext, canRun, busy, onRespond }: { offer: BackfillOfferRow; tz: string | null; isNext: boolean; canRun: boolean; busy: boolean; onRespond: (o: "accepted" | "declined" | "no_answer") => void }) {
  const [contact, setContact] = useState<{ name: string | null; phone: string | null } | null>(null);
  const reveal = useMutation({
    mutationFn: () => api.get<{ name: string | null; phone: string | null }>(`/practices/patients/${offer.patient_id}/contact`),
    onSuccess: setContact,
  });
  const queued = offer.state === "queued";
  return (
    <li className="list-row" style={{ alignItems: "flex-start", paddingTop: 14, paddingBottom: 14, background: isNext ? "var(--fill-blue, rgba(0,113,227,0.06))" : undefined }}>
      <div className="tabular" style={{ width: 28, fontWeight: 600, paddingTop: 1 }}>
        {offer.rank}
      </div>
      <div className="row-main">
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          <span className="row-title">{contact?.name ?? offer.patient}</span>
          <span className="chip">{TIER[offer.tier]}</span>
          {isNext && <Pill tone="blue">Call next</Pill>}
        </div>
        <div className="row-sub">
          {offer.reasons.join(" · ")}
          {offer.current_start ? ` · now booked ${when(offer.current_start, tz)}` : ""}
        </div>
        {contact && <div className="small mono" style={{ marginTop: 4 }}>{contact.phone ?? "No phone on file"}</div>}
      </div>
      {queued && canRun ? (
        <div className="row" style={{ gap: 6, flexWrap: "wrap", justifyContent: "flex-end" }}>
          {!contact && (
            <button className="btn btn-plain btn-icon" onClick={() => reveal.mutate()} disabled={reveal.isPending} aria-label="Show name and phone" title="Show name and phone (audited)">
              <Eye size={16} />
            </button>
          )}
          <button className="btn btn-primary" onClick={() => onRespond("accepted")} disabled={busy}>
            <CheckCircle2 size={15} aria-hidden /> Accepted
          </button>
          <button className="btn btn-secondary" onClick={() => onRespond("declined")} disabled={busy}>
            <XCircle size={15} aria-hidden /> Declined
          </button>
          <button className="btn btn-secondary" onClick={() => onRespond("no_answer")} disabled={busy}>
            <CircleDashed size={15} aria-hidden /> No answer
          </button>
        </div>
      ) : (
        <Pill tone={offer.state === "booked" ? "green" : offer.state === "queued" ? "gray" : offer.state === "failed" ? "red" : "gray"}>{offer.state.replace("_", " ")}</Pill>
      )}
    </li>
  );
}
