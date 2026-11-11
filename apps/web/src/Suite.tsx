import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, ChevronDown, ChevronRight, CircleDashed, FlaskConical, Play, Square, XCircle } from "lucide-react";
import { Fragment, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type AgentDetail, type AgentSuite, type SuiteResult, type SuiteRun, type SuiteScenarioResult } from "./api";
import { useCan } from "./App";
import { Banner, Card, Pill, Segmented, Switch, timeAgo } from "./ui";

type Tone = "green" | "orange" | "red" | "blue" | "purple" | "gray";

const RUN_STATE: Record<SuiteRun["state"], [Tone, string]> = {
  queued: ["blue", "Queued"],
  running: ["blue", "Running"],
  passed: ["green", "Passed"],
  failed: ["red", "Failed"],
  stale: ["orange", "Draft changed"],
  cancelled: ["gray", "Cancelled"],
};

const SCENARIO_STATUS: Record<SuiteScenarioResult["status"], [Tone, string]> = {
  pass: ["green", "Pass"],
  flaky: ["orange", "Flaky"],
  fail: ["red", "Fail"],
  error: ["purple", "Couldn't run"],
};

export function suiteLabel(result: SuiteResult | null | undefined): [Tone, string] {
  switch (result?.status) {
    case "passed":
      return ["green", `Suite passed ${result.passed}/${result.total}`];
    case "failed":
      return ["red", `Suite failed ${result.passed}/${result.total}`];
    case "outdated":
      return ["orange", "Suite result outdated"];
    default:
      return ["gray", "Suite not run"];
  }
}

const active = (r: SuiteRun) => r.state === "queued" || r.state === "running";

/** Pre-publish regression suite: simulate a version against its scenarios, with a baseline. */
export function RegressionSuite({ agent }: { agent: AgentDetail }) {
  const qc = useQueryClient();
  const canRun = useCan("browser_test");
  const suite = useQuery({
    queryKey: ["suite", agent.id],
    queryFn: () => api.get<AgentSuite>(`/agents/${agent.id}/suite`),
    refetchInterval: (q) => (q.state.data?.runs.some(active) ? 5000 : false),
  });
  const [use, setUse] = useState<"draft" | "published">(agent.draft ? "draft" : "published");
  const [repeats, setRepeats] = useState(3);
  const [compare, setCompare] = useState(true);
  const [open, setOpen] = useState<string | null>(null);
  const start = useMutation({
    mutationFn: () => api.post<SuiteRun>(`/agents/${agent.id}/suite/runs`, { use, repeats, compare }),
    onSuccess: (run) => {
      setOpen(run.id);
      qc.invalidateQueries({ queryKey: ["suite", agent.id] });
    },
  });

  const s = suite.data;
  const scenarioCount = s?.scenarios.length ?? 0;
  const withBaseline = compare && use === "draft" && !!agent.current;
  const simulations = scenarioCount * repeats * (withBaseline ? 2 : 1);
  const version = use === "draft" ? agent.draft : agent.current;
  const [tone, label] = suiteLabel(use === "draft" ? s?.draft : s?.published);
  const shown = open ?? s?.runs[0]?.id ?? null;

  return (
    <Card
      title={
        <span className="row">
          <FlaskConical size={16} aria-hidden /> Regression suite
        </span>
      }
      subtitle="Simulates this agent against its Voice Studio scenarios. An AI plays the other side; every run is checked by the platform's pass rules and the AI judge."
      actions={version ? <Pill tone={tone} icon={tone === "green" ? CheckCircle2 : tone === "red" ? XCircle : CircleDashed}>{label}</Pill> : null}
    >
      <div className="card-pad stack" style={{ gap: 14 }}>
        {suite.error && <Banner tone="red" title={(suite.error as ApiError).message} />}
        {s?.required_to_publish && (
          <Banner tone="blue" title="Publishing needs a passed suite on the exact draft">
            An admin can publish anyway with a reason, recorded in the audit log.
          </Banner>
        )}
        {s && scenarioCount === 0 ? (
          <div className="subcard small">
            No scenarios use this agent yet. In <Link to="/studio">Voice Studio</Link>, add scenarios (or the example library) and choose <strong>{agent.name}</strong> under “Agent that calls”.
          </div>
        ) : (
          <>
            <div className="row" style={{ gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
              <div className="field">
                <span className="field-label">Version</span>
                <Segmented
                  label="Version to evaluate"
                  value={use}
                  onChange={setUse}
                  options={[
                    { value: "draft", label: `Draft${agent.draft ? ` v${agent.draft.number}` : ""}` },
                    { value: "published", label: `Published${agent.current ? ` v${agent.current.number}` : ""}` },
                  ]}
                />
              </div>
              <label className="field" style={{ width: 120 }}>
                <span className="field-label">Runs per scenario</span>
                <select className="select" value={repeats} onChange={(e) => setRepeats(Number(e.target.value))}>
                  {Array.from({ length: s?.max_repeats ?? 5 }, (_, i) => i + 1).map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                </select>
              </label>
              {use === "draft" && agent.current && (
                <label className="row small" style={{ gap: 8, paddingBottom: 8 }}>
                  <Switch checked={compare} onChange={setCompare} label="Compare with the published version" /> Compare with published v{agent.current.number}
                </label>
              )}
              <span className="spacer" />
              <button className="btn btn-primary" disabled={!canRun || !version || !scenarioCount || start.isPending} onClick={() => start.mutate()}>
                <Play size={15} aria-hidden /> Run suite
              </button>
            </div>
            <div className="small muted">
              {scenarioCount} scenario{scenarioCount === 1 ? "" : "s"} × {repeats} run{repeats === 1 ? "" : "s"}
              {withBaseline ? " × 2 versions" : ""} = {simulations} simulations. A scenario passes when {Math.ceil((repeats * 2) / 3)} of {repeats} runs pass.
            </div>
            {start.error && <Banner tone="red" title={(start.error as ApiError).message} />}
          </>
        )}

        {s && s.runs.length > 0 && (
          <div className="stack" style={{ gap: 6 }}>
            <div className="field-label">Recent runs</div>
            {s.runs.map((r) => {
              const [rt, rl] = RUN_STATE[r.state];
              return (
                <button key={r.id} type="button" className={`list-row${shown === r.id ? " selected" : ""}`} style={{ textAlign: "left", minHeight: 44, borderRadius: "var(--r-sm)" }} aria-expanded={shown === r.id} onClick={() => setOpen(shown === r.id ? null : r.id)}>
                  {shown === r.id ? <ChevronDown size={14} aria-hidden /> : <ChevronRight size={14} aria-hidden />}
                  <span className="row-main">
                    {r.version.state === "draft" ? "Draft" : "Published"} v{r.version.number}
                    {r.baseline_version ? ` vs published v${r.baseline_version}` : ""} · {r.scenarios.length} scenarios × {r.repeats}
                  </span>
                  <Pill tone={rt} live={active(r)}>
                    {rl}
                  </Pill>
                  <span className="small muted">{timeAgo(r.created_at)}</span>
                </button>
              );
            })}
          </div>
        )}
        {shown && <RunDetail runId={shown} agentId={agent.id} />}
      </div>
    </Card>
  );
}

function RunDetail({ runId, agentId }: { runId: string; agentId: string }) {
  const qc = useQueryClient();
  const canRun = useCan("browser_test");
  const run = useQuery({
    queryKey: ["suite-run", runId],
    queryFn: () => api.get<SuiteRun>(`/suite-runs/${runId}`),
    refetchInterval: (q) => (q.state.data && active(q.state.data) ? 4000 : false),
  });
  const cancel = useMutation({
    mutationFn: () => api.post<SuiteRun>(`/suite-runs/${runId}/cancel`),
    onSuccess: (r) => {
      qc.setQueryData(["suite-run", runId], r);
      qc.invalidateQueries({ queryKey: ["suite", agentId] });
    },
  });
  const [openScenario, setOpenScenario] = useState<string | null>(null);
  const r = run.data;
  if (!r) return <div className="skeleton" style={{ height: 80 }} />;
  const results = r.live_summary ?? r.summary ?? [];
  const candidate = results.filter((x) => x.role === "candidate");
  const baseline = new Map(results.filter((x) => x.role === "baseline").map((x) => [x.scenario_id, x]));

  return (
    <div className="stack" style={{ gap: 10, borderTop: "1px solid var(--separator)", paddingTop: 12 }}>
      <div className="row" style={{ gap: 10 }}>
        <span className="small">
          {r.progress ? `${r.progress.done} of ${r.progress.total} simulations finished` : ""}
          {r.finished_at ? ` · finished ${timeAgo(r.finished_at)}` : ""}
        </span>
        <span className="spacer" />
        {active(r) && canRun && (
          <button className="btn btn-secondary" disabled={cancel.isPending} onClick={() => cancel.mutate()}>
            <Square size={14} aria-hidden /> Cancel run
          </button>
        )}
      </div>
      {r.error && <Banner tone="orange" title={r.error} />}
      <table className="suite-table small">
        <thead>
          <tr>
            <th>Scenario</th>
            <th>{r.version.state === "draft" ? "Draft" : "Published"} v{r.version.number}</th>
            {r.baseline_version && <th>Published v{r.baseline_version}</th>}
          </tr>
        </thead>
        <tbody>
          {candidate.map((c) => {
            const b = baseline.get(c.scenario_id);
            const isOpen = openScenario === c.scenario_id;
            const regressed = b && b.status === "pass" && c.status !== "pass";
            return (
              <Fragment key={c.scenario_id}>
                <tr>
                  <td>
                    <span className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                    <button type="button" className="disclosure" aria-expanded={isOpen} onClick={() => setOpenScenario(isOpen ? null : c.scenario_id)}>
                      {isOpen ? <ChevronDown size={12} aria-hidden /> : <ChevronRight size={12} aria-hidden />} {c.name}
                    </button>
                    {regressed && (
                      <Pill tone="red" icon={XCircle}>
                        Regression
                      </Pill>
                    )}
                    </span>
                  </td>
                  <td>
                    <ScenarioCell result={c} />
                  </td>
                  {r.baseline_version && <td>{b ? <ScenarioCell result={b} /> : "—"}</td>}
                </tr>
                {isOpen && (
                  <tr>
                    <td colSpan={r.baseline_version ? 3 : 2}>
                      <CaseList run={r} scenarioId={c.scenario_id} />
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ScenarioCell({ result }: { result: SuiteScenarioResult }) {
  const [tone, label] = SCENARIO_STATUS[result.status];
  return (
    <span className="row" style={{ gap: 6 }}>
      <Pill tone={tone}>{label}</Pill>
      <span className="muted tabular">
        {result.passes}/{result.runs} passed (needs {result.required})
      </span>
    </span>
  );
}

function CaseList({ run, scenarioId }: { run: SuiteRun; scenarioId: string }) {
  const cases = (run.cases ?? []).filter((c) => c.scenario_id === scenarioId);
  return (
    <ul className="stack" style={{ gap: 8, margin: 0, padding: 0, listStyle: "none" }}>
      {cases.map((c) => {
        const failed = c.rules.filter((x) => !x.ok);
        const Icon = c.verdict === "pass" ? CheckCircle2 : c.verdict ? XCircle : CircleDashed;
        return (
          <li key={c.id} className="stack" style={{ gap: 2 }}>
            <div className="row" style={{ gap: 6 }}>
              <Icon size={14} aria-hidden style={{ color: c.verdict === "pass" ? "var(--green)" : c.verdict ? "var(--red)" : "var(--label-2)" }} />
              <strong>
                {c.role === "baseline" ? "Published" : "Candidate"} · run {c.repeat}
              </strong>
              <span className="muted">{c.verdict ?? c.state}</span>
              {c.attempt_id && <Link to={`/calls/${c.attempt_id}`}>Open call</Link>}
            </div>
            {failed.map((x) => (
              <div key={x.rule} className="row muted" style={{ gap: 6, alignItems: "flex-start" }}>
                <XCircle size={13} aria-hidden style={{ color: "var(--red)", marginTop: 2, flex: "none" }} /> {x.rule}: {x.detail}
              </div>
            ))}
            {c.judge?.state === "done" && (
              <div className="muted">
                Judge: {c.judge.verdict} ({c.judge.score}/100){c.judge.summary ? ` · ${c.judge.summary}` : ""}
              </div>
            )}
            {c.error && <div style={{ color: "var(--orange)" }}>{c.error}</div>}
          </li>
        );
      })}
    </ul>
  );
}
