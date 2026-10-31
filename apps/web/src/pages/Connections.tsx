import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { OctagonX, PhoneIncoming, Plus, RefreshCw } from "lucide-react";
import { useState } from "react";
import { api, ApiError, type AgentSummary, type Readiness, type Route } from "../api";
import { useCan, useMe } from "../App";
import { Banner, Card, CheckPill, Empty, Pill, Sheet, Toolbar, timeAgo, useToast } from "../ui";

const LABEL: Record<string, string> = {
  database: "Database",
  livekit_api: "Voice platform (LiveKit)",
  outbound_route: "Outbound phone route",
  inbound_route: "Inbound number",
  voice_model: "Voice model (Gemini Live on Vertex AI)",
  agent_worker: "Agent worker",
};

export default function Connections() {
  const me = useMe();
  const isAdmin = useCan("admin");
  const qc = useQueryClient();
  const toast = useToast();
  const readiness = useQuery({ queryKey: ["readiness"], queryFn: () => api.get<Readiness>("/readiness") });
  const routes = useQuery({ queryKey: ["routes"], queryFn: () => api.get<Route[]>("/routes") });
  const agents = useQuery({ queryKey: ["agents"], queryFn: () => api.get<AgentSummary[]>("/agents") });
  const [adding, setAdding] = useState(false);
  const stop = useMutation({
    mutationFn: (enabled: boolean) => api.post<{ calls_ended: number }>("/workspace/emergency-stop", { enabled }),
    onSuccess: (r, enabled) => {
      qc.invalidateQueries();
      toast(enabled ? `Calling stopped · ${r.calls_ended} call(s) ended` : "Calling resumed");
    },
  });

  return (
    <>
      <Toolbar
        eyebrow="Administration"
        title="Connections & Routes"
        actions={
          <button className="btn btn-secondary" onClick={() => readiness.refetch()} disabled={readiness.isFetching}>
            <RefreshCw size={15} className={readiness.isFetching ? "spin" : ""} aria-hidden /> Re-check
          </button>
        }
      />
      <div className="content">
        <Card title="Connections" subtitle={readiness.data ? `Checked ${timeAgo(readiness.data.checked_at)}` : "Checking…"}>
          <ul className="list">
            {readiness.data?.checks.map((c) => (
              <li className="list-row" key={c.name} style={{ alignItems: "flex-start", paddingTop: 14, paddingBottom: 14 }}>
                <div className="row-main">
                  <div className="row-title">{LABEL[c.name] ?? c.name}</div>
                  <div className="small muted" style={{ whiteSpace: "normal" }}>
                    {c.message}
                  </div>
                  {Object.keys(c.detail).length > 0 && (
                    <div className="row" style={{ flexWrap: "wrap", marginTop: 6 }}>
                      {Object.entries(c.detail).map(([k, v]) => (
                        <span className="chip mono" key={k}>
                          {k}: {Array.isArray(v) ? v.join(", ") : String(v)}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
                <CheckPill status={c.status} />
              </li>
            ))}
          </ul>
        </Card>

        <Card
          title="Numbers & routes"
          subtitle="Each inbound number routes to one agent. Capabilities stay unverified until a real call qualifies the route."
          actions={
            isAdmin && (
              <button className="btn btn-tinted" onClick={() => setAdding(true)}>
                <Plus size={15} aria-hidden /> Add inbound number
              </button>
            )
          }
        >
          {routes.data?.length === 0 ? (
            <Empty icon={PhoneIncoming} title="No inbound numbers">
              Buy a number in LiveKit, point its dispatch rule at the CarePlix agent, then add it here.
            </Empty>
          ) : (
            <ul className="list">
              {routes.data?.map((r) => (
                <li className="list-row" key={r.id}>
                  <div className="row-icon" style={{ background: "#34c759" }}>
                    <PhoneIncoming size={17} />
                  </div>
                  <div className="row-main">
                    <div className="row-title">
                      {r.name} · <span className="tabular">{r.number}</span>
                    </div>
                    <div className="row-sub">
                      {r.provider === "livekit_number" ? "LiveKit number · inbound only, no forwarding" : "Carrier number"} · answers with{" "}
                      {agents.data?.find((a) => a.id === r.agent_id)?.name ?? "—"}
                    </div>
                  </div>
                  <div className="row" style={{ flexWrap: "wrap", justifyContent: "flex-end" }}>
                    {Object.entries(r.capabilities).map(([cap, on]) => (
                      <Pill key={cap} tone={on ? "blue" : "gray"}>
                        {on ? "" : "No "}
                        {cap}
                      </Pill>
                    ))}
                    {!r.verified_at && <Pill tone="orange">Not qualified</Pill>}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>

        {isAdmin && (
          <Card title="Emergency stop" subtitle="Blocks new calls in this workspace and ends active ones. Every use is audited." pad>
            <div className="row">
              {me.emergency_stop ? (
                <>
                  <Banner tone="red" title="Calling is stopped for this workspace." icon={OctagonX} />
                  <span className="spacer" />
                  <button className="btn btn-secondary" onClick={() => stop.mutate(false)}>
                    Resume calling
                  </button>
                </>
              ) : (
                <button className="btn btn-danger" onClick={() => confirm("Stop all calling and end active calls in this workspace?") && stop.mutate(true)}>
                  <OctagonX size={16} aria-hidden /> Stop all calling
                </button>
              )}
            </div>
          </Card>
        )}
      </div>
      {adding && <AddRoute agents={(agents.data ?? []).filter((a) => a.template.direction === "inbound")} onClose={() => setAdding(false)} />}
    </>
  );
}

function AddRoute({ agents, onClose }: { agents: AgentSummary[]; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [name, setName] = useState("Authorization line");
  const [number, setNumber] = useState("");
  const [provider, setProvider] = useState("livekit_number");
  const [agentId, setAgentId] = useState(agents[0]?.id ?? "");
  const save = useMutation({
    mutationFn: () => api.post<Route>("/routes", { name, number, provider, agent_id: agentId }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["routes"] });
      toast("Inbound number added");
      onClose();
    },
  });
  const error = save.error instanceof ApiError ? save.error.message : null;
  return (
    <Sheet
      title="Add inbound number"
      subtitle="The called number decides which agent answers. Caller ID is never trusted for routing."
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" onClick={() => save.mutate()} disabled={!number || !agentId || save.isPending}>
            Add number
          </button>
        </>
      }
    >
      <label className="field">
        <span className="field-label">Name</span>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="field">
        <span className="field-label">Phone number</span>
        <input className="input tabular" placeholder="+1 202 555 0142" value={number} onChange={(e) => setNumber(e.target.value)} aria-invalid={!!error} />
        {error && <span className="field-error">{error}</span>}
      </label>
      <label className="field">
        <span className="field-label">Provider</span>
        <select className="select" value={provider} onChange={(e) => setProvider(e.target.value)}>
          <option value="livekit_number">LiveKit number (inbound only)</option>
          <option value="carrier">Carrier number</option>
        </select>
      </label>
      <label className="field">
        <span className="field-label">Answering agent</span>
        <select className="select" value={agentId} onChange={(e) => setAgentId(e.target.value)}>
          {agents.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name} {a.current ? `· v${a.current.number}` : "· not published"}
            </option>
          ))}
        </select>
        <span className="field-help">The agent must be published before the number can answer calls.</span>
      </label>
    </Sheet>
  );
}
