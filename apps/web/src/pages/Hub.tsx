import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleCheck, CircleHelp, ExternalLink, FlaskConical, Minus, Network, UserCheck } from "lucide-react";
import { useState } from "react";
import { api, ApiError, type HubCatalog, type HubConnection, type HubProfile, type HubSupport } from "../api";
import { useCan } from "../App";
import { Banner, Card, Pill, Sheet, Toolbar, useToast } from "../ui";

const SHORT: Record<string, string> = {
  "schedule.read": "Sync",
  "eligibility.check": "Eligibility",
  "appointment.book": "Book",
  "appointment.cancel": "Cancel",
  "appointment.confirm": "Confirm",
  "appointment.check_in": "Check-in",
  "appointment.reschedule": "Reschedule",
  "chart.alert": "Chart alert",
  "document.upload": "Documents",
  "payment.post": "Payments",
};

const CATEGORY: Record<HubProfile["category"], string> = { medical: "Medical", dental: "Dental", ortho: "Orthodontic", general: "General" };

const CONFIRM: Record<HubProfile["confirm"], string> = {
  immediate: "Written to the EHR on the API call",
  async: "Accepted first, confirmed when the EHR syncs (minutes)",
  async_office_hours: "Accepted first, confirmed when the office is signed in (office hours)",
};

function Cell({ s }: { s: HubSupport }) {
  if (s === "yes") return <CircleCheck size={16} color="#34c759" aria-label="Supported" />;
  if (s === "existing_patients") return <UserCheck size={16} color="#34c759" aria-label="Existing patients only" />;
  if (s === "verify") return <CircleHelp size={16} color="#ff9500" aria-label="Verify per practice" />;
  return <Minus size={16} color="#8e8e93" aria-label="Not supported" />;
}

function status(p: HubProfile, conns: HubConnection[]): { tone: "green" | "purple" | "blue" | "gray"; label: string } {
  if (conns.some((c) => c.emulating === p.id)) return { tone: "purple", label: "Emulated" };
  const own = conns.filter((c) => !c.emulating && c.profile === p.id);
  if (own.some((c) => c.env === "production")) return { tone: "green", label: "Live" };
  if (own.some((c) => c.kind === "openemr")) return { tone: "green", label: "Live (test clinic)" };
  if (own.length) return { tone: "blue", label: "Sandbox" };
  return { tone: "gray", label: p.path === "nexhealth" ? "Needs a practice" : "Not connected" };
}

export default function Hub() {
  const isAdmin = useCan("admin");
  const qc = useQueryClient();
  const toast = useToast();
  const [open, setOpen] = useState<HubProfile | null>(null);
  const catalog = useQuery({ queryKey: ["hub-catalog"], queryFn: () => api.get<HubCatalog>("/practices/catalog") });
  const emulate = useMutation({
    mutationFn: ({ profile, practice }: { profile: string | null; practice: string }) =>
      api.put<HubConnection>(`/practices/emulation?practice=${encodeURIComponent(practice)}`, { profile }),
    onSuccess: (r) => {
      toast(r.emulating ? `OpenEMR now behaves as ${r.profile_name}` : "OpenEMR is back to itself");
      qc.invalidateQueries({ queryKey: ["hub-catalog"] });
      qc.invalidateQueries({ queryKey: ["practice"] });
      setOpen(null);
    },
  });
  const d = catalog.data;
  const openemr = d?.connections.find((c) => c.kind === "openemr");
  const ops = d?.operations ?? [];
  const groups: HubProfile["category"][] = ["general", "medical", "dental", "ortho"];

  return (
    <>
      <Toolbar eyebrow="CarePlix Connect" title="EHR hub" />
      <div className="content">
        <Banner tone="blue" title="One integration layer for every CarePlix product." icon={Network}>
          {" "}
          Each EHR connects once, through OpenEMR directly or NexHealth's 15 systems. Every product reads and writes through the same connectors,
          and the hub only sends the writes each EHR supports; anything else becomes a staff task.
        </Banner>
        {catalog.error && <Banner tone="red" title={(catalog.error as ApiError).message} />}

        {d && (
          <Card title="Connected practices" subtitle="How each practice connection maps onto the catalog.">
            <ul className="list">
              {d.connections.length === 0 && <li className="list-row muted">No practice EHR is connected yet.</li>}
              {d.connections.map((c) => (
                <li className="list-row" key={c.connection_id} style={{ flexWrap: "wrap", gap: 8 }}>
                  <div className="row-main">
                    <div className="row-title">{c.name}</div>
                    <div className="row-sub">
                      {c.kind === "openemr" ? "Direct (OpenEMR)" : "NexHealth"}
                      {c.env ? ` · ${c.env}` : ""} · behaves as {c.profile_name ?? "unknown EHR (no sync yet)"}
                    </div>
                  </div>
                  {c.emulating && (
                    <Pill tone="purple" icon={FlaskConical}>
                      Emulating {c.profile_name}
                    </Pill>
                  )}
                  {c.links?.home && (
                    <a className="btn btn-secondary" href={c.links.home} target="_blank" rel="noreferrer">
                      <ExternalLink size={15} aria-hidden /> Open EHR
                    </a>
                  )}
                </li>
              ))}
            </ul>
            {openemr && isAdmin && (
              <div className="card-pad row" style={{ gap: 8, flexWrap: "wrap" }}>
                <label className="small" htmlFor="emulate">
                  Run the OpenEMR test clinic as
                </label>
                <select
                  id="emulate"
                  className="input"
                  style={{ maxWidth: 280 }}
                  value={openemr.emulating ?? ""}
                  disabled={emulate.isPending}
                  onChange={(e) => emulate.mutate({ profile: e.target.value || null, practice: openemr.connection_id })}
                >
                  <option value="">OpenEMR itself</option>
                  {groups
                    .filter((g) => g !== "general")
                    .map((g) => (
                      <optgroup key={g} label={CATEGORY[g]}>
                        {d.profiles
                          .filter((p) => p.category === g)
                          .map((p) => (
                            <option key={p.id} value={p.id}>
                              {p.name} (via NexHealth)
                            </option>
                          ))}
                      </optgroup>
                    ))}
                </select>
                <span className="small muted">Writes still land in OpenEMR; the hub applies that EHR's rules and confirmation timing.</span>
              </div>
            )}
            {emulate.error && <div className="card-pad field-error">{(emulate.error as ApiError).message}</div>}
          </Card>
        )}

        {d && (
          <Card title={`Supported EHRs (${d.profiles.length})`} subtitle="What CarePlix can automate on each EHR. Select a row for details.">
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13, minWidth: 860 }}>
                <thead>
                  <tr style={{ textAlign: "left" }}>
                    <th style={{ padding: "8px 12px" }}>EHR</th>
                    <th style={{ padding: "8px 6px" }}>Status</th>
                    {ops.map((o) => (
                      <th key={o.id} title={o.label} style={{ padding: "8px 6px", textAlign: "center", fontWeight: 500 }}>
                        {SHORT[o.id] ?? o.label}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {groups.map((g) => {
                    const rows = d.profiles.filter((p) => p.category === g);
                    if (!rows.length) return null;
                    return [
                      <tr key={`h-${g}`}>
                        <td colSpan={ops.length + 2} className="small muted" style={{ padding: "12px 12px 4px", fontWeight: 600 }}>
                          {CATEGORY[g]}
                        </td>
                      </tr>,
                      ...rows.map((p) => {
                        const st = status(p, d.connections);
                        return (
                          <tr key={p.id} onClick={() => setOpen(p)} style={{ cursor: "pointer", borderTop: "1px solid var(--separator, rgba(0,0,0,0.08))" }}>
                            <td style={{ padding: "8px 12px" }}>
                              <div style={{ fontWeight: 500 }}>{p.name}</div>
                              <div className="small muted">
                                {p.path === "direct" ? "Direct" : "via NexHealth"} · {p.deployment}
                              </div>
                            </td>
                            <td style={{ padding: "8px 6px" }}>
                              <Pill tone={st.tone}>{st.label}</Pill>
                            </td>
                            {ops.map((o) => (
                              <td key={o.id} style={{ padding: "8px 6px", textAlign: "center" }}>
                                <Cell s={p.ops[o.id]} />
                              </td>
                            ))}
                          </tr>
                        );
                      }),
                    ];
                  })}
                </tbody>
              </table>
            </div>
            <div className="card-pad small muted row" style={{ gap: 14, flexWrap: "wrap" }}>
              <span className="row" style={{ gap: 4 }}>
                <CircleCheck size={14} color="#34c759" /> Supported
              </span>
              <span className="row" style={{ gap: 4 }}>
                <UserCheck size={14} color="#34c759" /> Existing patients only
              </span>
              <span className="row" style={{ gap: 4 }}>
                <CircleHelp size={14} color="#ff9500" /> Verify per practice
              </span>
              <span className="row" style={{ gap: 4 }}>
                <Minus size={14} color="#8e8e93" /> Not supported: becomes a staff task
              </span>
            </div>
          </Card>
        )}
      </div>

      {open && d && (
        <Sheet
          title={open.name}
          subtitle={`${CATEGORY[open.category]} · ${open.path === "direct" ? "Direct connector" : "via NexHealth"} · ${open.deployment}`}
          onClose={() => setOpen(null)}
          footer={
            openemr && isAdmin && open.path === "nexhealth" ? (
              <button
                className="btn btn-primary"
                disabled={emulate.isPending || openemr.emulating === open.id}
                onClick={() => emulate.mutate({ profile: open.id, practice: openemr.connection_id })}
              >
                <FlaskConical size={15} aria-hidden /> {openemr.emulating === open.id ? "Emulating now" : "Emulate on the OpenEMR clinic"}
              </button>
            ) : undefined
          }
        >
          <div className="card-pad" style={{ display: "grid", gap: 12 }}>
            <div>
              <div className="small muted">How a practice connects</div>
              <div>{open.connects_by}</div>
            </div>
            <div>
              <div className="small muted">How writes are confirmed</div>
              <div>{CONFIRM[open.confirm]}</div>
            </div>
            <ul className="list">
              {ops.map((o) => (
                <li className="list-row" key={o.id}>
                  <div className="row-main">{o.label}</div>
                  <Cell s={open.ops[o.id]} />
                </li>
              ))}
            </ul>
            {open.notes.length > 0 && (
              <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
                {open.notes.map((n) => (
                  <li key={n}>{n}</li>
                ))}
              </ul>
            )}
          </div>
        </Sheet>
      )}
    </>
  );
}
