import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, ChevronLeft, ChevronRight, CircleDashed, FileUp, GitCompareArrows, Plus, Square, XCircle } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  api,
  ApiError,
  type AgentSummary,
  type ShadowItem,
  type ShadowOutcome,
  type ShadowPreview,
  type ShadowRun,
  type ShadowRunDetail,
  type ShadowVerdict,
  type Template,
} from "../api";
import { useCan } from "../App";
import { Banner, Card, Empty, ErrorState, ListSkeleton, Pill, Segmented, Sheet, Toolbar, humanize, timeAgo } from "../ui";

type Tone = "green" | "orange" | "red" | "blue" | "purple" | "gray";

const RUN_STATE: Record<ShadowRun["state"], [Tone, string]> = {
  queued: ["blue", "Queued"],
  running: ["blue", "Running"],
  done: ["green", "Finished"],
  cancelled: ["gray", "Cancelled"],
};

const OUTCOME: Record<ShadowOutcome, [Tone, string]> = {
  match: ["green", "Same answer"],
  mismatch: ["red", "Different"],
  ours_only: ["blue", "Only CarePlix"],
  theirs_only: ["orange", "Only incumbent"],
  neither: ["gray", "Neither"],
};

const VERDICT_LABEL: Record<ShadowVerdict, string> = {
  ours: "CarePlix was right",
  theirs: "Incumbent was right",
  both: "Both acceptable",
  neither: "Neither was right",
};

const INCUMBENTS = ["SuperDial", "Infinitus", "VoiceCare AI", "Prosper AI", "Hippocratic AI", "Assort Health", "In-house team"];

const SYNONYMS: Record<string, string[]> = {
  authorization: ["auth"],
  reference: ["ref", "number", "no"],
  reported: ["status"],
  status: ["state"],
  summary: ["result", "status"],
  steps: ["step", "action"],
};

/** Pre-match the agent's answer fields to the file's columns: a column matches a field when it
 * contains every word of the field (or a synonym); the best matches are assigned first. */
function guessValues(fields: string[], columns: string[]): Record<string, string> {
  const words = (c: string) => c.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
  const pairs: [number, string, string][] = [];
  for (const field of fields) {
    const tokens = field.split("_");
    for (const col of columns) {
      const have = words(col);
      const hits = tokens.filter((t) => [t, ...(SYNONYMS[t] ?? [])].some((w) => have.includes(w))).length;
      if (hits === tokens.length) pairs.push([hits, field, col]);
    }
  }
  pairs.sort((a, b) => b[0] - a[0]);
  const out: Record<string, string> = {};
  const taken = new Set<string>();
  for (const [, field, col] of pairs) {
    if (!out[field] && !taken.has(col)) {
      out[field] = col;
      taken.add(col);
    }
  }
  return out;
}

const pct = (v: number | null | undefined) => (v == null ? "—" : `${Math.round(v * 100)}%`);
const times = (n: number) => (n === 0 ? "never" : n === 1 ? "once" : n === 2 ? "twice" : `${n} times`);
const mins = (s: number | null | undefined) => (s == null ? "—" : s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`);

export default function ShadowMode() {
  const { id } = useParams();
  return id ? <RunDetail id={id} /> : <RunList />;
}

// ---------- List ----------

function RunList() {
  const canRun = useCan("browser_test");
  const runs = useQuery({ queryKey: ["shadow-runs"], queryFn: () => api.get<ShadowRun[]>("/shadow-runs"), refetchInterval: 8000 });
  const [creating, setCreating] = useState(false);
  return (
    <>
      <Toolbar
        eyebrow="Build"
        title="Shadow mode"
        description="Run CarePlix on a worklist your current vendor already worked, then compare the answers side by side. Settle each disagreement, and take the parity report to your switch decision."
        actions={
          canRun && (
            <button className="btn btn-primary" onClick={() => setCreating(true)}>
              <Plus size={16} aria-hidden /> New shadow run
            </button>
          )
        }
      />
      <div className="content">
        <Card>
          {runs.isPending && <ListSkeleton rows={3} />}
          {runs.error && <ErrorState error={runs.error} onRetry={() => runs.refetch()} title="Couldn't load shadow runs" />}
          {runs.data?.length === 0 ? (
            <Empty
              icon={GitCompareArrows}
              title="No shadow runs yet"
              action={
                canRun && (
                  <button className="btn btn-primary" onClick={() => setCreating(true)}>
                    <FileUp size={16} aria-hidden /> Import a worklist
                  </button>
                )
              }
            >
              Export the calls your current vendor made (one row per call, with the case details and the answers it recorded) as a CSV file. CarePlix works the same rows and shows where the answers agree.
            </Empty>
          ) : (
            <ul className="list">
              {runs.data?.map((r) => {
                const [tone, label] = RUN_STATE[r.state];
                return (
                  <li key={r.id}>
                    <Link to={`/shadow/${r.id}`} className="list-row">
                      <div className="row-icon" style={{ background: "var(--tile-navy)" }}>
                        <GitCompareArrows size={18} aria-hidden />
                      </div>
                      <div className="row-main">
                        <div className="row-title">{r.name}</div>
                        <div className="row-sub">
                          CarePlix vs {r.incumbent} · {r.mode === "replay" ? "Replay" : "Live calls"} · {timeAgo(r.created_at)}
                        </div>
                      </div>
                      {r.summary && <span className="small muted tabular">{pct(r.summary.agreement)} agreement</span>}
                      <Pill tone={tone} live={r.state === "running"}>
                        {label}
                      </Pill>
                      <ChevronRight size={16} className="chevron" aria-hidden />
                    </Link>
                  </li>
                );
              })}
            </ul>
          )}
        </Card>
      </div>
      {creating && <NewRun onClose={() => setCreating(false)} />}
    </>
  );
}

// ---------- New run ----------

interface FactRow {
  name: string;
  column: string;
}

function NewRun({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const canLive = useCan("start_task");
  const agents = useQuery({ queryKey: ["agents"], queryFn: () => api.get<AgentSummary[]>("/agents") });
  const templates = useQuery({ queryKey: ["templates"], queryFn: () => api.get<Template[]>("/templates") });
  const scenarios = useQuery({ queryKey: ["scenarios"], queryFn: () => api.get<{ scenarios: { id: string; name: string; agent_id: string | null }[] }>("/scenarios") });
  const outbound = (agents.data ?? []).filter((a) => a.template.direction === "outbound");

  const [name, setName] = useState("");
  const [incumbent, setIncumbent] = useState("SuperDial");
  const [agentId, setAgentId] = useState("");
  const [use, setUse] = useState<"published" | "draft">("published");
  const [scenarioId, setScenarioId] = useState("");
  const [mode, setMode] = useState<"replay" | "live">("replay");
  const [csv, setCsv] = useState<string | null>(null);
  const [fileName, setFileName] = useState("");
  const [preview, setPreview] = useState<ShadowPreview | null>(null);
  const [refCol, setRefCol] = useState("");
  const [phoneCol, setPhoneCol] = useState("");
  const [durationCol, setDurationCol] = useState("");
  const [resolvedCol, setResolvedCol] = useState("");
  const [facts, setFacts] = useState<FactRow[]>([]);
  const [values, setValues] = useState<Record<string, string>>({});

  const agent = outbound.find((a) => a.id === agentId);
  const fields = useMemo(() => Object.keys(templates.data?.find((t) => t.id === agent?.template.id)?.output_fields ?? {}), [templates.data, agent]);
  const agentScenarios = (scenarios.data?.scenarios ?? []).filter((s) => !agentId || s.agent_id === agentId);

  const readFile = useMutation({
    mutationFn: async (file: File) => {
      const text = await file.text();
      const p = await api.post<ShadowPreview>("/shadow-runs/preview", { csv: text });
      return { text, p, file };
    },
    onSuccess: ({ text, p, file }) => {
      setCsv(text);
      setFileName(file.name);
      setPreview(p);
      const find = (...words: string[]) => p.columns.find((c) => words.some((w) => c.toLowerCase().includes(w))) ?? "";
      setRefCol(find("id", "ref", "case"));
      setPhoneCol(find("phone", "number", "destination"));
      setDurationCol(find("duration", "minutes", "length", "time"));
      setResolvedCol(find("resolved", "complete", "done", "success"));
      setValues(guessValues(fields, p.columns));
      if (!name) setName(file.name.replace(/\.csv$/i, ""));
    },
  });

  const create = useMutation({
    mutationFn: () =>
      api.post<ShadowRunDetail>("/shadow-runs", {
        name: name || "Shadow run",
        incumbent,
        agent_id: agentId,
        use,
        scenario_id: scenarioId,
        mode,
        csv,
        mapping: {
          external_ref: refCol || null,
          destination: phoneCol || null,
          duration_s: durationCol || null,
          resolved: resolvedCol || null,
          facts: Object.fromEntries(facts.filter((f) => f.name && f.column).map((f) => [f.name, f.column])),
          values: Object.fromEntries(Object.entries(values).filter(([, col]) => col)),
        },
      }),
    onSuccess: (run) => {
      qc.invalidateQueries({ queryKey: ["shadow-runs"] });
      navigate(`/shadow/${run.id}`);
    },
  });

  const mapped = Object.values(values).filter(Boolean).length;
  const tooMany = !!preview && preview.total > preview.max_items;
  const ready = !!agentId && !!scenarioId && !!csv && mapped > 0 && !tooMany && (mode === "replay" || !!phoneCol);
  const columnOptions = (
    <>
      <option value="">Not used</option>
      {preview?.columns.map((c) => (
        <option key={c} value={c}>
          {c}
        </option>
      ))}
    </>
  );

  return (
    <Sheet
      wide
      title="New shadow run"
      subtitle="CarePlix works every row of your current vendor’s worklist again, with the same case details, and compares the answers."
      onClose={onClose}
      footer={
        <>
          {!ready && <span className="small muted" style={{ marginRight: "auto" }}>Choose an agent and a call plan, upload the worklist, and map at least one answer column.</span>}
          <button className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" disabled={!ready || create.isPending} onClick={() => create.mutate()}>
            <GitCompareArrows size={15} aria-hidden /> {create.isPending ? "Starting…" : `Start shadow run${preview ? ` · ${preview.total} rows` : ""}`}
          </button>
        </>
      }
    >
      {create.error && <Banner tone="red" title={(create.error as ApiError).message} />}

      <div className="form-section">
        <h3 className="form-section-title">1. What to compare</h3>
        <div className="grid-2">
          <label className="field">
            <span className="field-label">Name</span>
            <input className="input" value={name} maxLength={120} onChange={(e) => setName(e.target.value)} placeholder="Claim status, October worklist" />
          </label>
          <label className="field">
            <span className="field-label">Current vendor</span>
            <input className="input" list="incumbents" value={incumbent} maxLength={80} onChange={(e) => setIncumbent(e.target.value)} />
            <datalist id="incumbents">
              {INCUMBENTS.map((v) => (
                <option key={v} value={v} />
              ))}
            </datalist>
          </label>
          <label className="field">
            <span className="field-label">CarePlix agent</span>
            <select
              className="select"
              value={agentId}
              onChange={(e) => {
                const next = outbound.find((a) => a.id === e.target.value);
                const nextFields = Object.keys(templates.data?.find((t) => t.id === next?.template.id)?.output_fields ?? {});
                setAgentId(e.target.value);
                setScenarioId("");
                setValues(preview ? guessValues(nextFields, preview.columns) : {});
              }}
            >
              <option value="">Choose an agent…</option>
              {outbound.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name} · {a.template.name}
                </option>
              ))}
            </select>
            {agent && (
              <span className="field-help">
                Runs {use === "published" ? (agent.current ? `published v${agent.current.number}` : "no published version yet") : agent.draft ? `draft v${agent.draft.number}` : "no draft"}.
              </span>
            )}
          </label>
          <div className="field">
            <span className="field-label">Version</span>
            <Segmented
              label="Agent version"
              value={use}
              onChange={setUse}
              options={[
                { value: "published", label: "Published" },
                { value: "draft", label: "Draft" },
              ]}
            />
          </div>
          <label className="field">
            <span className="field-label">Call plan (Voice Studio scenario)</span>
            <select className="select" value={scenarioId} onChange={(e) => setScenarioId(e.target.value)}>
              <option value="">Choose a scenario…</option>
              {agentScenarios.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
            <span className="field-help">Its goal, limits and phone menu apply to every row. Each row brings its own case details.</span>
          </label>
          <div className="field">
            <span className="field-label">How CarePlix works each row</span>
            <Segmented
              label="Shadow mode"
              value={mode}
              onChange={(v) => setMode(v)}
              options={[
                { value: "replay", label: "Replay" },
                ...(canLive ? [{ value: "live" as const, label: "Live calls" }] : []),
              ]}
            />
            <span className="field-help">
              {mode === "replay"
                ? "An AI plays the other side using the answers your vendor recorded. Measures whether CarePlix gets the same answers. No real calls."
                : "CarePlix calls each row’s number itself, one call at a time. Approved numbers only."}
            </span>
          </div>
        </div>
      </div>

      <div className="form-section">
        <h3 className="form-section-title">2. The worklist</h3>
        <p className="form-section-help">A CSV export from your current vendor: one row per call, up to 200 rows.</p>
        <label className="btn btn-secondary" style={{ justifySelf: "start", cursor: "pointer" }}>
          <FileUp size={15} aria-hidden /> {fileName ? "Choose another file" : "Choose CSV file"}
          <input
            type="file"
            accept=".csv,text/csv"
            className="sr-only"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) readFile.mutate(file);
            }}
          />
        </label>
        {readFile.isPending && <span className="small muted">Reading the file…</span>}
        {readFile.error && <span className="field-error">{(readFile.error as ApiError).message}</span>}
        {preview && (
          <>
            <div className="small">
              <strong>{fileName}</strong> · {preview.total} rows · {preview.columns.length} columns
            </div>
            {tooMany && <Banner tone="orange" title={`This file has ${preview.total} rows`}>A shadow run takes at most {preview.max_items}. Split the worklist into smaller files.</Banner>}
            <div style={{ overflowX: "auto", border: "1px solid var(--separator)", borderRadius: "var(--r-sm)" }}>
              <table className="suite-table small">
                <thead>
                  <tr>
                    {preview.columns.map((c) => (
                      <th key={c}>{c}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {preview.sample.slice(0, 3).map((row, i) => (
                    <tr key={i}>
                      {preview.columns.map((c) => (
                        <td key={c} className="mono" style={{ whiteSpace: "nowrap" }}>
                          {row[c]}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>

      {preview && (
        <div className="form-section">
          <h3 className="form-section-title">3. Match the columns</h3>
          <div className="grid-2">
            <label className="field">
              <span className="field-label">Row ID</span>
              <select className="select" value={refCol} onChange={(e) => setRefCol(e.target.value)}>
                {columnOptions}
              </select>
              <span className="field-help">Your vendor’s id for the call, so you can find it again.</span>
            </label>
            <label className="field">
              <span className="field-label">Phone number{mode === "live" ? " (required)" : ""}</span>
              <select className="select" value={phoneCol} onChange={(e) => setPhoneCol(e.target.value)}>
                {columnOptions}
              </select>
            </label>
            <label className="field">
              <span className="field-label">Call length</span>
              <select className="select" value={durationCol} onChange={(e) => setDurationCol(e.target.value)}>
                {columnOptions}
              </select>
              <span className="field-help">Seconds, m:ss, or “3m 4s”.</span>
            </label>
            <label className="field">
              <span className="field-label">Resolved</span>
              <select className="select" value={resolvedCol} onChange={(e) => setResolvedCol(e.target.value)}>
                {columnOptions}
              </select>
              <span className="field-help">Yes/no, true/false, resolved/unresolved.</span>
            </label>
          </div>

          <div className="field">
            <span className="field-label">Case details the agent may share</span>
            <span className="field-help">Only these columns are given to the agent, and only shared when needed. Stored encrypted.</span>
            <div className="stack" style={{ gap: 6 }}>
              {facts.map((f, i) => (
                <div className="row" key={i} style={{ gap: 6 }}>
                  <input className="input mono" style={{ maxWidth: 220 }} value={f.name} placeholder="member_id" aria-label="Fact name" onChange={(e) => setFacts(facts.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))} />
                  <select className="select" value={f.column} aria-label="Column" onChange={(e) => setFacts(facts.map((x, j) => (j === i ? { ...x, column: e.target.value } : x)))}>
                    {columnOptions}
                  </select>
                  <button type="button" className="btn btn-plain btn-icon" aria-label="Remove case detail" onClick={() => setFacts(facts.filter((_, j) => j !== i))}>
                    <XCircle size={15} aria-hidden />
                  </button>
                </div>
              ))}
              <button type="button" className="btn btn-secondary" onClick={() => setFacts([...facts, { name: "", column: "" }])}>
                <Plus size={14} aria-hidden /> Add case detail
              </button>
            </div>
          </div>

          <div className="field">
            <span className="field-label">Answers to compare</span>
            {!agent ? (
              <span className="field-help">Choose an agent first: the answers are the findings it records.</span>
            ) : (
              <div className="grid-2">
                {fields.map((f) => (
                  <label className="field" key={f}>
                    <span className="small">{humanize(f)}</span>
                    <select className="select" value={values[f] ?? ""} onChange={(e) => setValues({ ...values, [f]: e.target.value })}>
                      <option value="">Not compared</option>
                      {preview.columns.map((c) => (
                        <option key={c} value={c}>
                          {c}
                        </option>
                      ))}
                    </select>
                  </label>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </Sheet>
  );
}

// ---------- Detail ----------

function RunDetail({ id }: { id: string }) {
  const qc = useQueryClient();
  const canRun = useCan("browser_test");
  const canSettle = useCan("review");
  const run = useQuery({
    queryKey: ["shadow-run", id],
    queryFn: () => api.get<ShadowRunDetail>(`/shadow-runs/${id}`),
    refetchInterval: (q) => (q.state.data && ["queued", "running"].includes(q.state.data.state) ? 4000 : false),
  });
  const cancel = useMutation({
    mutationFn: () => api.post<ShadowRunDetail>(`/shadow-runs/${id}/cancel`),
    onSuccess: (r) => {
      qc.setQueryData(["shadow-run", id], r);
      qc.invalidateQueries({ queryKey: ["shadow-runs"] });
    },
  });
  const [filter, setFilter] = useState<"all" | "differ" | "open">("all");
  const back = (
    <Link to="/shadow" className="row" style={{ gap: 2 }}>
      <ChevronLeft size={15} aria-hidden /> Shadow mode
    </Link>
  );
  if (run.error) {
    return (
      <>
        <Toolbar eyebrow={back} title="Shadow run" />
        <div className="content">
          <Card>
            <ErrorState error={run.error} onRetry={() => run.refetch()} title="Couldn't load this shadow run" />
          </Card>
        </div>
      </>
    );
  }
  const r = run.data;
  if (!r) return <Toolbar eyebrow={back} title={<span className="skeleton" style={{ display: "inline-block", width: 260, height: 28 }} aria-label="Loading" />} />;

  const s = r.summary;
  const [tone, label] = RUN_STATE[r.state];
  const active = r.state === "queued" || r.state === "running";
  const differs = (i: ShadowItem) => (i.comparison ?? []).some((c) => c.outcome === "mismatch" || c.outcome === "ours_only" || c.outcome === "theirs_only");
  const unsettled = (i: ShadowItem) => (i.comparison ?? []).some((c) => ["mismatch", "ours_only", "theirs_only"].includes(c.outcome) && !c.adjudication);
  const rows = r.items.filter((i) => (filter === "differ" ? differs(i) : filter === "open" ? unsettled(i) : true));

  return (
    <>
      <Toolbar
        eyebrow={back}
        title={
          <span className="row" style={{ gap: 12 }}>
            {r.name}
            <Pill tone={tone} live={r.state === "running"}>
              {label}
            </Pill>
          </span>
        }
        description={`CarePlix (${r.version.state} v${r.version.number}) against ${r.incumbent} · ${r.mode === "replay" ? "Replay: an AI plays the other side with the answers " + r.incumbent + " recorded" : "Live calls to each row’s number"}`}
        actions={
          active &&
          canRun && (
            <button className="btn btn-secondary" disabled={cancel.isPending} onClick={() => cancel.mutate()}>
              <Square size={14} aria-hidden /> Cancel run
            </button>
          )
        }
      />
      <div className="content">
        <div className="grid-3" style={{ gridTemplateColumns: "repeat(4, minmax(0, 1fr))" }}>
          <Metric label="Answers that agree" value={pct(s.agreement)} sub={`${s.finished} of ${s.items} rows finished${s.errors ? ` · ${s.errors} couldn’t run` : ""}`} />
          <Metric label="Disagreements" value={String(s.disagreements)} sub={`${s.adjudicated.open} still to settle`} />
          <Metric label="Resolved" value={`${pct(s.resolution.ours)} vs ${pct(s.resolution.theirs)}`} sub={`CarePlix vs ${r.incumbent}`} />
          <Metric label="Median call length" value={`${mins(s.median_duration_s.ours)} vs ${mins(s.median_duration_s.theirs)}`} sub={`CarePlix vs ${r.incumbent}`} />
        </div>

        {s.adjudicated.ours + s.adjudicated.theirs + s.adjudicated.both + s.adjudicated.neither > 0 && (
          <Banner tone="blue" title="Settled disagreements" icon={CheckCircle2}>
            CarePlix was right {times(s.adjudicated.ours)}, {r.incumbent} {times(s.adjudicated.theirs)}. Both were acceptable {times(s.adjudicated.both)}; neither was right {times(s.adjudicated.neither)}.
          </Banner>
        )}

        <Card title="By answer" subtitle="How often each answer matched, among rows where both sides recorded one.">
          <table className="suite-table small" style={{ margin: "0 0 4px" }}>
            <thead>
              <tr>
                <th>Answer</th>
                <th>Agree</th>
                <th>Same</th>
                <th>Different</th>
                <th>Only CarePlix</th>
                <th>Only {r.incumbent}</th>
                <th>Neither</th>
              </tr>
            </thead>
            <tbody>
              {s.fields.map((f) => (
                <tr key={f.field}>
                  <td style={{ fontWeight: 600 }}>{humanize(f.field)}</td>
                  <td className="tabular">{pct(f.agreement)}</td>
                  <td className="tabular">{f.match}</td>
                  <td className="tabular">{f.mismatch}</td>
                  <td className="tabular">{f.ours_only}</td>
                  <td className="tabular">{f.theirs_only}</td>
                  <td className="tabular">{f.neither}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>

        <Card
          title="Rows"
          subtitle="Each row of the worklist, with both sides’ answers."
          actions={
            <Segmented
              label="Filter rows"
              value={filter}
              onChange={setFilter}
              options={[
                { value: "all", label: `All ${r.items.length}` },
                { value: "differ", label: "Answers differ" },
                { value: "open", label: "To settle" },
              ]}
            />
          }
        >
          {rows.length === 0 ? (
            <Empty icon={CheckCircle2} title={filter === "open" ? "Nothing left to settle" : "No rows here"} />
          ) : (
            <div style={{ overflowX: "auto" }}>
              <table className="suite-table small">
                <thead>
                  <tr>
                    <th>Row</th>
                    {r.fields.map((f) => (
                      <th key={f}>{humanize(f)}</th>
                    ))}
                    <th>Call</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((item) => (
                    <ItemRow key={item.id} run={r} item={item} canSettle={canSettle} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </>
  );
}

function Metric({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <div className="card stat">
      <div className="stat-value" style={{ fontSize: 24 }}>
        {value}
      </div>
      <div className="stat-label" style={{ color: "var(--label)", fontWeight: 600 }}>
        {label}
      </div>
      <div className="small muted">{sub}</div>
    </div>
  );
}

function ItemRow({ run, item, canSettle }: { run: ShadowRunDetail; item: ShadowItem; canSettle: boolean }) {
  const qc = useQueryClient();
  const settle = useMutation({
    mutationFn: ({ field, verdict }: { field: string; verdict: ShadowVerdict | null }) =>
      api.post<ShadowRunDetail>(`/shadow-runs/${run.id}/items/${item.id}/adjudicate`, { field, verdict }),
    onSuccess: (r) => qc.setQueryData(["shadow-run", run.id], r),
  });
  const byField = Object.fromEntries((item.comparison ?? []).map((c) => [c.field, c]));
  return (
    <tr>
      <td>
        <div style={{ fontWeight: 600 }} className="mono">
          {item.external_ref}
        </div>
        {item.state !== "done" ? (
          <Pill tone={item.state === "running" ? "blue" : "gray"} icon={CircleDashed} live={item.state === "running"}>
            {item.state === "running" ? "On the call" : item.state === "queued" ? "Waiting" : "Cancelled"}
          </Pill>
        ) : item.error ? (
          <span className="small" style={{ color: "var(--red)" }}>
            {item.error}
          </span>
        ) : null}
      </td>
      {run.fields.map((f) => {
        const c = byField[f];
        const differs = c && ["mismatch", "ours_only", "theirs_only"].includes(c.outcome);
        return (
          <td key={f} style={{ minWidth: 200 }}>
            <div className="stack" style={{ gap: 4 }}>
              <div>
                <span className="muted">{run.incumbent}: </span>
                <span className="mono">{item.theirs[f] || "—"}</span>
              </div>
              <div>
                <span className="muted">CarePlix: </span>
                <span className="mono">{item.state === "done" ? item.ours[f] || "—" : "…"}</span>
              </div>
              {c && (
                <span>
                  <Pill tone={OUTCOME[c.outcome][0]}>{OUTCOME[c.outcome][1]}</Pill>
                </span>
              )}
              {differs &&
                (canSettle ? (
                  <select
                    className="select"
                    style={{ minHeight: 28, fontSize: 13 }}
                    aria-label={`Who was right about ${humanize(f)} for ${item.external_ref}`}
                    value={c.adjudication ?? ""}
                    disabled={settle.isPending}
                    onChange={(e) => settle.mutate({ field: f, verdict: (e.target.value || null) as ShadowVerdict | null })}
                  >
                    <option value="">Who was right?</option>
                    {(Object.keys(VERDICT_LABEL) as ShadowVerdict[]).map((v) => (
                      <option key={v} value={v}>
                        {VERDICT_LABEL[v]}
                      </option>
                    ))}
                  </select>
                ) : c.adjudication ? (
                  <span className="small muted">{VERDICT_LABEL[c.adjudication]}</span>
                ) : null)}
            </div>
          </td>
        );
      })}
      <td>{item.attempt_id ? <Link to={`/calls/${item.attempt_id}`}>Open call</Link> : <span className="muted">—</span>}</td>
    </tr>
  );
}
