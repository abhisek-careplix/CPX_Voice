import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, Briefcase, ChevronRight, EyeOff, FlaskConical, Phone, Plus, Search, ShieldCheck } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, ApiError, type CaseDetail, type CaseField, type CaseItem, type CaseStatus, type CaseType } from "../api";
import { useCan } from "../App";
import { Banner, Card, Empty, ErrorState, ListSkeleton, Pill, Segmented, Sheet, TaskPill, Toolbar, timeAgo, useToast } from "../ui";

export const CASE_STATUS: Record<CaseStatus, { label: string; tone: "blue" | "orange" | "green" | "gray" }> = {
  open: { label: "Open", tone: "blue" },
  in_progress: { label: "In progress", tone: "orange" },
  done: { label: "Done", tone: "green" },
  closed: { label: "Closed", tone: "gray" },
};

const PACK_NAME: Record<string, string> = {
  "clinical-scope": "Clinical scope",
  "patient-privacy": "Patient privacy",
  "financial-safety": "Financial safety",
  "manipulation-resistance": "Manipulation resistance",
  "outreach-consent": "Outreach consent",
};

export function CaseStatusPill({ status }: { status: CaseStatus }) {
  const s = CASE_STATUS[status];
  return <Pill tone={s.tone}>{s.label}</Pill>;
}

export function useCaseTypes() {
  return useQuery({ queryKey: ["case-types"], queryFn: () => api.get<CaseType[]>("/case-types"), staleTime: 60_000 });
}

export default function Cases() {
  const canStart = useCan("start_task");
  const [params, setParams] = useSearchParams();
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<"all" | CaseStatus>("all");
  const [type, setType] = useState("");
  const [sheet, setSheet] = useState<"new" | "samples" | null>(null);
  const openId = params.get("case");
  const caseTypes = useCaseTypes();
  const qs = new URLSearchParams({ q: query, ...(type ? { case_type: type } : {}), ...(status !== "all" ? { status } : {}) });
  const cases = useQuery({ queryKey: ["cases", qs.toString()], queryFn: () => api.get<CaseItem[]>(`/cases?${qs}`) });
  const open = (id: string | null) => setParams(id ? { case: id } : {}, { replace: true });
  const filtered = query || type || status !== "all";

  return (
    <>
      <Toolbar
        eyebrow="Operate"
        title="Cases"
        description="Each case is one piece of work: a prior auth to chase, a claim to check, a patient to remind. Its type decides which agents can work it, who they call and what they know. Tasks are created from cases."
        actions={
          canStart && (
            <>
              <button className="btn btn-secondary" onClick={() => setSheet("samples")}>
                <FlaskConical size={15} aria-hidden /> Add sample cases
              </button>
              <button className="btn btn-primary" onClick={() => setSheet("new")}>
                <Plus size={16} aria-hidden /> New case
              </button>
            </>
          )
        }
      />
      <div className="content">
        <div className="row" style={{ flexWrap: "wrap", gap: 10 }}>
          <div className="row" style={{ position: "relative", flex: "1 1 240px", minWidth: 0 }}>
            <Search size={16} style={{ position: "absolute", left: 12, color: "var(--label-3)" }} aria-hidden />
            <input className="input" style={{ paddingLeft: 36 }} placeholder="Search by reference, patient, payer or service" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search cases" />
          </div>
          <select className="select" style={{ width: "auto", minWidth: 190 }} value={type} onChange={(e) => setType(e.target.value)} aria-label="Case type">
            <option value="">All case types</option>
            {caseTypes.data?.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
          <Segmented
            label="Status"
            value={status}
            onChange={setStatus}
            options={[{ value: "all", label: "All" }, ...(Object.keys(CASE_STATUS) as CaseStatus[]).map((s) => ({ value: s, label: CASE_STATUS[s].label }))]}
          />
        </div>
        <Card>
          {cases.isPending && <ListSkeleton rows={5} />}
          {cases.error && <ErrorState error={cases.error} onRetry={() => cases.refetch()} title="Couldn't load cases" />}
          {cases.data?.length === 0 &&
            (filtered ? (
              <Empty icon={Search} title="No case matches">
                Try another search, type or status.
              </Empty>
            ) : (
              <Empty
                icon={Briefcase}
                title="No cases yet"
                action={
                  canStart && (
                    <button className="btn btn-primary" onClick={() => setSheet("samples")}>
                      <FlaskConical size={15} aria-hidden /> Add sample cases
                    </button>
                  )
                }
              >
                Cases arrive from your systems, worklists and care programs. Until real data is allowed, add synthetic sample cases to try each kind of work.
              </Empty>
            ))}
          {!!cases.data?.length && (
            <ul className="list">
              {cases.data.map((c) => (
                <li key={c.id}>
                  <button type="button" className="list-row" onClick={() => open(c.id)} style={{ width: "100%", textAlign: "left" }}>
                    <div className="row-main">
                      <div className="row-title">
                        <span className="mono">{c.case_ref}</span> · {c.type_name}
                        {c.headline && <span className="muted"> · {c.headline}</span>}
                      </div>
                      <div className="row-sub">
                        {[c.subject, c.counterparty.name && c.counterparty.name !== c.subject && `${c.counterparty.label}: ${c.counterparty.name}`, c.source_label, c.task_count ? `${c.task_count} task${c.task_count === 1 ? "" : "s"}` : "No tasks yet"].filter(Boolean).join(" · ")}
                      </div>
                    </div>
                    {c.missing.length > 0 && (
                      <Pill tone="orange" icon={AlertCircle}>
                        Missing facts
                      </Pill>
                    )}
                    <CaseStatusPill status={c.status} />
                    <ChevronRight size={16} className="chevron" aria-hidden />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
      {openId && <CaseSheet id={openId} onClose={() => open(null)} />}
      {sheet === "new" && caseTypes.data && <NewCaseSheet types={caseTypes.data} onClose={() => setSheet(null)} onCreated={(id) => { setSheet(null); open(id); }} />}
      {sheet === "samples" && caseTypes.data && <SamplesSheet types={caseTypes.data} onClose={() => setSheet(null)} />}
    </>
  );
}

function CaseSheet({ id, onClose }: { id: string; onClose: () => void }) {
  const canStart = useCan("start_task");
  const navigate = useNavigate();
  const qc = useQueryClient();
  const detail = useQuery({ queryKey: ["case", id], queryFn: () => api.get<CaseDetail>(`/cases/${id}`) });
  const setStatus = useMutation({
    mutationFn: (status: "open" | "closed") => api.post<CaseDetail>(`/cases/${id}/status`, { status }),
    onSuccess: (c) => {
      qc.setQueryData(["case", id], c);
      qc.invalidateQueries({ queryKey: ["cases"] });
    },
  });
  const c = detail.data;
  const plan = c?.plan;
  const ready = plan?.agents.some((a) => a.published);
  return (
    <Sheet
      wide
      title={c ? `${c.case_ref} · ${c.type_name}` : "Case"}
      subtitle={c ? [c.headline, c.program].filter(Boolean).join(" · ") : undefined}
      onClose={onClose}
      footer={
        c &&
        canStart && (
          <>
            {c.status === "closed" ? (
              <button className="btn btn-secondary" onClick={() => setStatus.mutate("open")} disabled={setStatus.isPending}>
                Reopen case
              </button>
            ) : (
              <button className="btn btn-secondary" onClick={() => setStatus.mutate("closed")} disabled={setStatus.isPending}>
                Close case
              </button>
            )}
            <span className="spacer" />
            <button className="btn btn-primary" disabled={c.status === "closed" || c.missing.length > 0} onClick={() => navigate(`/tasks/new?case=${c.id}`)}>
              <Phone size={15} aria-hidden /> Create task
            </button>
          </>
        )
      }
    >
      {detail.isPending && <ListSkeleton rows={4} icon={false} />}
      {detail.error && <ErrorState error={detail.error} onRetry={() => detail.refetch()} title="Couldn't load this case" />}
      {c && plan && (
        <div className="stack" style={{ gap: 22 }}>
          <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
            <CaseStatusPill status={c.status} />
            <span className="chip">{c.source_label}</span>
            {c.synthetic && <span className="sample-label">Synthetic</span>}
            {c.created_at && <span className="small muted">Added {timeAgo(c.created_at)}</span>}
          </div>

          {c.missing.length > 0 && (
            <Banner tone="orange" title="This case is missing facts the call needs" icon={AlertCircle}>
              {" "}
              {c.fields.filter((f) => c.missing.includes(f.key)).map((f) => f.label).join(", ")}.
            </Banner>
          )}

          <section className="stack" style={{ gap: 6 }}>
            <h3 className="field-label">Who the agent calls</h3>
            <div className="row" style={{ gap: 10 }}>
              <Phone size={16} aria-hidden style={{ color: "var(--label-3)" }} />
              <div>
                <div>
                  {plan.destination.label}: {plan.destination.name ?? "not given"}
                </div>
                <div className="small muted">
                  {plan.destination.on_file ? (
                    <>
                      <span className="mono">{plan.destination.on_file}</span> on file
                      {plan.destination.on_file_approved ? " · approved for calls" : " · not an approved test number, so a task calls an approved number in its place"}
                    </>
                  ) : (
                    "No number on file. Choose an approved number when you create the task."
                  )}
                </div>
              </div>
            </div>
          </section>

          <section className="stack" style={{ gap: 6 }}>
            <h3 className="field-label">What the agent is asked to do</h3>
            <p style={{ margin: 0 }}>{plan.objective ?? "The prior-auth agent's own objective: get the authorization's status, reference number and anything the payer still needs."}</p>
          </section>

          <section className="stack" style={{ gap: 6 }}>
            <h3 className="field-label">What the agent knows</h3>
            <FactList fields={c.fields} />
            <p className="small muted" style={{ margin: 0 }}>
              <EyeOff size={12} aria-hidden style={{ verticalAlign: "-1px" }} /> Used to place the call or check identity, never said to the other side.
            </p>
          </section>

          <section className="stack" style={{ gap: 6 }}>
            <h3 className="field-label">Who can work it</h3>
            {plan.agents.length === 0 ? (
              <p className="small muted" style={{ margin: 0 }}>
                No agent in this workspace can work a {c.type_name.toLowerCase()} case yet. <Link to="/agents">Create one on Agents</Link>.
              </p>
            ) : (
              <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
                {plan.agents.map((a) => (
                  <span key={a.id} className="chip">
                    {a.name} {a.published ? `· v${a.version}` : "· not published"}
                  </span>
                ))}
              </div>
            )}
            {!ready && plan.agents.length > 0 && <p className="small muted" style={{ margin: 0 }}>Publish one of these agents before creating a task.</p>}
          </section>

          {(plan.packs.length > 0 || plan.checks.length > 0) && (
            <section className="stack" style={{ gap: 6 }}>
              <h3 className="field-label">Safeguards on every call</h3>
              {plan.packs.length > 0 && (
                <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
                  {plan.packs.map((p) => (
                    <span key={p} className="chip">
                      <ShieldCheck size={12} aria-hidden /> {PACK_NAME[p] ?? p}
                    </span>
                  ))}
                </div>
              )}
              {plan.checks.length > 0 && (
                <ul className="small muted" style={{ margin: 0, paddingLeft: 18 }}>
                  {plan.checks.map((check) => (
                    <li key={check}>{check}</li>
                  ))}
                </ul>
              )}
            </section>
          )}

          <section className="stack" style={{ gap: 6 }}>
            <h3 className="field-label">Tasks</h3>
            {c.tasks.length === 0 ? (
              <p className="small muted" style={{ margin: 0 }}>
                No tasks yet.
              </p>
            ) : (
              <ul className="list card" style={{ boxShadow: "none" }}>
                {c.tasks.map((t) => (
                  <li key={t.id}>
                    <Link to={`/results/${t.id}`} className="list-row">
                      <div className="row-main">
                        <div className="row-title">{t.agent ?? "Agent"}</div>
                        <div className="row-sub">
                          {t.destination ? `to ${t.destination} · ` : ""}
                          {timeAgo(t.created_at)}
                          {t.outcome_reason ? ` · ${t.outcome_reason}` : ""}
                        </div>
                      </div>
                      <TaskPill state={t.state} />
                      <ChevronRight size={16} className="chevron" aria-hidden />
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      )}
    </Sheet>
  );
}

export function FactList({ fields, missing }: { fields: (CaseField & { value: string | number | null })[]; missing?: string[] }) {
  return (
    <ul className="list small">
      {fields.map((f) => {
        const empty = f.value === null || f.value === undefined || f.value === "";
        return (
          <li key={f.key} className="row" style={{ padding: "4px 0", gap: 10, alignItems: "baseline" }}>
            <span className="muted" style={{ width: "38%", maxWidth: 170, flex: "none" }}>
              {f.label}
            </span>
            {empty ? (
              <span style={{ color: f.required || missing?.includes(f.key) ? "var(--orange)" : "var(--label-3)" }}>{f.required ? "Missing" : "Not given"}</span>
            ) : (
              <span className={["code", "phone", "date", "datetime", "number", "money"].includes(f.kind) ? "mono" : undefined} style={{ minWidth: 0, overflowWrap: "anywhere" }}>
                {String(f.value)}
              </span>
            )}
            {!f.share && <EyeOff size={12} aria-label="Not said on the call" style={{ color: "var(--label-3)", flex: "none" }} />}
          </li>
        );
      })}
    </ul>
  );
}

const INPUT_TYPE: Record<CaseField["kind"], string> = { text: "text", date: "date", datetime: "datetime-local", phone: "tel", code: "text", number: "number", money: "text" };

function NewCaseSheet({ types, onClose, onCreated }: { types: CaseType[]; onClose: () => void; onCreated: (id: string) => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [typeId, setTypeId] = useState(types[0]?.id ?? "");
  const [values, setValues] = useState<Record<string, string>>({});
  const type = types.find((t) => t.id === typeId);
  const create = useMutation({
    mutationFn: () => {
      const fields = Object.fromEntries(
        Object.entries(values)
          .filter(([, v]) => v.trim() !== "")
          .map(([k, v]) => [k, type?.fields.find((f) => f.key === k)?.kind === "datetime" ? v.replace("T", " ") : v]),
      );
      return api.post<CaseDetail>("/cases", { case_type: typeId, fields });
    },
    onSuccess: (c) => {
      toast(`Created ${c.case_ref}`);
      qc.invalidateQueries({ queryKey: ["cases"] });
      onCreated(c.id);
    },
  });
  const error = create.error instanceof ApiError ? create.error : null;
  const missing = useMemo(() => new Set<string>((error?.detail as { missing?: string[] } | undefined)?.missing ?? []), [error]);
  return (
    <Sheet
      title="New case"
      subtitle="Enter the facts for one piece of work. The type decides what's needed."
      onClose={onClose}
      footer={
        <>
          <span className="spacer" />
          <button className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" onClick={() => create.mutate()} disabled={!type || create.isPending}>
            {create.isPending ? "Creating…" : "Create case"}
          </button>
        </>
      }
    >
      <form
        className="stack"
        style={{ gap: 16 }}
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <Banner tone="blue" title="Synthetic data only">
          {" "}
          Use made-up people and organisations. A patient's name must include “Synthetic”, “Sample”, “Example” or “Test”.
        </Banner>
        <label className="field">
          <span className="field-label">Case type</span>
          <select
            className="select"
            value={typeId}
            onChange={(e) => {
              setTypeId(e.target.value);
              create.reset();
            }}
          >
            {types.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name} · {t.program}
              </option>
            ))}
          </select>
          {type && <span className="field-help">{type.summary}</span>}
        </label>
        {type?.fields.map((f) => (
          <label key={f.key} className="field">
            <span className="field-label">
              {f.label}
              {!f.required && <span className="muted"> (optional)</span>}
            </span>
            <input
              className="input"
              type={INPUT_TYPE[f.kind]}
              value={values[f.key] ?? ""}
              onChange={(e) => setValues((v) => ({ ...v, [f.key]: e.target.value }))}
              placeholder={f.kind === "phone" ? "+1 202 555 0142" : f.kind === "money" ? "$0.00" : undefined}
              aria-invalid={missing.has(f.key)}
              required={f.required}
            />
            {!f.share && <span className="field-help">Used to place the call or check identity. The agent never says it.</span>}
          </label>
        ))}
        {error && <Banner tone="red" title={error.message} />}
        <button type="submit" hidden />
      </form>
    </Sheet>
  );
}

function SamplesSheet({ types, onClose }: { types: CaseType[]; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [typeId, setTypeId] = useState(types[0]?.id ?? "");
  const [count, setCount] = useState(3);
  const add = useMutation({
    mutationFn: () => api.post<CaseItem[]>("/cases/samples", { case_type: typeId, count }),
    onSuccess: (rows) => {
      toast(`Added ${rows.length} synthetic ${rows.length === 1 ? "case" : "cases"}`);
      qc.invalidateQueries({ queryKey: ["cases"] });
      onClose();
    },
  });
  const type = types.find((t) => t.id === typeId);
  return (
    <Sheet
      title="Add sample cases"
      subtitle="Synthetic cases with made-up patients, payers and 555 phone numbers, for trying each kind of work."
      onClose={onClose}
      footer={
        <>
          <span className="spacer" />
          <button className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" onClick={() => add.mutate()} disabled={add.isPending}>
            {add.isPending ? "Adding…" : `Add ${count}`}
          </button>
        </>
      }
    >
      <div className="stack" style={{ gap: 16 }}>
        <label className="field">
          <span className="field-label">Case type</span>
          <select className="select" value={typeId} onChange={(e) => setTypeId(e.target.value)}>
            {types.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name} · {t.program}
              </option>
            ))}
          </select>
          {type && <span className="field-help">{type.summary} Calls the {type.counterparty.toLowerCase()}.</span>}
        </label>
        <label className="field">
          <span className="field-label">How many</span>
          <select className="select" value={count} onChange={(e) => setCount(Number(e.target.value))}>
            {[1, 3, 5, 10].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        {add.error && <Banner tone="red" title={add.error instanceof ApiError ? add.error.message : "Couldn't add sample cases"} />}
      </div>
    </Sheet>
  );
}
