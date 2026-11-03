import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, CircleDashed, Clock, Hash, Library, Mic, PhoneOutgoing, Plus, Sparkles, Trash2, XCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type AgentSummary } from "../api";
import { useCan } from "../App";
import { BrowserTestSheet } from "../BrowserTest";
import { CallBlocked } from "../CallBlocked";
import { Card, Empty, Pill, Toolbar, timeAgo, useToast } from "../ui";

type Category = "healthcare" | "travel" | "pharmacy" | "utilities" | "retail" | "government" | "other";

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
  updated_at: string;
}

interface Scorecard {
  attempt_id: string;
  mode: "rehearsal" | "live";
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
}

type ScenarioDetail = ScenarioRow & { runs: Scorecard[] };

const CATEGORY_LABEL: Record<Category, string> = {
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
        eyebrow="Plan a call, rehearse it, run it, score it"
        title="Voice Studio"
        actions={
          canEdit && (
            <div className="row" style={{ gap: 8 }}>
              <button className="btn btn-secondary" onClick={() => examples.mutate()} disabled={examples.isPending}>
                <Library size={15} aria-hidden /> Add examples
              </button>
              <button className="btn btn-primary" onClick={() => setSelected("new")}>
                <Plus size={16} aria-hidden /> New scenario
              </button>
            </div>
          )
        }
      />
      <div className="content">
        <div className="split" style={{ gridTemplateColumns: "300px minmax(0,1fr)", alignItems: "start" }}>
          <Card title="Scenarios">
            {rows.length === 0 ? (
              <Empty icon={Sparkles} title="No scenarios yet">
                Add the example library (airline, payer, pharmacy, subscription) or write your own.
              </Empty>
            ) : (
              <ul className="list">
                {rows.map((s) => (
                  <li key={s.id}>
                    <button type="button" className="list-row" onClick={() => setSelected(s.id)} aria-pressed={selected === s.id} style={{ width: "100%", textAlign: "left", background: selected === s.id ? "var(--accent-tint)" : undefined }}>
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
};

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
  const [rehearsing, setRehearsing] = useState(false);
  useEffect(() => {
    if (!isNew && detail.data && form === null) {
      const { id: _id, updated_at: _u, runs: _r, ...rest } = detail.data;
      setForm({ ...rest, facts: rest.facts ?? {} });
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
  const live = useMutation({
    mutationFn: () => api.post<{ attempt_id: string }>(`/scenarios/${id}/runs`, { mode: "live" }),
    onSuccess: () => {
      toast("Calling now. Follow it live in the run below or under Calls.");
      qc.invalidateQueries({ queryKey: ["scenario", id] });
    },
  });

  if (!form) return <Card pad>Loading…</Card>;
  const set = <K extends keyof typeof EMPTY>(k: K, v: (typeof EMPTY)[K]) => setForm({ ...form, [k]: v });
  const factRows = Object.entries(form.facts ?? {});
  const agent = outbound.find((a) => a.id === form.agent_id);

  return (
    <div className="stack" style={{ gap: 16 }}>
      <Card
        title={isNew ? "New scenario" : form.name}
        subtitle="The agent's whole job for this call: who, what, what it may say, and where it must stop."
        actions={
          !isNew && (
            <div className="row" style={{ gap: 8 }}>
              <button className="btn btn-secondary" disabled={!agent?.current} onClick={() => setRehearsing(true)} title={agent?.current ? "" : "Publish the agent first"}>
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
        <div className="card-pad stack" style={{ gap: 14 }}>
          {live.error && <CallBlocked error={live.error} onCleared={() => live.reset()} />}
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
            <Field label="Agent that calls" flex={1} help={agent ? (agent.current ? `Published v${agent.current.number}` : "Not published yet") : "Choose an outbound agent"}>
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
          <div className="row" style={{ gap: 12, flexWrap: "wrap" }}>
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
          <Field label="Rehearsal: how to play the other side" help="Shown to you during a browser rehearsal. Later used by the simulated counterpart.">
            <textarea className="textarea" rows={2} value={form.rehearsal_script ?? ""} onChange={(e) => set("rehearsal_script", e.target.value || null)} disabled={!canEdit} />
          </Field>
          {canEdit && (
            <div className="row" style={{ gap: 8 }}>
              <button className="btn btn-primary" onClick={() => save.mutate()} disabled={save.isPending || !form.name || !form.goal || !form.counterpart || !form.on_behalf_of}>
                Save scenario
              </button>
              {!isNew && (
                <button className="btn btn-plain" onClick={() => confirm("Delete this scenario? Past runs stay under Calls.") && remove.mutate()}>
                  Delete
                </button>
              )}
            </div>
          )}
        </div>
      </Card>

      {!isNew && <Runs runs={detail.data?.runs ?? []} />}

      {rehearsing && form.agent_id && (
        <BrowserTestSheet
          agentId={form.agent_id}
          use="published"
          scenarioId={id}
          rehearsalNote={form.rehearsal_script ?? `Answer as ${form.counterpart}.`}
          onClose={() => {
            setRehearsing(false);
            qc.invalidateQueries({ queryKey: ["scenario", id] });
          }}
        />
      )}
    </div>
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

function Runs({ runs }: { runs: Scorecard[] }) {
  return (
    <Card title="Runs and scorecards" subtitle="Measured from what the call recorded, not from what the agent says.">
      {runs.length === 0 ? (
        <div className="card-pad small muted">No runs yet. Rehearse in the browser first, then call live.</div>
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
                      {r.mode === "live" ? "Live call" : "Rehearsal"} · {timeAgo(r.started_at)}
                    </span>
                    <Link className="small" to={`/calls/${r.attempt_id}`}>
                      Open call
                    </Link>
                  </div>
                  {r.summary && <div>{r.summary}</div>}
                  <div className="row small" style={{ gap: 14, flexWrap: "wrap" }}>
                    <span title="Call length">⏱ {minutes(r.metrics.duration_s)}</span>
                    <span title="Time until a person answered">👤 {minutes(r.metrics.time_to_person_s)}</span>
                    <span title="Keypad presses in the phone menu">
                      <Hash size={12} aria-hidden /> {r.metrics.menu_presses} presses
                    </span>
                    <span title="Time on hold">
                      ⏸ {r.metrics.holds} hold{r.metrics.holds === 1 ? "" : "s"} · {minutes(r.metrics.hold_s)}
                    </span>
                    <span title="Actions the platform refused">⛔ {r.metrics.refused_actions} refused</span>
                  </div>
                  {(r.reference_number || r.options_offered || r.blocker || r.next_step) && (
                    <div className="small">
                      {r.reference_number && <div>Reference: <span className="mono">{r.reference_number}</span></div>}
                      {r.options_offered && <div>Options: {r.options_offered}</div>}
                      {r.blocker && <div>Blocker: {r.blocker}</div>}
                      {r.next_step && <div>Next: {r.next_step}</div>}
                    </div>
                  )}
                  {r.checks.length > 0 && (
                    <ul className="small muted" style={{ margin: 0, paddingLeft: 18 }}>
                      {r.checks.map((c) => (
                        <li key={c.check}>{c.check} · to review</li>
                      ))}
                    </ul>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}
