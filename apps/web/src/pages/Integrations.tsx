import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Building2, CircleDashed, Database, FileCheck2, Plus, RefreshCw, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { api, ApiError, type IntegrationsOverview, type PayerConnection } from "../api";
import { useCan } from "../App";
import { Banner, Card, Empty, Pill, Sheet, Switch, Toolbar, timeAgo, useToast } from "../ui";

const PHASE_LABEL: Record<string, string> = { verify: "Verification", pre_call: "Before call", post_call: "After call", manual: "Manual check" };

export default function Integrations() {
  const isAdmin = useCan("admin");
  const qc = useQueryClient();
  const toast = useToast();
  const data = useQuery({ queryKey: ["integrations"], queryFn: () => api.get<IntegrationsOverview>("/integrations"), refetchInterval: 5000 });
  const [adding, setAdding] = useState(false);
  const refresh = () => qc.invalidateQueries({ queryKey: ["integrations"] });
  const verify = useMutation({
    mutationFn: (id: string) => api.post<{ ok: boolean; error?: string; duration_ms?: number }>(`/integrations/payers/${id}/verify`),
    onSuccess: (r) => {
      toast(r.ok ? `Verified · test inquiry answered in ${r.duration_ms} ms` : `Verification failed`);
      refresh();
    },
  });
  const enable = useMutation({ mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => api.put(`/integrations/payers/${id}/enabled`, { enabled }), onSuccess: refresh });
  const d = data.data;
  const names = Object.fromEntries((d?.connections ?? []).map((c) => [c.id, c.name]));

  return (
    <>
      <Toolbar
        eyebrow="Settings"
        title="Integrations"
        description="Payer APIs and public systems of record. Before a status call CarePlix asks the payer's API; after it, it checks what was heard against the payer record."
        actions={
          <>
            <button className="btn btn-secondary" onClick={refresh}>
              <RefreshCw size={15} aria-hidden /> Refresh
            </button>
            {isAdmin && (
              <button className="btn btn-primary" onClick={() => setAdding(true)}>
                <Plus size={16} aria-hidden /> Add payer connection
              </button>
            )}
          </>
        }
      />
      <div className="content">
        <Banner tone="blue" title="API first, call when the API can't answer." icon={FileCheck2}>
          {" "}
          Before a status call, CarePlix asks the payer's Prior Authorization API (Da Vinci PAS <span className="mono">Claim/$inquire</span>). After the call, it checks what was heard against the payer record and flags any conflict for review.
        </Banner>

        <Card title="Connections" subtitle="Payer APIs and public systems of record. Each must pass a live test before it is trusted.">
          {d && d.connections.length === 0 ? (
            <Empty icon={Building2} title="No payer connections">
              Add a payer's prior-authorization (PAS) endpoint, or connect the CarePlix Payer Sandbox to try the flow with synthetic data.
            </Empty>
          ) : (
            <ul className="list">
              {d?.connections.map((c) => (
                <ConnectionRow key={c.id} c={c} isAdmin={isAdmin} verifying={verify.isPending && verify.variables === c.id} onVerify={() => verify.mutate(c.id)} onToggle={(enabled) => enable.mutate({ id: c.id, enabled })} />
              ))}
            </ul>
          )}
          {verify.error && <div className="card-pad field-error">{(verify.error as ApiError).message}</div>}
        </Card>

        <div className="split">
          <Card title="Recent payer calls" subtitle="Audit of every external call: outcome codes and timing only, never patient details.">
            {d && d.recent_calls.length === 0 ? (
              <Empty icon={Database} title="No calls yet">
                Verify a connection or create a task to see electronic checks here.
              </Empty>
            ) : (
              <ul className="list">
                {d?.recent_calls.map((r) => (
                  <li className="list-row" key={r.id} style={{ minHeight: 46 }}>
                    <div className="row-main">
                      <div className="row-title">
                        <span className="mono">{r.operation}</span> · {PHASE_LABEL[r.phase ?? ""] ?? r.phase}
                      </div>
                      <div className="row-sub">
                        {names[r.connection_id] ?? "Connection"} · {timeAgo(r.at)}
                        {r.summary?.reported_status ? ` · ${String(r.summary.reported_status).replace(/_/g, " ")}` : ""}
                        {r.summary?.review_action ? ` (${String(r.summary.review_action)})` : ""}
                        {r.error ? ` · ${r.error}` : ""}
                      </div>
                    </div>
                    <span className="small muted tabular">{r.duration_ms} ms</span>
                    <Pill tone={r.ok ? (r.status === "not_found" ? "orange" : "green") : "red"}>{r.status.replace(/_/g, " ")}</Pill>
                  </li>
                ))}
              </ul>
            )}
          </Card>
          <Card title="Standards coverage" subtitle="CMS-0057-F Prior Authorization API (payers, from 1 Jan 2027) and the Da Vinci guides.">
            <ul className="list">
              {d?.supported.map((s, i) => (
                <li className="list-row" key={i} style={{ alignItems: "flex-start" }}>
                  <div className="row-main">
                    <div className="row-title">{s.operations.join(" · ")}</div>
                    <div className="row-sub">
                      {s.kind.toUpperCase()} · {s.ig_version}
                    </div>
                  </div>
                  {s.status === "available" ? (
                    <Pill tone="green" icon={ShieldCheck}>
                      Available
                    </Pill>
                  ) : (
                    <Pill tone="gray" icon={CircleDashed}>
                      {s.status}
                    </Pill>
                  )}
                </li>
              ))}
            </ul>
          </Card>
        </div>
      </div>
      {adding && <AddConnection onClose={() => setAdding(false)} onDone={refresh} />}
    </>
  );
}

function ConnectionRow({ c, isAdmin, verifying, onVerify, onToggle }: { c: PayerConnection; isAdmin: boolean; verifying: boolean; onVerify: () => void; onToggle: (v: boolean) => void }) {
  return (
    <li className="list-row" style={{ alignItems: "flex-start", paddingTop: 16, paddingBottom: 16 }}>
      <div className="row-icon" style={{ background: c.synthetic ? "var(--tile-purple)" : "var(--tile-blue)" }}>
        <Building2 size={17} />
      </div>
      <div className="row-main">
        <div className="row" style={{ flexWrap: "wrap" }}>
          <span style={{ fontWeight: 600 }}>{c.name}</span>
          {c.synthetic && <span className="sample-label">Synthetic</span>}
          {c.verified_at ? (
            <Pill tone="green" icon={ShieldCheck}>
              Verified {timeAgo(c.verified_at)}
            </Pill>
          ) : (
            <Pill tone="orange" icon={CircleDashed}>
              Not verified
            </Pill>
          )}
        </div>
        <div className="small muted mono" style={{ marginTop: 2 }}>
          {c.base_url}
        </div>
        <div className="row" style={{ flexWrap: "wrap", marginTop: 6 }}>
          <span className="chip">{c.kind === "pas" ? c.ig_version : c.kind === "nexhealth" ? "EHR via NexHealth" : c.kind === "stedi" ? "Clearinghouse (X12)" : "Public reference API"}</span>
          <span className="chip">{c.auth_type === "none" ? (c.kind === "pas" ? "No auth (sandbox)" : "No auth (public)") : c.auth_type === "google_id_token" ? "Google identity (private)" : c.auth_type === "api_key" ? "API key (server secret)" : "SMART Backend Services"}</span>
          <span className="chip">Payers: {c.payer_names.includes("*") ? "all" : c.payer_names.join(", ")}</span>
          {c.capability?.operations?.map((op) => (
            <span className="chip mono" key={op}>
              {c.kind === "pas" ? `Claim/$${op}` : op}
            </span>
          ))}
        </div>
        {c.last_error && <div className="field-error" style={{ marginTop: 6 }}>{c.last_error}</div>}
      </div>
      {isAdmin && (
        <div className="row">
          <Switch label={`Enable ${c.name}`} checked={c.enabled} onChange={onToggle} />
          <button className="btn btn-tinted" onClick={onVerify} disabled={verifying}>
            {verifying ? <span className="spinner" aria-label="Verifying" /> : <ShieldCheck size={15} aria-hidden />} Verify
          </button>
        </div>
      )}
    </li>
  );
}

function AddConnection({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [f, setF] = useState({ name: "", base_url: "", auth_type: "smart_backend", client_id: "", token_url: "", key_env: "", kid: "", payers: "" });
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF((x) => ({ ...x, [k]: e.target.value }));
  const save = useMutation({
    mutationFn: () =>
      api.post("/integrations/payers", {
        name: f.name,
        base_url: f.base_url,
        auth_type: f.auth_type,
        client_id: f.client_id,
        token_url: f.token_url,
        key_env: f.key_env,
        kid: f.kid,
        payer_names: f.payers.split(",").map((x) => x.trim()).filter(Boolean),
      }),
    onSuccess: () => {
      toast("Connection added. Verify it before use.");
      onDone();
      onClose();
    },
  });
  const smart = f.auth_type === "smart_backend";
  return (
    <Sheet
      title="Add payer connection"
      subtitle="A payer's (or intermediary's) Da Vinci PAS endpoint. Credentials are referenced by environment variable name; CarePlix never stores private keys."
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" onClick={() => save.mutate()} disabled={!f.name || !f.base_url || save.isPending}>
            Add connection
          </button>
        </>
      }
    >
      <label className="field">
        <span className="field-label">Name</span>
        <input className="input" value={f.name} onChange={set("name")} placeholder="Northwind Health Plan — PAS" autoFocus />
      </label>
      <label className="field">
        <span className="field-label">FHIR base URL</span>
        <input className="input mono" value={f.base_url} onChange={set("base_url")} placeholder="https://payer.example.com/fhir" />
      </label>
      <label className="field">
        <span className="field-label">Authentication</span>
        <select className="select" value={f.auth_type} onChange={set("auth_type")}>
          <option value="smart_backend">SMART Backend Services (production)</option>
          <option value="none">None (sandboxes only)</option>
        </select>
      </label>
      {smart && (
        <div className="grid-2" style={{ gap: 14 }}>
          <label className="field">
            <span className="field-label">Client ID</span>
            <input className="input mono" value={f.client_id} onChange={set("client_id")} />
          </label>
          <label className="field">
            <span className="field-label">Token URL</span>
            <input className="input mono" value={f.token_url} onChange={set("token_url")} />
          </label>
          <label className="field">
            <span className="field-label">Private key reference</span>
            <input className="input mono" value={f.key_env} onChange={set("key_env")} placeholder="PAYER_NORTHWIND_KEY" />
            <span className="field-help">The name the key is stored under. The key itself stays in the server's secret store.</span>
          </label>
          <label className="field">
            <span className="field-label">Key ID (kid)</span>
            <input className="input mono" value={f.kid} onChange={set("kid")} />
          </label>
        </div>
      )}
      <label className="field">
        <span className="field-label">Payers served</span>
        <input className="input" value={f.payers} onChange={set("payers")} placeholder="Northwind Health Plan (synthetic), …" />
        <span className="field-help">Comma-separated payer names as they appear on cases. Leave empty to serve all payers.</span>
      </label>
      {save.error && <Banner tone="red" title={(save.error as ApiError).message} />}
    </Sheet>
  );
}
