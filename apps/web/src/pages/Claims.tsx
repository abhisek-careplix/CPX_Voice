import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, Banknote, CircleDashed, ClipboardList, FileSearch, FlaskConical, Hourglass, PhoneCall, Play, RefreshCw, Send } from "lucide-react";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api";
import { useCan } from "../App";
import { Banner, Card, Empty, ErrorState, ListSkeleton, Pill, Sheet, Toolbar, timeAgo, useToast } from "../ui";

// ---------- Types (mirror apps/api claims views) ----------

type ClaimStatus = "paid" | "denied" | "no_payment" | "finalized" | "pending" | "accepted" | "info_requested" | "rejected" | "not_found" | "error" | "unavailable";

interface ClaimRow {
  id: string;
  claim_number: string;
  payer_claim_number: string | null;
  source: "sample" | "ehr";
  synthetic: boolean;
  sample_scenario: string | null;
  payer_id: string | null;
  payer_name: string;
  plan_kind: string | null;
  provider_name: string;
  date_of_service: string;
  billed_amount: string;
  cpt_code: string | null;
  submitted_on: string;
  age_days: number;
  status: ClaimStatus | null;
  status_label: string;
  status_since: string | null;
  status_mock: boolean;
  final: boolean;
  amount_paid: string | null;
  no_route: string | null;
  last_checked_at: string | null;
  next_check_at: string | null;
  failures: number;
  follow_up_case_id: string | null;
  follow_up_reason: string | null;
  follow_up_error: string | null;
}

interface StatusCheck {
  id: string;
  trigger: "schedule" | "manual";
  source: "clearinghouse" | "sample" | "none";
  mode: string;
  mock: boolean;
  state: "done" | "failed" | "unavailable";
  status: ClaimStatus;
  label: string;
  summary: { category_code?: string | null; category_text?: string | null; status_code?: string | null; status_text?: string | null; effective_date?: string | null; amount_paid?: string | null; payer_claim_number?: string | null };
  message: string | null;
  duration_ms: number;
  has_response: boolean;
  created_at: string;
}

interface ClaimDetail extends ClaimRow {
  due: boolean;
  needs_call: string | null;
  checks: StatusCheck[];
}

interface DirectoryPayer {
  id: string;
  payer_id: string;
  name: string;
  programs: string[];
  support: Record<string, string | null>;
  phone: string | null;
  source: "builtin" | "network";
  claim_status_route: string | null;
}

interface ClearinghouseOverview {
  name: string;
  connected: boolean;
  mode: "test" | "production" | null;
  transactions: { id: string; label: string; ready: boolean }[];
  payers: DirectoryPayer[];
}

type Tone = "green" | "orange" | "red" | "blue" | "purple" | "gray";

const TONE: Record<ClaimStatus, Tone> = {
  paid: "green",
  denied: "red",
  no_payment: "red",
  finalized: "gray",
  pending: "blue",
  accepted: "blue",
  info_requested: "orange",
  rejected: "red",
  not_found: "orange",
  error: "orange",
  unavailable: "gray",
};

function StatusPill({ status, label }: { status: ClaimStatus | null; label: string }) {
  return <Pill tone={status ? TONE[status] : "gray"} icon={status ? undefined : CircleDashed}>{label}</Pill>;
}

const money = (v: string | null | undefined) => (v ? `$${Number(v).toFixed(2)}` : "–");
const SOURCE_LABEL = { clearinghouse: "Clearinghouse", sample: "Synthetic sample", none: "Not asked" } as const;

export default function Claims() {
  const canStart = useCan("start_task");
  const qc = useQueryClient();
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const openId = params.get("claim");
  const claims = useQuery({ queryKey: ["claims"], queryFn: () => api.get<ClaimRow[]>("/claims") });
  const ch = useQuery({ queryKey: ["clearinghouse"], queryFn: () => api.get<ClearinghouseOverview>("/clearinghouse") });
  const refresh = () => qc.invalidateQueries({ queryKey: ["claims"] });
  const samples = useMutation({
    mutationFn: () => api.post<ClaimRow[]>("/claims/samples", {}),
    onSuccess: (r) => {
      toast(`${r.length} synthetic claims added`);
      refresh();
    },
    onError: (e) => toast((e as ApiError).message),
  });
  const run = useMutation({
    mutationFn: () => api.post<{ checked: number; cases: number; errors: number }>("/claims/follow-up"),
    onSuccess: (r) => {
      toast(`${r.checked} checked · ${r.cases} payer call${r.cases === 1 ? "" : "s"} opened${r.errors ? ` · ${r.errors} failed` : ""}`);
      refresh();
      qc.invalidateQueries({ queryKey: ["cases"] });
    },
    onError: (e) => toast((e as ApiError).message),
  });
  const rows = claims.data ?? [];
  const count = (pred: (c: ClaimRow) => boolean) => (claims.data ? rows.filter(pred).length : undefined);

  return (
    <>
      <Toolbar
        eyebrow="Back office"
        title="Claim follow-up"
        description="Submitted claims are checked electronically by age (first at 14 days, then weekly) until the payer finalizes them. When the electronic status is stale or unavailable, a claim status case is opened so the agent can call the payer."
        actions={
          canStart && (
            <>
              <button className="btn btn-secondary" onClick={() => samples.mutate()} disabled={samples.isPending}>
                <FlaskConical size={15} aria-hidden /> Add sample claims
              </button>
              <button className="btn btn-primary" onClick={() => run.mutate()} disabled={run.isPending}>
                {run.isPending ? <span className="spinner" aria-label="Running" /> : <Play size={15} aria-hidden />} Run follow-up now
              </button>
            </>
          )
        }
      />
      <div className="content">
        {ch.data && !ch.data.connected && (
          <Banner tone="orange" title="The clearinghouse isn't connected." icon={AlertCircle}>
            {" "}
            Sample claims still answer with synthetic payer responses; claims from the EHR wait until it's connected.
          </Banner>
        )}
        {ch.data?.mode === "test" && (
          <Banner tone="blue" title="Clearinghouse test mode.">
            {" "}
            Checks sent to the clearinghouse carry its published test claim, never your patient, and the answer is labelled Test. Sample claims answer with
            synthetic payer responses on the schedule.
          </Banner>
        )}

        <div className="grid-3" style={{ gridTemplateColumns: "repeat(4, minmax(0, 1fr))" }}>
          <Stat icon={ClipboardList} tint="var(--tile-blue)" value={count((c) => !c.final)} label="Open claims" />
          <Stat icon={Banknote} tint="var(--tile-green)" value={count((c) => c.status === "paid")} label="Paid" />
          <Stat icon={Hourglass} tint="var(--tile-purple)" value={count((c) => !c.final && c.age_days >= 30)} label="Over 30 days" />
          <Stat icon={PhoneCall} tint="var(--tile-orange)" value={count((c) => !!c.follow_up_case_id)} label="Payer calls opened" />
        </div>

        <Card title="Claims" subtitle="Oldest first. Select a claim for its status history and evidence.">
          {claims.isPending && <ListSkeleton rows={4} />}
          {claims.error && <ErrorState error={claims.error} onRetry={() => claims.refetch()} title="Couldn't load claims" />}
          {claims.data && rows.length === 0 && (
            <Empty icon={FileSearch} title="No claims yet">
              Claims arrive from the EHR's billing once that pull is connected. Until then, add synthetic sample claims to see the follow-up work.
            </Empty>
          )}
          {rows.length > 0 && (
            <ul className="list">
              {rows.map((c) => (
                <li key={c.id}>
                  <button className="list-row" style={{ width: "100%", textAlign: "left" }} onClick={() => setParams({ claim: c.id }, { replace: true })}>
                    <div className="row-main">
                      <div className="row-title">
                        <span className="mono">{c.claim_number}</span> · {c.payer_name}
                      </div>
                      <div className="row-sub">
                        Service {c.date_of_service} · {money(c.billed_amount)} · {c.age_days} days with the payer
                        {c.follow_up_case_id ? " · payer call opened" : ""}
                      </div>
                    </div>
                    {c.synthetic && <span className="sample-label">Synthetic</span>}
                    {c.status_mock && <span className="chip">Test</span>}
                    <StatusPill status={c.status} label={c.status_label} />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <ClearinghouseCard data={ch.data} error={ch.error} />
      </div>
      {openId && <ClaimSheet id={openId} canStart={canStart} connected={!!ch.data?.connected} onClose={() => setParams({}, { replace: true })} />}
    </>
  );
}

function ClaimSheet({ id, canStart, connected, onClose }: { id: string; canStart: boolean; connected: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const canReview = useCan("review");
  const detail = useQuery({ queryKey: ["claims", id], queryFn: () => api.get<ClaimDetail>(`/claims/${id}`) });
  const [evidence, setEvidence] = useState<{ id: string; text: string } | null>(null);
  const check = useMutation({
    mutationFn: (via: "auto" | "clearinghouse") => api.post<ClaimDetail>(`/claims/${id}/check?via=${via}`),
    onSuccess: (r) => {
      toast(`${r.claim_number}: ${r.status_label}`);
      qc.invalidateQueries({ queryKey: ["claims"] });
    },
    onError: (e) => toast((e as ApiError).message),
  });
  const show = async (checkId: string) => {
    try {
      const body = await api.get<unknown>(`/claims/${id}/checks/${checkId}/response`);
      setEvidence({ id: checkId, text: JSON.stringify(body, null, 2) });
    } catch (e) {
      toast((e as ApiError).message);
    }
  };
  const c = detail.data;
  return (
    <Sheet
      wide
      title={c ? `${c.claim_number} · ${c.payer_name}` : "Claim"}
      subtitle={c ? `${c.provider_name} · service ${c.date_of_service} · ${money(c.billed_amount)}${c.cpt_code ? ` · CPT ${c.cpt_code}` : ""}` : undefined}
      onClose={onClose}
      footer={
        c &&
        canStart && (
          <>
            {c.source === "sample" && connected && (
              <button className="btn btn-secondary" onClick={() => check.mutate("clearinghouse")} disabled={check.isPending}>
                <Send size={15} aria-hidden /> Ask the clearinghouse
              </button>
            )}
            <span className="spacer" />
            <button className="btn btn-primary" onClick={() => check.mutate("auto")} disabled={check.isPending}>
              {check.isPending ? <span className="spinner" aria-label="Checking" /> : <RefreshCw size={15} aria-hidden />} Check status now
            </button>
          </>
        )
      }
    >
      {detail.isPending && <ListSkeleton rows={4} icon={false} />}
      {detail.error && <ErrorState error={detail.error} onRetry={() => detail.refetch()} title="Couldn't load this claim" />}
      {c && (
        <div className="stack" style={{ gap: 22 }}>
          <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
            <StatusPill status={c.status} label={c.status_label} />
            {c.status_mock && <span className="chip">Test-mode answer (the clearinghouse's test claim, not this one)</span>}
            {c.synthetic && <span className="sample-label">Synthetic</span>}
            <span className="small muted">
              {c.age_days} days with the payer{c.status_since ? ` · in this status since ${c.status_since}` : ""}
            </span>
          </div>

          {c.no_route && (
            <Banner tone="gray" title="No electronic status." icon={AlertCircle}>
              {" "}
              {c.no_route}
            </Banner>
          )}
          {c.follow_up_case_id ? (
            <Banner tone="orange" title="A payer call is open for this claim." icon={PhoneCall}>
              {" "}
              {c.follow_up_reason} <Link to={`/cases?case=${c.follow_up_case_id}`}>Open the case</Link>
            </Banner>
          ) : c.needs_call ? (
            <Banner tone="orange" title="This claim needs a payer call." icon={PhoneCall}>
              {" "}
              {c.needs_call} {c.follow_up_error ?? "The next follow-up run opens the case."}
            </Banner>
          ) : null}

          <section className="stack" style={{ gap: 6 }}>
            <h3 className="field-label">Schedule</h3>
            <p style={{ margin: 0 }}>
              {c.final
                ? "Finalized by the payer: no more checks."
                : c.no_route
                  ? "Not checked electronically; followed by phone once the claim is 30 days old."
                  : c.next_check_at
                    ? `Next check ${new Date(c.next_check_at).toLocaleString()}.`
                    : c.age_days < 14
                      ? `First check when the claim is 14 days old (in ${14 - c.age_days} days).`
                      : "Due now: the next follow-up run checks it."}
              {c.amount_paid && Number(c.amount_paid) > 0 ? ` Paid ${money(c.amount_paid)}.` : ""}
              {c.payer_claim_number ? ` Payer claim number ${c.payer_claim_number}.` : ""}
            </p>
          </section>

          <section className="stack" style={{ gap: 6 }}>
            <h3 className="field-label">Status checks (evidence)</h3>
            {c.checks.length === 0 ? (
              <p className="small muted" style={{ margin: 0 }}>
                Not checked yet.
              </p>
            ) : (
              <ul className="list">
                {c.checks.map((k) => (
                  <li className="list-row" key={k.id} style={{ alignItems: "flex-start" }}>
                    <div className="row-main">
                      <div className="row-title">
                        {k.label}
                        {k.summary.category_code ? (
                          <span className="mono small muted">
                            {" "}
                            · {k.summary.category_code}/{k.summary.status_code}
                          </span>
                        ) : null}
                      </div>
                      <div className="row-sub">
                        {SOURCE_LABEL[k.source]}
                        {k.mock ? " · test mode" : ""} · {k.trigger === "schedule" ? "scheduled" : "by hand"} · {timeAgo(k.created_at)}
                        {k.summary.status_text ? ` · ${k.summary.status_text}` : ""}
                        {k.message ? ` · ${k.message}` : ""}
                      </div>
                    </div>
                    {k.has_response && canReview && (
                      <button className="btn btn-secondary btn-sm" onClick={() => (evidence?.id === k.id ? setEvidence(null) : show(k.id))}>
                        {evidence?.id === k.id ? "Hide 277" : "View 277"}
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {evidence && (
              <pre className="mono small" style={{ maxHeight: 320, overflow: "auto", background: "var(--fill-2, rgba(127,127,127,.08))", padding: 12, borderRadius: 8, margin: 0 }}>
                {evidence.text}
              </pre>
            )}
          </section>
        </div>
      )}
    </Sheet>
  );
}

const SUPPORT_LABEL: Record<string, string> = { SUPPORTED: "Yes", NOT_SUPPORTED: "No", ENROLLMENT_REQUIRED: "Enrollment needed" };

function ClearinghouseCard({ data, error }: { data?: ClearinghouseOverview; error: unknown }) {
  const isAdmin = useCan("admin");
  const qc = useQueryClient();
  const toast = useToast();
  const [query, setQuery] = useState("");
  const refresh = useMutation({
    mutationFn: () => api.post<DirectoryPayer[]>("/clearinghouse/payers/refresh", { query }),
    onSuccess: (r) => {
      toast(`${r.length} payer${r.length === 1 ? "" : "s"} updated from the network`);
      qc.invalidateQueries({ queryKey: ["clearinghouse"] });
    },
    onError: (e) => toast((e as ApiError).message),
  });
  return (
    <Card
      title="Clearinghouse"
      subtitle={
        data
          ? `${data.connected ? (data.mode === "test" ? "Connected in test mode" : "Connected") : "Not connected"} · ${data.transactions
              .filter((t) => t.ready)
              .map((t) => t.label)
              .join(" · ")}`
          : undefined
      }
      actions={
        isAdmin &&
        data?.connected && (
          <div className="row" style={{ gap: 8 }}>
            <input className="input" style={{ maxWidth: 200 }} placeholder="Find a payer on the network" value={query} onChange={(e) => setQuery(e.target.value)} />
            <button className="btn btn-secondary" onClick={() => refresh.mutate()} disabled={query.trim().length < 2 || refresh.isPending}>
              <RefreshCw size={15} aria-hidden /> Refresh
            </button>
          </div>
        )
      }
    >
      {error ? <ErrorState error={error} title="Couldn't load the clearinghouse" /> : null}
      {!data && !error && <ListSkeleton rows={3} icon={false} />}
      {data && (
        <ul className="list">
          {data.payers.map((p) => (
            <li className="list-row" key={p.id}>
              <div className="row-main">
                <div className="row-title">
                  {p.name} <span className="mono small muted">{p.payer_id}</span>
                </div>
                <div className="row-sub">
                  Claim status: {p.claim_status_route ?? SUPPORT_LABEL[p.support.claim_status ?? ""] ?? "not confirmed yet"} · Eligibility:{" "}
                  {SUPPORT_LABEL[p.support.eligibility ?? ""] ?? "not confirmed yet"}
                  {p.phone ? ` · ${p.phone}` : ""}
                </div>
              </div>
              <span className="chip">{p.source === "network" ? "From the network" : "Built in"}</span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function Stat({ icon: Icon, tint, value, label }: { icon: typeof Banknote; tint: string; value: number | undefined; label: string }) {
  return (
    <div className="card stat row" style={{ gap: 14 }}>
      <div className="row-icon" style={{ background: tint, width: 40, height: 40 }}>
        <Icon size={20} aria-hidden />
      </div>
      <div>
        <div className="stat-value">{value ?? <span className="skeleton" style={{ display: "inline-block", width: 40, height: 26 }} />}</div>
        <div className="stat-label">{label}</div>
      </div>
    </div>
  );
}
