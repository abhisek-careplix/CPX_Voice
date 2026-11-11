import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, Bot, CheckCircle2, CircleDashed, CircleHelp, Clock, Hash, Library, Mic, OctagonX, Pause, PhoneOutgoing, Plus, Sparkles, Timer, Trash2, UserRound, XCircle, type LucideIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type AgentSummary, type ScenarioExpect } from "../api";
import { useCan } from "../App";
import { BrowserTestSheet } from "../BrowserTest";
import { CallBlocked } from "../CallBlocked";
import { Card, Empty, ErrorState, ListSkeleton, Pill, Toolbar, timeAgo, useToast } from "../ui";

type Category = "eligibility" | "claim_status" | "denials" | "prior_auth" | "patient_billing" | "healthcare" | "pharmacy" | "travel" | "utilities" | "retail" | "government" | "other";

interface ScenarioRow {
  id: string;
  name: string;
  category: Category;
  agent_id: string | null;
  counterpart: string;
  on_behalf_of: string;
  destination: string | null;
  goal: string;
  facts: Record<string, string> | null;
  limits: string[];
  success_checks: string[];
  rehearsal_script: string | null;
  simulation: { menu: string | null; hold_s: number; voice: string };
  expect: ScenarioExpect;
  updated_at: string;
}

interface Scorecard {
  attempt_id: string;
  mode: "rehearsal" | "simulation" | "live";
  state: string;
  started_at: string;
  verdict: "achieved" | "partly_achieved" | "not_achieved" | "running" | "unknown";
  summary: string | null;
  reference_number: string | null;
  options_offered: string | null;
  blocker: string | null;
  next_step: string | null;
  metrics: { duration_s: number | null; time_to_person_s: number | null; menu_presses: number; holds: number; hold_s: number; refused_actions: number; findings_recorded: number };
  ended: string | null;
  checks: { check: string; status: string }[];
  judge: Judgement | null;
}

interface Judgement {
  state: "pending" | "done" | "failed";
  verdict: "pass" | "partial" | "fail" | null;
  score: number | null;
  summary: string | null;
  checks: { check: string; status: "met" | "not_met" | "unclear"; evidence: string }[];
  limits: { limit: string; status: "kept" | "broken" | "unclear"; evidence: string }[];
  coaching: string[];
  model_id: string | null;
  error: string | null;
  judged_at: string | null;
}

type ScenarioDetail = ScenarioRow & { runs: Scorecard[]; judge_summary: { runs: number; average: number | null; passed: number } };

const JUDGE_VERDICT = {
  pass: { label: "Judge: pass", tone: "green" as const },
  partial: { label: "Judge: partial", tone: "orange" as const },
  fail: { label: "Judge: fail", tone: "red" as const },
};
const MARK: Record<string, [LucideIcon, string, string]> = {
  met: [CheckCircle2, "var(--green)", "Met"],
  kept: [CheckCircle2, "var(--green)", "Kept"],
  not_met: [XCircle, "var(--red)", "Not met"],
  broken: [OctagonX, "var(--red)", "Broken"],
  unclear: [CircleHelp, "var(--label-3)", "Unclear"],
};

function Mark({ status }: { status: string }) {
  const [Icon, color, label] = MARK[status] ?? MARK.unclear;
  return <Icon size={14} color={color} aria-label={label} />;
}

const CATEGORY_LABEL: Record<Category, string> = {
  eligibility: "Insurance & benefits",
  claim_status: "Claim status",
  denials: "Denials",
  prior_auth: "Prior authorization",
  patient_billing: "Patient balances",
  healthcare: "Healthcare",
  travel: "Travel",
  pharmacy: "Pharmacy",
  utilities: "Utilities",
  retail: "Retail",
  government: "Government",
  other: "Other",
};

const VERDICT: Record<Scorecard["verdict"], { label: string; tone: "green" | "orange" | "red" | "blue" | "gray"; icon: typeof CheckCircle2 }> = {
  achieved: { label: "Goal achieved", tone: "green", icon: CheckCircle2 },
  partly_achieved: { label: "Partly achieved", tone: "orange", icon: CircleDashed },
  not_achieved: { label: "Not achieved", tone: "red", icon: XCircle },
  running: { label: "In progress", tone: "blue", icon: Clock },
  unknown: { label: "No outcome recorded", tone: "gray", icon: CircleDashed },
};

const minutes = (s: number | null) => (s == null ? "—" : s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`);

export default function VoiceStudio() {
  const canEdit = useCan("edit_draft");
  const toast = useToast();
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["scenarios"], queryFn: () => api.get<{ categories: Category[]; scenarios: ScenarioRow[] }>("/scenarios") });
  const [selected, setSelected] = useState<string | "new" | null>(null);
  const examples = useMutation({
    mutationFn: () => api.post<{ added: number }>("/scenarios/examples"),
    onSuccess: (r) => {
      toast(r.added ? `Added ${r.added} example scenarios` : "Examples are already here");
      qc.invalidateQueries({ queryKey: ["scenarios"] });
      qc.invalidateQueries({ queryKey: ["agents"] });
    },
    onError: (e) => toast(e instanceof ApiError ? e.message : "Could not add examples"),
  });
  const rows = list.data?.scenarios ?? [];
  useEffect(() => {
    if (selected === null && rows.length) setSelected(rows[0].id);
  }, [rows, selected]);

  return (
    <>
      <Toolbar
        eyebrow="Build"
        title="Voice Studio"
        description="Write a scenario for each call your agent should handle, then rehearse it yourself or simulate it while you listen. The same scenarios make up the agent's regression suite."
        actions={
          canEdit && (
            <>
              <button className="btn btn-secondary" onClick={() => examples.mutate()} disabled={examples.isPending}>
                <Library size={15} aria-hidden /> {examples.isPending ? "Adding…" : "Add examples"}
              </button>
              <button className="btn btn-primary" onClick={() => setSelected("new")}>
                <Plus size={16} aria-hidden /> New scenario
              </button>
            </>
          )
        }
      />
      <div className="content">
        <div className="split split-list">
          <Card title="Scenarios" subtitle={list.data ? `${rows.length} in this workspace` : undefined}>
            {list.isPending && <ListSkeleton rows={4} icon={false} />}
            {list.error && <ErrorState error={list.error} onRetry={() => list.refetch()} title="Couldn't load scenarios" />}
            {list.data && rows.length === 0 ? (
              <Empty
                icon={Sparkles}
                title="No scenarios yet"
                action={
                  canEdit && (
                    <button className="btn btn-secondary" onClick={() => examples.mutate()} disabled={examples.isPending}>
                      <Library size={15} aria-hidden /> Add the example library
                    </button>
                  )
                }
              >
                Start from the revenue-cycle library (insurance checks, claim status, denials, prior auth, patient balances), or write your own with New scenario.
              </Empty>
            ) : (
              <ul className="list">
                {rows.map((s) => (
                  <li key={s.id}>
                    <button type="button" className="list-row" onClick={() => setSelected(s.id)} aria-pressed={selected === s.id}>
                      <div className="row-main">
                        <div className="row-title">{s.name}</div>
                        <div className="row-sub">
                          {CATEGORY_LABEL[s.category]} · calls {s.counterpart}
                        </div>
                      </div>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Card>
          {selected && <ScenarioPane key={selected} id={selected} categories={list.data?.categories ?? []} onSaved={setSelected} onDeleted={() => setSelected(null)} />}
        </div>
      </div>
    </>
  );
}

const EMPTY: Omit<ScenarioRow, "id" | "updated_at"> = {
  name: "",
  category: "other",
  agent_id: null,
  counterpart: "",
  on_behalf_of: "",
  destination: null,
  goal: "",
  facts: {},
  limits: [],
  success_checks: [],
  rehearsal_script: null,
  simulation: { menu: null, hold_s: 0, voice: "Charon" },
  // New scenarios expect the goal achieved; the library leaves the goal to the judge.
  expect: { outcome: "achieved", findings: [], max_duration_s: null, max_menu_presses: null, no_refused_actions: true },
};

const OUTCOME_LABEL: Record<ScenarioExpect["outcome"], string> = {
  achieved: "Achieved",
  achieved_or_partly: "Achieved or partly achieved",
  any: "Any (leave the goal to the AI judge)",
};

const VOICES = ["Charon", "Kore", "Fenrir", "Aoede", "Leda", "Orus", "Zephyr", "Puck"];

function ScenarioPane({ id, categories, onSaved, onDeleted }: { id: string; categories: Category[]; onSaved: (id: string) => void; onDeleted: () => void }) {
  const isNew = id === "new";
  const qc = useQueryClient();
  const toast = useToast();
  const canEdit = useCan("edit_draft");
  const canLive = useCan("start_task");
  const detail = useQuery({ queryKey: ["scenario", id], queryFn: () => api.get<ScenarioDetail>(`/scenarios/${id}`), enabled: !isNew, refetchInterval: 5000 });
  const agents = useQuery({ queryKey: ["agents"], queryFn: () => api.get<AgentSummary[]>("/agents") });
  const outbound = (agents.data ?? []).filter((a) => a.template.direction === "outbound");
  const [form, setForm] = useState<typeof EMPTY | null>(isNew ? EMPTY : null);
  // Rehearse and Simulate run the version chosen here; live calls always use the published one.
  const [running, setRunning] = useState<null | "rehearse" | "simulate">(null);
  const [use, setUse] = useState<"published" | "draft">("published");
  useEffect(() => {
    if (!isNew && detail.data && form === null) {
      const { id: _id, updated_at: _u, runs: _r, ...rest } = detail.data;
      setForm({ ...rest, facts: rest.facts ?? {}, simulation: rest.simulation ?? EMPTY.simulation, expect: rest.expect ?? EMPTY.expect });
    }
  }, [detail.data, form, isNew]);

  const save = useMutation({
    mutationFn: () => {
      const body = { ...form!, facts: form!.facts ?? {}, destination: form!.destination || null };
      return isNew ? api.post<ScenarioRow>("/scenarios", body) : api.put<ScenarioRow>(`/scenarios/${id}`, body);
    },
    onSuccess: (r) => {
      toast("Scenario saved");
      qc.invalidateQueries({ queryKey: ["scenarios"] });
      qc.invalidateQueries({ queryKey: ["scenario", r.id] });
      onSaved(r.id);
    },
    onError: (e) => toast(e instanceof ApiError ? e.message : "Could not save"),
  });
  const remove = useMutation({
    mutationFn: () => api.del(`/scenarios/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["scenarios"] });
      onDeleted();
    },
  });
  const isAdmin = useCan("admin");
  const live = useMutation({
    mutationFn: () => api.post<{ attempt_id: string }>(`/scenarios/${id}/runs`, { mode: "live" }),
    onSuccess: () => {
      toast("Calling now. Follow it live in the run below or under Calls.");
      qc.invalidateQueries({ queryKey: ["scenario", id] });
    },
  });
  // Outbound calls only go to approved numbers. An admin can approve this scenario's number here
  // (recorded in the audit log) instead of switching to Connections.
  const approveAndCall = useMutation({
    mutationFn: async () => {
      const o = await api.get<{ outbound: { approved_destinations: string[] } }>("/connections");
      const numbers = [...new Set([...o.outbound.approved_destinations, form!.destination!])];
      await api.put("/connections/outbound", { approved_destinations: numbers });
      return api.post<{ attempt_id: string }>(`/scenarios/${id}/runs`, { mode: "live" });
    },
    onSuccess: () => {
      live.reset();
      toast("Number approved. Calling now.");
      qc.invalidateQueries({ queryKey: ["scenario", id] });
      qc.invalidateQueries({ queryKey: ["connections"] });
    },
    onError: (e) => toast(e instanceof ApiError ? e.message : "Could not approve the number"),
  });
  const notApproved = live.error instanceof ApiError && live.error.code === "destination_not_approved";

  if (!form)
    return (
      <Card>
        {detail.error ? <ErrorState error={detail.error} onRetry={() => detail.refetch()} title="Couldn't load this scenario" /> : <ListSkeleton rows={3} icon={false} />}
      </Card>
    );
  const set = <K extends keyof typeof EMPTY>(k: K, v: (typeof EMPTY)[K]) => setForm({ ...form, [k]: v });
  const factRows = Object.entries(form.facts ?? {});
  const agent = outbound.find((a) => a.id === form.agent_id);
  const draftAhead = !!agent?.draft && (!agent.current || agent.draft.number > agent.current.number);
  const runnable = use === "draft" ? !!agent?.draft : !!agent?.current;
  const runHint = runnable ? "" : use === "draft" ? "This agent has no draft" : "Publish the agent first";

  return (
    <div className="stack" style={{ gap: 16 }}>
      <Card
        title={isNew ? "New scenario" : form.name}
        subtitle={isNew ? "The agent's whole job for this call: who, what, what it may say, and where it must stop." : `${CATEGORY_LABEL[form.category]} · calls ${form.counterpart || "—"}`}
        actions={
          !isNew && (
            <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
              <button className="btn btn-secondary" disabled={!runnable} onClick={() => setRunning("simulate")} title={runHint || "The agent calls an AI playing the other side. You listen; no one needs to talk."}>
                <Bot size={15} aria-hidden /> Simulate
              </button>
              <button className="btn btn-secondary" disabled={!runnable} onClick={() => setRunning("rehearse")} title={runHint}>
                <Mic size={15} aria-hidden /> Rehearse in browser
              </button>
              {canLive && (
                <button className="btn btn-primary" disabled={!form.destination || live.isPending} onClick={() => confirm(`Call ${form.counterpart} at ${form.destination} now?`) && live.mutate()}>
                  <PhoneOutgoing size={15} aria-hidden /> Call live
                </button>
              )}
            </div>
          )
        }
      >
        {!isNew && agent && (
          <div className="card-pad stack" style={{ gap: 10, paddingTop: 0 }}>
            <div className="row subcard" style={{ gap: 10, flexWrap: "wrap" }}>
              <span className="small">
                <strong>{agent.name}</strong> rehearses and simulates with
              </span>
              <select className="select" style={{ width: "auto" }} value={use} onChange={(e) => setUse(e.target.value as "published" | "draft")} aria-label="Agent version for rehearse and simulate">
                <option value="published" disabled={!agent.current}>
                  Published{agent.current ? ` v${agent.current.number}` : " (none yet)"}
                </option>
                <option value="draft" disabled={!agent.draft}>
                  Draft{agent.draft ? ` v${agent.draft.number}` : " (none)"}
                </option>
              </select>
              <span className="small muted">Live calls always use the published version.</span>
            </div>
            {draftAhead && use === "published" && (
              <div className="small" style={{ color: "var(--orange)" }} role="status">
                {agent.name} has unpublished changes in draft v{agent.draft!.number}. This scenario runs {agent.current ? `published v${agent.current.number}` : "nothing until you publish"}; choose the draft above to try them, or publish them in <Link to={`/agents/${agent.id}`}>Agents</Link>.
              </div>
            )}
          </div>
        )}
        <div className="card-pad stack" style={{ gap: 16, borderTop: "1px solid var(--separator)" }}>
          {live.error && !notApproved && <CallBlocked error={live.error} onCleared={() => live.reset()} />}
          {notApproved && (
            <div className="field-error" role="alert">
              {form.destination} isn't an approved number yet. Calls only go to approved numbers.{" "}
              {isAdmin ? (
                <button type="button" className="link-button" disabled={approveAndCall.isPending} onClick={() => approveAndCall.mutate()}>
                  {approveAndCall.isPending ? "Approving…" : "Approve this number and call"}
                </button>
              ) : (
                "Ask an administrator to approve it under Connections."
              )}
            </div>
          )}
          <div className="form-section">
          <h3 className="form-section-title">The call</h3>
          <div className="row" style={{ gap: 12, alignItems: "flex-start", flexWrap: "wrap" }}>
            <Field label="Name" flex={2}>
              <input className="input" value={form.name} onChange={(e) => set("name", e.target.value)} disabled={!canEdit} />
            </Field>
            <Field label="Category" flex={1}>
              <select className="select" value={form.category} onChange={(e) => set("category", e.target.value as Category)} disabled={!canEdit}>
                {(categories.length ? categories : (Object.keys(CATEGORY_LABEL) as Category[])).map((c) => (
                  <option key={c} value={c}>
                    {CATEGORY_LABEL[c]}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Agent that calls" flex={1} help={agent ? `${agent.current ? `Published v${agent.current.number}` : "Not published yet"}${draftAhead ? ` · draft v${agent.draft!.number} not published` : ""}` : "Choose an outbound agent"}>
              <select className="select" value={form.agent_id ?? ""} onChange={(e) => set("agent_id", e.target.value || null)} disabled={!canEdit}>
                <option value="">Choose…</option>
                {outbound.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name} · {a.template.name}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <div className="row" style={{ gap: 12, alignItems: "flex-start", flexWrap: "wrap" }}>
            <Field label="Calls (who answers)" flex={1}>
              <input className="input" value={form.counterpart} onChange={(e) => set("counterpart", e.target.value)} placeholder="Airline reservations" disabled={!canEdit} />
            </Field>
            <Field label="On behalf of" flex={1}>
              <input className="input" value={form.on_behalf_of} onChange={(e) => set("on_behalf_of", e.target.value)} placeholder="Sam Example (the passenger)" disabled={!canEdit} />
            </Field>
            <Field label="Number to call (live runs)" flex={1} help="Must be under Connections → Approved destinations">
              <input className="input" value={form.destination ?? ""} onChange={(e) => set("destination", e.target.value)} placeholder="+1…" disabled={!canEdit} />
            </Field>
          </div>
          </div>
          <div className="form-section">
          <h3 className="form-section-title">Goal and boundaries</h3>
          <Field label="Goal">
            <textarea className="textarea" rows={3} value={form.goal} onChange={(e) => set("goal", e.target.value)} disabled={!canEdit} />
          </Field>
          <Field label="Facts the agent may share" help="Only these, and only when needed. Stored encrypted.">
            <div className="stack" style={{ gap: 6 }}>
              {factRows.map(([k, v], i) => (
                <div className="row" key={i} style={{ gap: 6 }}>
                  <input className="input mono" style={{ maxWidth: 200 }} value={k} disabled={!canEdit} onChange={(e) => set("facts", Object.fromEntries(factRows.map(([kk, vv], j) => (j === i ? [e.target.value, vv] : [kk, vv]))))} />
                  <input className="input" value={v} disabled={!canEdit} onChange={(e) => set("facts", Object.fromEntries(factRows.map(([kk, vv], j) => (j === i ? [kk, e.target.value] : [kk, vv]))))} />
                  {canEdit && (
                    <button className="btn btn-plain btn-icon" aria-label="Remove fact" onClick={() => set("facts", Object.fromEntries(factRows.filter((_, j) => j !== i)))}>
                      <Trash2 size={15} />
                    </button>
                  )}
                </div>
              ))}
              {canEdit && (
                <button className="btn btn-secondary" style={{ alignSelf: "flex-start" }} onClick={() => set("facts", { ...form.facts, [`fact_${factRows.length + 1}`]: "" })}>
                  <Plus size={14} aria-hidden /> Add fact
                </button>
              )}
            </div>
          </Field>
          <div className="row" style={{ gap: 12, alignItems: "flex-start", flexWrap: "wrap" }}>
            <Field label="Limits (one per line)" flex={1}>
              <textarea className="textarea" rows={3} value={form.limits.join("\n")} onChange={(e) => set("limits", e.target.value.split("\n"))} disabled={!canEdit} />
            </Field>
            <Field label="Done when (one per line)" flex={1}>
              <textarea className="textarea" rows={3} value={form.success_checks.join("\n")} onChange={(e) => set("success_checks", e.target.value.split("\n"))} disabled={!canEdit} />
            </Field>
          </div>
          </div>
          <PassRules expect={form.expect} canEdit={canEdit} onChange={(e) => set("expect", e)} />
          <div className="form-section">
          <h3 className="form-section-title">The other side</h3>
          <p className="form-section-help">Used by browser rehearsals and by the simulated counterpart.</p>
          <Field label="How to play the other side" help="Shown to you in a browser rehearsal, and followed by the simulated counterpart.">
            <textarea className="textarea" rows={2} value={form.rehearsal_script ?? ""} onChange={(e) => set("rehearsal_script", e.target.value || null)} disabled={!canEdit} />
          </Field>
          <div className="row" style={{ gap: 12, alignItems: "flex-start", flexWrap: "wrap" }}>
            <Field label="Simulation: phone menu (optional)" flex={2} help="The simulated line reads this first and waits for keypad presses.">
              <textarea className="textarea" rows={2} value={form.simulation.menu ?? ""} onChange={(e) => set("simulation", { ...form.simulation, menu: e.target.value || null })} disabled={!canEdit} />
            </Field>
            <Field label="Hold (seconds)" flex={1} help="Hold music before a person answers. 0 = none.">
              <input className="input" type="number" min={0} max={600} value={form.simulation.hold_s} onChange={(e) => set("simulation", { ...form.simulation, hold_s: Number(e.target.value) || 0 })} disabled={!canEdit} />
            </Field>
            <Field label="Counterpart voice" flex={1}>
              <select className="select" value={form.simulation.voice} onChange={(e) => set("simulation", { ...form.simulation, voice: e.target.value })} disabled={!canEdit}>
                {VOICES.map((v) => (
                  <option key={v}>{v}</option>
                ))}
              </select>
            </Field>
          </div>
          </div>
        </div>
        {canEdit && (
          <div className="form-footer">
            <button className="btn btn-primary" onClick={() => save.mutate()} disabled={save.isPending || !form.name || !form.goal || !form.counterpart || !form.on_behalf_of}>
              {save.isPending ? "Saving…" : isNew ? "Create scenario" : "Save scenario"}
            </button>
            {(!form.name || !form.goal || !form.counterpart || !form.on_behalf_of) && <span className="small muted">Name, who answers, on behalf of and goal are required.</span>}
            <span className="spacer" />
            {!isNew && (
              <button className="btn btn-plain" style={{ color: "var(--red)" }} onClick={() => confirm("Delete this scenario? Past runs stay under Calls.") && remove.mutate()}>
                <Trash2 size={14} aria-hidden /> Delete
              </button>
            )}
          </div>
        )}
      </Card>

      {!isNew && <Runs scenarioId={id} runs={detail.data?.runs ?? []} summary={detail.data?.judge_summary} />}

      {running && form.agent_id && (
        <BrowserTestSheet
          agentId={form.agent_id}
          use={use}
          scenarioId={id}
          simulate={running === "simulate"}
          rehearsalNote={form.rehearsal_script ?? `Answer as ${form.counterpart}.`}
          onClose={() => {
            setRunning(null);
            qc.invalidateQueries({ queryKey: ["scenario", id] });
          }}
        />
      )}
    </div>
  );
}

/** Pass rules for the regression suite: checked from the run's record, before the AI judge. */
function PassRules({ expect, canEdit, onChange }: { expect: ScenarioExpect; canEdit: boolean; onChange: (e: ScenarioExpect) => void }) {
  const set = <K extends keyof ScenarioExpect>(k: K, v: ScenarioExpect[K]) => onChange({ ...expect, [k]: v });
  const num = (v: string) => (v.trim() ? Math.max(1, Number(v) || 1) : null);
  return (
    <fieldset className="form-section" style={{ border: 0, borderTop: "1px solid var(--separator)", padding: "16px 0 0", margin: 0, minWidth: 0 }}>
      <legend className="form-section-title" style={{ float: "left", width: "100%", padding: 0 }}>
        Pass rules for the regression suite
      </legend>
      <p className="form-section-help" style={{ marginTop: 0 }}>
        Checked by the platform from what the call recorded. A suite run passes only when these hold and the AI judge passes it.
      </p>
      <div className="row" style={{ gap: 12, flexWrap: "wrap", alignItems: "flex-start" }}>
        <Field label="Goal outcome the agent must record" flex={2}>
          <select className="select" value={expect.outcome} onChange={(e) => set("outcome", e.target.value as ScenarioExpect["outcome"])} disabled={!canEdit}>
            {(Object.keys(OUTCOME_LABEL) as ScenarioExpect["outcome"][]).map((o) => (
              <option key={o} value={o}>
                {OUTCOME_LABEL[o]}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Max call length (seconds)" flex={1}>
          <input className="input" type="number" min={1} value={expect.max_duration_s ?? ""} onChange={(e) => set("max_duration_s", num(e.target.value))} placeholder="No limit" disabled={!canEdit} />
        </Field>
        <Field label="Max keypad presses" flex={1}>
          <input className="input" type="number" min={1} value={expect.max_menu_presses ?? ""} onChange={(e) => set("max_menu_presses", num(e.target.value))} placeholder="No limit" disabled={!canEdit} />
        </Field>
      </div>
      <div className="stack" style={{ gap: 6 }}>
        <span className="field-label">Findings the agent must record</span>
        {expect.findings.map((f, i) => (
          <div className="row" key={i} style={{ gap: 6 }}>
            <input className="input mono" style={{ maxWidth: 200 }} value={f.field} placeholder="reference_number" disabled={!canEdit} onChange={(e) => set("findings", expect.findings.map((x, j) => (j === i ? { ...x, field: e.target.value } : x)))} />
            <input className="input mono" value={f.pattern ?? ""} placeholder="Pattern, optional (e.g. ^CLM-\d+$)" disabled={!canEdit} onChange={(e) => set("findings", expect.findings.map((x, j) => (j === i ? { ...x, pattern: e.target.value || null } : x)))} />
            {canEdit && (
              <button className="btn btn-plain btn-icon" aria-label="Remove finding rule" onClick={() => set("findings", expect.findings.filter((_, j) => j !== i))}>
                <Trash2 size={15} />
              </button>
            )}
          </div>
        ))}
        {canEdit && expect.findings.length < 10 && (
          <button className="btn btn-secondary" style={{ alignSelf: "flex-start" }} onClick={() => set("findings", [...expect.findings, { field: "", pattern: null }])}>
            <Plus size={14} aria-hidden /> Add finding rule
          </button>
        )}
      </div>
      <label className="row small" style={{ gap: 8 }}>
        <input type="checkbox" checked={expect.no_refused_actions} onChange={(e) => set("no_refused_actions", e.target.checked)} disabled={!canEdit} /> Fail the run if the platform refused any action
      </label>
    </fieldset>
  );
}

function Field({ label, help, flex, children }: { label: string; help?: string; flex?: number; children: React.ReactNode }) {
  return (
    <label className="field" style={{ flex: flex ?? undefined, minWidth: flex ? 180 : undefined }}>
      <span className="field-label">{label}</span>
      {children}
      {help && <span className="field-help">{help}</span>}
    </label>
  );
}

function Runs({ scenarioId, runs, summary }: { scenarioId: string; runs: Scorecard[]; summary?: ScenarioDetail["judge_summary"] }) {
  const subtitle =
    summary && summary.runs > 0
      ? `Measured from what the call recorded. AI judge: average ${summary.average}/100 over the last ${summary.runs} scored run${summary.runs === 1 ? "" : "s"}, ${summary.passed} passed.`
      : "Measured from what the call recorded, not from what the agent says. Finished runs are scored by the AI judge.";
  return (
    <Card title="Runs and scorecards" subtitle={subtitle}>
      {runs.length === 0 ? (
        <Empty icon={Mic} title="No runs yet">
          Rehearse in the browser or simulate the call first. Each run gets a scorecard here.
        </Empty>
      ) : (
        <ul className="list">
          {runs.map((r) => {
            const v = VERDICT[r.verdict];
            return (
              <li key={r.attempt_id} className="list-row" style={{ alignItems: "flex-start", paddingTop: 14, paddingBottom: 14 }}>
                <div className="row-main stack" style={{ gap: 6 }}>
                  <div className="row" style={{ gap: 8 }}>
                    <Pill tone={v.tone} icon={v.icon}>
                      {v.label}
                    </Pill>
                    <span className="small muted">
                      {r.mode === "live" ? "Live call" : r.mode === "simulation" ? "Simulation" : "Rehearsal"} · {timeAgo(r.started_at)}
                    </span>
                    <Link className="small" to={`/calls/${r.attempt_id}`}>
                      Open call
                    </Link>
                  </div>
                  {r.summary && <div>{r.summary}</div>}
                  <div className="metrics">
                    <span title="Call length">
                      <Timer size={13} aria-hidden /> <span className="sr-only">Call length</span>
                      {minutes(r.metrics.duration_s)}
                    </span>
                    <span title="Time until a person answered">
                      <UserRound size={13} aria-hidden /> <span className="sr-only">Time to a person</span>
                      {minutes(r.metrics.time_to_person_s)}
                    </span>
                    <span title="Keypad presses in the phone menu">
                      <Hash size={13} aria-hidden /> {r.metrics.menu_presses} presses
                    </span>
                    <span title="Time on hold">
                      <Pause size={13} aria-hidden /> {r.metrics.holds} hold{r.metrics.holds === 1 ? "" : "s"} · {minutes(r.metrics.hold_s)}
                    </span>
                    <span title="Actions the platform refused">
                      <Ban size={13} aria-hidden /> {r.metrics.refused_actions} refused
                    </span>
                  </div>
                  {(r.reference_number || r.options_offered || r.blocker || r.next_step) && (
                    <div className="small">
                      {r.reference_number && <div>Reference: <span className="mono">{r.reference_number}</span></div>}
                      {r.options_offered && <div>Options: {r.options_offered}</div>}
                      {r.blocker && <div>Blocker: {r.blocker}</div>}
                      {r.next_step && <div>Next: {r.next_step}</div>}
                    </div>
                  )}
                  {r.judge?.state === "done" ? null : r.checks.length > 0 && (
                    <ul className="small muted" style={{ margin: 0, paddingLeft: 18 }}>
                      {r.checks.map((c) => (
                        <li key={c.check}>{c.check} · to review</li>
                      ))}
                    </ul>
                  )}
                  <JudgePanel scenarioId={scenarioId} run={r} />
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

function JudgePanel({ scenarioId, run }: { scenarioId: string; run: Scorecard }) {
  const canEdit = useCan("edit_draft");
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const score = useMutation({
    mutationFn: () => api.post<Judgement>(`/scenarios/${scenarioId}/runs/${run.attempt_id}/judge`, {}),
    onSettled: () => qc.invalidateQueries({ queryKey: ["scenario", scenarioId] }),
  });
  const j = run.judge;
  const finished = !["running"].includes(run.verdict) && !["Created", "Queued", "Dialing", "Connected", "Ending"].includes(run.state);
  const button = canEdit && finished && (
    <button className="btn btn-plain" disabled={score.isPending} onClick={() => score.mutate()}>
      <Sparkles size={13} aria-hidden /> {score.isPending ? "Scoring…" : j ? "Score again" : "Score with AI judge"}
    </button>
  );
  if (!j || j.state === "pending") {
    return (
      <div className="row small muted" style={{ gap: 8 }}>
        {j?.state === "pending" || (finished && !j) ? <span>AI judge: scoring shortly…</span> : null}
        {button}
      </div>
    );
  }
  if (j.state === "failed") {
    return (
      <div className="row small" style={{ gap: 8 }}>
        <span className="muted">AI judge couldn't score this run: {j.error}</span>
        {button}
      </div>
    );
  }
  const v = JUDGE_VERDICT[j.verdict ?? "fail"];
  return (
    <div className="judge-panel">
      <div className="row" style={{ gap: 8 }}>
        <Pill tone={v.tone} icon={Sparkles}>
          {v.label} · {j.score}/100
        </Pill>
        <button className="btn btn-plain" onClick={() => setOpen(!open)} aria-expanded={open}>
          {open ? "Hide evidence" : "Show evidence"}
        </button>
        {button}
      </div>
      {j.summary && <div className="small">{j.summary}</div>}
      {open && (
        <div className="small stack" style={{ gap: 6 }}>
          {j.checks.length > 0 && (
            <div>
              <strong>Success checks</strong>
              <ul className="mark-list">
                {j.checks.map((c) => (
                  <li key={c.check}>
                    <Mark status={c.status} />
                    <span>
                      {c.check}
                      {c.evidence && <span className="muted" style={{ display: "block" }}>“{c.evidence}”</span>}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {j.limits.length > 0 && (
            <div>
              <strong>Limits</strong>
              <ul className="mark-list">
                {j.limits.map((c) => (
                  <li key={c.limit}>
                    <Mark status={c.status} />
                    <span>
                      {c.limit}
                      {c.evidence && <span className="muted" style={{ display: "block" }}>“{c.evidence}”</span>}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {j.coaching.length > 0 && (
            <div>
              <strong>Coaching for the agent</strong>
              <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                {j.coaching.map((c) => (
                  <li key={c}>{c}</li>
                ))}
              </ul>
            </div>
          )}
          <div className="muted">Scored by {j.model_id} from the transcript and measured events.</div>
        </div>
      )}
    </div>
  );
}
