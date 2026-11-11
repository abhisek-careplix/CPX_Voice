import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, AudioLines, Check, ChevronLeft, FlaskConical, Lock, RotateCcw, Rocket, Save, ShieldCheck } from "lucide-react";
import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError, type AgentConfig, type AgentDetail, type CaseItem, type PublishPreview, type Template, type Tool } from "../api";
import { useCan } from "../App";
import { Banner, Card, Pill, Segmented, Sheet, Switch, Toolbar, humanize, timeAgo, useToast } from "../ui";

// LiveKit client code loads only when a test starts.
const BrowserTestSheet = lazy(() => import("../BrowserTest").then((m) => ({ default: m.BrowserTestSheet })));

type Tab = "instructions" | "voice" | "tools" | "guardrails" | "tests";

// Gemini Live voices exposed in the demo (subset of the provider list).
const VOICES = [
  ["Puck", "Upbeat"],
  ["Charon", "Informative"],
  ["Kore", "Firm"],
  ["Fenrir", "Excitable"],
  ["Aoede", "Breezy"],
  ["Leda", "Youthful"],
  ["Orus", "Firm"],
  ["Zephyr", "Bright"],
  ["Sulafat", "Warm"],
  ["Achird", "Friendly"],
] as const;

export default function AgentEditor() {
  const { id = "" } = useParams();
  const qc = useQueryClient();
  const toast = useToast();
  const canEdit = useCan("edit_draft");
  const canPublish = useCan("publish");
  const agent = useQuery({ queryKey: ["agent", id], queryFn: () => api.get<AgentDetail>(`/agents/${id}`) });
  const templates = useQuery({ queryKey: ["templates"], queryFn: () => api.get<Template[]>("/templates") });
  const tools = useQuery({ queryKey: ["tools"], queryFn: () => api.get<Tool[]>("/tools") });
  const [tab, setTab] = useState<Tab>("instructions");
  const [config, setConfig] = useState<AgentConfig | null>(null);
  const [publishing, setPublishing] = useState(false);
  const [testing, setTesting] = useState<{ use: "draft" | "published"; caseId?: string } | null>(null);

  useEffect(() => {
    if (agent.data?.draft_config) setConfig(structuredClone(agent.data.draft_config));
  }, [agent.data?.draft_config]);

  const dirty = useMemo(() => !!config && JSON.stringify(config) !== JSON.stringify(agent.data?.draft_config), [config, agent.data?.draft_config]);
  const template = templates.data?.find((t) => t.id === agent.data?.template.id);

  const save = useMutation({
    mutationFn: () => api.put<AgentDetail>(`/agents/${id}/draft`, { config }),
    onSuccess: (data) => {
      qc.setQueryData(["agent", id], data);
      qc.invalidateQueries({ queryKey: ["agents"] });
      toast(`Draft v${data.draft?.number} saved`);
    },
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "s") {
        e.preventDefault();
        if (dirty && canEdit) save.mutate();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [dirty, canEdit, save]);

  if (!agent.data || !config || !template) {
    return <Toolbar title={<span className="skeleton" style={{ display: "inline-block", width: 220, height: 32 }} />} />;
  }
  const a = agent.data;
  const update = (fn: (c: AgentConfig) => void) =>
    setConfig((c) => {
      const next = structuredClone(c!);
      fn(next);
      return next;
    });

  return (
    <>
      <Toolbar
        eyebrow={
          <>
            <Link to="/agents" className="row" style={{ gap: 2 }}>
              <ChevronLeft size={15} aria-hidden /> Agents
            </Link>
            <span>·</span> {template.name}
          </>
        }
        title={
          <span className="row" style={{ gap: 12 }}>
            {a.name}
            {a.draft ? <Pill tone="orange">Draft v{a.draft.number}</Pill> : null}
            {a.current ? <Pill tone="green" icon={Check}>Published v{a.current.number}</Pill> : <Pill tone="gray">Not published</Pill>}
          </span>
        }
        actions={
          <>
            {canEdit && (
              <button className="btn btn-secondary" disabled={!dirty || save.isPending} onClick={() => save.mutate()} title="Save draft (⌘S)">
                <Save size={15} aria-hidden /> {save.isPending ? "Saving…" : dirty ? "Save Draft" : "Saved"}
              </button>
            )}
            <button className="btn btn-tinted" onClick={() => setTab("tests")}>
              <AudioLines size={15} aria-hidden /> Test
            </button>
            {canPublish && (
              <button className="btn btn-primary" disabled={dirty || !a.draft} onClick={() => setPublishing(true)} title={dirty ? "Save the draft before publishing" : undefined}>
                <Rocket size={15} aria-hidden /> Publish
              </button>
            )}
          </>
        }
      />
      <div className="content">
        <div className="row" style={{ flexWrap: "wrap" }}>
          <Segmented<Tab>
            label="Agent sections"
            value={tab}
            onChange={setTab}
            options={[
              { value: "instructions", label: "Instructions" },
              { value: "voice", label: "Voice" },
              { value: "tools", label: "Tools" },
              { value: "guardrails", label: "Guardrails" },
              { value: "tests", label: "Tests" },
            ]}
          />
          <span className="spacer" />
          {a.draft_issues.length > 0 && !dirty && (
            <Pill tone="orange" icon={AlertTriangle}>
              {a.draft_issues.length} issue{a.draft_issues.length > 1 ? "s" : ""} before publishing
            </Pill>
          )}
          {save.error && <span className="field-error">{(save.error as ApiError).message}</span>}
        </div>

        {a.draft_issues.length > 0 && !dirty && (
          <Banner tone="orange" title="This draft can't be published yet" icon={AlertTriangle}>
            <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
              {a.draft_issues.map((i, n) => (
                <li key={n}>
                  <span className="mono">{i.field}</span> — {i.message}
                </li>
              ))}
            </ul>
          </Banner>
        )}

        <fieldset disabled={!canEdit} style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
          {tab === "instructions" && <InstructionsTab config={config} update={update} template={template} />}
          {tab === "voice" && <VoiceTab config={config} update={update} approvedModel={a.approved_model} />}
          {tab === "tools" && <ToolsTab config={config} update={update} template={template} tools={tools.data ?? []} />}
          {tab === "guardrails" && <GuardrailsTab config={config} update={update} template={template} />}
        </fieldset>
        {tab === "tests" && <TestsTab agent={a} dirty={dirty} onTest={setTesting} />}
      </div>
      {publishing && <PublishSheet agentId={id} onClose={() => setPublishing(false)} />}
      {testing && (
        <Suspense fallback={null}>
          <BrowserTestSheet agentId={id} use={testing.use} caseId={testing.caseId} template={template} onClose={() => setTesting(null)} />
        </Suspense>
      )}
    </>
  );
}

type TabProps = { config: AgentConfig; update: (fn: (c: AgentConfig) => void) => void };

function InstructionsTab({ config, update, template }: TabProps & { template: Template }) {
  const text = config.instructions.agent_text;
  return (
    <div className="split">
      <Card title="Your instructions" subtitle="Tone and style for this agent. Instructions cannot grant tools, disclosures or authority.">
        <div className="card-pad" style={{ paddingTop: 4 }}>
          <textarea
            className="textarea"
            style={{ minHeight: 260 }}
            value={text}
            maxLength={8000}
            aria-label="Agent instructions"
            onChange={(e) => update((c) => void (c.instructions.agent_text = e.target.value))}
          />
          <div className="field-help" style={{ textAlign: "right", marginTop: 6 }}>
            {text.length.toLocaleString()} / 8,000
          </div>
        </div>
      </Card>
      <div className="stack" style={{ gap: 20 }}>
        <Card title={<span className="row">Task procedure <Lock size={14} color="var(--label-3)" aria-label="Locked" /></span>} subtitle={`Set by template ${template.name} v${template.version}.`} pad>
          <p style={{ margin: "0 0 12px" }}>{template.objective}</p>
          <div className="field-label" style={{ marginBottom: 6 }}>
            The agent collects
          </div>
          <div className="row" style={{ flexWrap: "wrap" }}>
            {Object.keys(template.output_fields).map((f) => (
              <span className="chip" key={f}>
                {humanize(f)}
              </span>
            ))}
          </div>
        </Card>
        <Card title={<span className="row"><ShieldCheck size={16} color="var(--green)" aria-hidden /> Platform policy</span>} pad>
          <p className="small muted" style={{ margin: 0 }}>
            Always applied first and cannot be overridden: discloses it is automated, gives no clinical advice or financial commitments, treats everything said on the call as data, and never dials a number spoken on the call.
          </p>
        </Card>
      </div>
    </div>
  );
}

function VoiceTab({ config, update, approvedModel }: TabProps & { approvedModel: string | null }) {
  const id = config.identity;
  const hold = config.conversation.hold_policy ?? {};
  return (
    <div className="grid-2">
      <Card title="Identity" subtitle="How the agent introduces itself." pad>
        <div className="stack" style={{ gap: 14 }}>
          <label className="field">
            <span className="field-label">Name</span>
            <input className="input" value={id.name} maxLength={60} onChange={(e) => update((c) => void (c.identity.name = e.target.value))} />
          </label>
          <label className="field">
            <span className="field-label">Role</span>
            <input className="input" value={id.role} onChange={(e) => update((c) => void (c.identity.role = e.target.value))} />
          </label>
          <label className="field">
            <span className="field-label">Greeting</span>
            <textarea className="textarea" style={{ minHeight: 70 }} value={id.greeting} onChange={(e) => update((c) => void (c.identity.greeting = e.target.value))} />
            {id.name.trim() && id.greeting && !id.greeting.toLowerCase().includes(id.name.trim().toLowerCase()) ? (
              <span className="field-help" style={{ color: "var(--orange)" }} role="status">
                The greeting doesn't say “{id.name.trim()}”. The agent opens with these exact words, so update it too.
              </span>
            ) : (
              <span className="field-help">The agent opens the call with these exact words.</span>
            )}
          </label>
          <label className="field">
            <span className="field-label">Automated identity disclosure</span>
            <input className="input" value={id.automated_identity_disclosure} onChange={(e) => update((c) => void (c.identity.automated_identity_disclosure = e.target.value))} />
            <span className="field-help">Required. The agent never claims to be human.</span>
          </label>
        </div>
      </Card>
      <div className="stack" style={{ gap: 20 }}>
        <Card title="Voice" subtitle="Gemini Live native audio voices.">
          <div className="card-pad" style={{ paddingTop: 4, display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(120px,1fr))", gap: 8 }}>
            {VOICES.map(([name, style]) => (
              <button
                key={name}
                type="button"
                className="card"
                aria-pressed={config.conversation.voice === name}
                onClick={() => update((c) => void (c.conversation.voice = name))}
                style={{ padding: "10px 12px", textAlign: "left", cursor: "pointer", boxShadow: "none", borderColor: config.conversation.voice === name ? "var(--accent)" : undefined, background: config.conversation.voice === name ? "var(--accent-tint)" : undefined }}
              >
                <div style={{ fontWeight: 600 }}>{name}</div>
                <div className="small muted">{style}</div>
              </button>
            ))}
          </div>
        </Card>
        <Card title="Conversation" pad>
          <div className="stack" style={{ gap: 14 }}>
            <div className="field">
              <span className="field-label">On hold</span>
              <Segmented
                label="Hold behaviour"
                value={hold.mode ?? "suspend_model"}
                onChange={(v) => update((c) => void (c.conversation.hold_policy = { ...c.conversation.hold_policy, mode: v }))}
                options={[
                  { value: "suspend_model", label: "Pause model, wait for a person" },
                  { value: "keep_model", label: "Keep listening" },
                ]}
              />
              <span className="field-help">Pausing saves cost and avoids model session limits on long holds.</span>
            </div>
            <label className="field">
              <span className="field-label">Silence timeout · {config.conversation.silence_timeout_s ?? 30}s</span>
              <input type="range" min={5} max={120} value={config.conversation.silence_timeout_s ?? 30} onChange={(e) => update((c) => void (c.conversation.silence_timeout_s = Number(e.target.value)))} />
            </label>
            <div className="field">
              <span className="field-label">Model</span>
              <div className="row">
                <span className="chip mono">{config.model.model_id || "not set"}</span>
                {approvedModel && config.model.model_id !== approvedModel && (
                  <button type="button" className="btn btn-plain small" onClick={() => update((c) => void (c.model.model_id = approvedModel))}>
                    Use approved model
                  </button>
                )}
              </div>
              <span className="field-help">Only the workspace's approved Gemini Live model can be published.</span>
            </div>
          </div>
        </Card>
      </div>
    </div>
  );
}

const CLASS_LABEL = { read: "Reads data", write: "Writes data", call_control: "Controls the call" };

function ToolsTab({ config, update, template, tools }: TabProps & { template: Template; tools: Tool[] }) {
  const enabled = new Set(config.tools.map((t) => t.name));
  return (
    <Card title="Tools" subtitle="Every tool call is authorised by the CarePlix server. Turning a tool off removes it from new calls.">
      <ul className="list">
        {tools.map((tool) => {
          const permitted = template.tools.includes(tool.name);
          const available = tool.enabled_in_demo;
          return (
            <li className="list-row" key={tool.name} style={{ alignItems: "flex-start", paddingTop: 14, paddingBottom: 14 }}>
              <div className="row-main">
                <div className="row" style={{ flexWrap: "wrap" }}>
                  <span className="mono" style={{ fontWeight: 600 }}>
                    {tool.name}
                  </span>
                  <span className="chip">{CLASS_LABEL[tool.classification]}</span>
                  <span className="small muted">v{tool.version}</span>
                </div>
                <div className="small muted" style={{ whiteSpace: "normal", marginTop: 4 }}>
                  {tool.description}
                </div>
                {!available && <div className="small" style={{ color: "var(--orange)", marginTop: 4 }}>Available from the supervised pilot.</div>}
                {available && !permitted && <div className="small" style={{ color: "var(--label-3)", marginTop: 4 }}>Not permitted by the {template.name} template.</div>}
              </div>
              <Switch
                label={`Enable ${tool.name}`}
                checked={enabled.has(tool.name)}
                disabled={!available || !permitted}
                onChange={(on) =>
                  update((c) => {
                    c.tools = on ? [...c.tools, { name: tool.name, version: tool.version }] : c.tools.filter((t) => t.name !== tool.name);
                  })
                }
              />
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

function GuardrailsTab({ config, update, template }: TabProps & { template: Template }) {
  const allowed = new Set(config.guardrails.allowed_disclosures ?? []);
  const [trigger, setTrigger] = useState("");
  const triggers = config.guardrails.escalation_triggers ?? [];
  return (
    <div className="grid-2">
      <Card title="May disclose" subtitle="Case details the agent may say on a call. Limited by the template.">
        {template.disclose_allowlist.map((field) => (
          <label className="checkbox-row" key={field}>
            <input
              type="checkbox"
              checked={allowed.has(field)}
              onChange={(e) =>
                update((c) => {
                  const set = new Set(c.guardrails.allowed_disclosures ?? []);
                  if (e.target.checked) set.add(field);
                  else set.delete(field);
                  c.guardrails.allowed_disclosures = template.disclose_allowlist.filter((f) => set.has(f));
                })
              }
            />
            {humanize(field)}
          </label>
        ))}
      </Card>
      <div className="stack" style={{ gap: 20 }}>
        <Card title="Escalate when" subtitle="Situations that stop the task and ask for human review." pad>
          <div className="row" style={{ flexWrap: "wrap", marginBottom: 10 }}>
            {triggers.map((t) => (
              <span className="chip" key={t}>
                {t}
                <button type="button" className="btn-plain" style={{ border: 0, background: "none", cursor: "pointer", padding: 0, color: "var(--label-3)" }} aria-label={`Remove ${t}`} onClick={() => update((c) => void (c.guardrails.escalation_triggers = triggers.filter((x) => x !== t)))}>
                  ✕
                </button>
              </span>
            ))}
          </div>
          <form
            className="row"
            onSubmit={(e) => {
              e.preventDefault();
              if (trigger.trim()) update((c) => void (c.guardrails.escalation_triggers = [...triggers, trigger.trim()]));
              setTrigger("");
            }}
          >
            <input className="input" placeholder="Add a trigger" value={trigger} onChange={(e) => setTrigger(e.target.value)} />
            <button className="btn btn-secondary" type="submit">
              Add
            </button>
          </form>
        </Card>
        <Card title="Limits" pad>
          <label className="field">
            <span className="field-label">Maximum call length · {Math.round(config.guardrails.max_duration_s / 60)} min</span>
            <input type="range" min={60} max={900} step={30} value={config.guardrails.max_duration_s} onChange={(e) => update((c) => void (c.guardrails.max_duration_s = Number(e.target.value)))} />
            <span className="field-help">The call ends automatically at this limit, including hold time.</span>
          </label>
        </Card>
      </div>
    </div>
  );
}

function TestsTab({ agent, dirty, onTest }: { agent: AgentDetail; dirty: boolean; onTest: (t: { use: "draft" | "published"; caseId?: string }) => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const canTest = useCan("browser_test");
  const canPublish = useCan("publish");
  const cases = useQuery({ queryKey: ["cases"], queryFn: () => api.get<CaseItem[]>("/cases") });
  const [use, setUse] = useState<"draft" | "published">(agent.draft ? "draft" : "published");
  const [caseId, setCaseId] = useState("");
  const rollback = useMutation({
    mutationFn: (versionId: string) => api.post<AgentDetail>(`/agents/${agent.id}/rollback`, { version_id: versionId }),
    onSuccess: (data) => {
      qc.setQueryData(["agent", agent.id], data);
      toast(`v${data.current?.number} is now active for new calls`);
    },
  });
  const outbound = agent.template.direction === "outbound";
  const testable = use === "draft" ? !!agent.draft : !!agent.current;
  const chosen = cases.data?.find((c) => c.id === caseId);
  const actingFor = chosen?.context.provider_name ? String(chosen.context.provider_name) : null;

  return (
    <div className="split">
      <div className="stack" style={{ gap: 20 }}>
        <Card title="Browser Test" subtitle="Talk to the agent through your microphone. Uses LiveKit and Gemini Live for real; runs as a sandbox call with synthetic data." pad>
          <div className="stack" style={{ gap: 14 }}>
            <div className="field">
              <span className="field-label">Configuration</span>
              <Segmented
                label="Configuration to test"
                value={use}
                onChange={setUse}
                options={[
                  { value: "draft", label: `Draft${agent.draft ? ` v${agent.draft.number}` : ""}` },
                  { value: "published", label: `Published${agent.current ? ` v${agent.current.number}` : ""}` },
                ]}
              />
              {use === "draft" && dirty && <span className="field-help" style={{ color: "var(--orange)" }}>You have unsaved changes. The test uses the last saved draft.</span>}
            </div>
            {outbound && (
              <label className="field">
                <span className="field-label">Synthetic case</span>
                <select className="select" value={caseId} onChange={(e) => setCaseId(e.target.value)}>
                  <option value="">No case: no patient or provider details</option>
                  {cases.data?.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.case_ref} · {String(c.context.provider_name)} · {String(c.context.service_description)} · {String(c.context.payer_name)}
                    </option>
                  ))}
                </select>
                <span className="field-help">
                  The agent calls for <strong>{actingFor ?? "the provider's office"}</strong>
                  {actingFor ? " and sees only this case's allowed fields." : " and has no case details."} You play the other side.
                </span>
              </label>
            )}
            <div>
              <button className="btn btn-primary btn-lg" disabled={!canTest || !testable} onClick={() => onTest({ use, caseId: caseId || undefined })}>
                <AudioLines size={18} aria-hidden /> Start Browser Test
              </button>
              {!testable && <div className="field-help" style={{ marginTop: 6 }}>{use === "published" ? "Publish a version first." : "No draft to test."}</div>}
            </div>
          </div>
        </Card>
        <Card title={<span className="row"><FlaskConical size={16} aria-hidden /> Scenario evaluations</span>} pad>
          <p className="small muted" style={{ margin: 0 }}>
            The synthetic scenario suite (IVR, hold, voicemail, injection and transfer-failure scripts) runs against the exact candidate before publishing once the counterpart simulator ships. Until then, publishing shows these areas as untested.
          </p>
        </Card>
      </div>
      <Card title="Version history" subtitle="Published versions are immutable. Activating one changes new calls only.">
        {agent.versions.length === 0 ? (
          <div className="empty small">Nothing published yet.</div>
        ) : (
          <ul className="list">
            {agent.versions.map((v) => (
              <li className="list-row" key={v.id} style={{ alignItems: "flex-start" }}>
                <div className="row-main">
                  <div className="row-title">
                    Version {v.number} {agent.current?.id === v.id && <Pill tone="green">Active</Pill>}
                  </div>
                  <div className="row-sub" style={{ whiteSpace: "normal" }}>
                    {v.change_note ?? "No change note"} · {timeAgo(v.published_at)}
                  </div>
                  <div className="mono muted" style={{ fontSize: 11 }}>
                    {v.config_hash?.slice(0, 19)}…
                  </div>
                </div>
                {canPublish && agent.current?.id !== v.id && (
                  <button className="btn btn-secondary" onClick={() => rollback.mutate(v.id)} disabled={rollback.isPending}>
                    <RotateCcw size={14} aria-hidden /> Make active
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

function PublishSheet({ agentId, onClose }: { agentId: string; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const preview = useQuery({ queryKey: ["publish-preview", agentId], queryFn: () => api.get<PublishPreview>(`/agents/${agentId}/publish-preview`), gcTime: 0 });
  const [note, setNote] = useState("");
  const publish = useMutation({
    mutationFn: () => api.post<AgentDetail>(`/agents/${agentId}/publish`, { change_note: note }),
    onSuccess: (data) => {
      qc.setQueryData(["agent", agentId], data);
      qc.invalidateQueries({ queryKey: ["agents"] });
      toast(`Version ${data.current?.number} published`);
      onClose();
    },
  });
  const p = preview.data;
  const priv = p?.privilege_changes;
  const privilegeChanged = priv && (priv.tools_added.length || priv.disclosures_added.length);
  const blocked = !p || p.issues.length > 0;

  return (
    <Sheet
      title={p ? `Publish version ${p.draft_version}` : "Publish"}
      subtitle={p?.current_version ? `Replaces version ${p.current_version} for new calls. Calls in progress keep their version.` : "First published version of this agent."}
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" disabled={blocked || publish.isPending} onClick={() => publish.mutate()}>
            <Rocket size={15} aria-hidden /> {publish.isPending ? "Publishing…" : "Publish"}
          </button>
        </>
      }
    >
      {!p && <div className="skeleton" style={{ height: 120 }} />}
      {p && p.issues.length > 0 && (
        <Banner tone="red" title="Fix these before publishing" icon={AlertTriangle}>
          <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
            {p.issues.map((i, n) => (
              <li key={n}>{i.message}</li>
            ))}
          </ul>
        </Banner>
      )}
      {publish.error && <Banner tone="red" title={(publish.error as ApiError).message} />}
      {p && privilegeChanged ? (
        <Banner tone="orange" title="This version increases what the agent can do" icon={ShieldCheck}>
          {priv!.tools_added.length > 0 && ` Tools added: ${priv!.tools_added.join(", ")}.`}
          {priv!.disclosures_added.length > 0 && ` New disclosures: ${priv!.disclosures_added.map(humanize).join(", ")}.`}
        </Banner>
      ) : null}
      {p && (
        <div>
          <div className="field-label">Changes</div>
          {p.changes.length === 0 ? (
            <div className="small muted" style={{ paddingTop: 6 }}>
              No configuration changes.
            </div>
          ) : (
            p.changes.slice(0, 30).map((c) => (
              <div className="diff-row" key={c.path}>
                <span className="mono muted">{c.path}</span>
                <span>
                  {c.before !== undefined && c.before !== null && <span className="diff-before">{short(c.before)}</span>} <span className="diff-after">{short(c.after)}</span>
                </span>
              </div>
            ))
          )}
        </div>
      )}
      {p && (
        <div className="grid-2">
          <div>
            <div className="field-label">Affected routes</div>
            <div className="small" style={{ paddingTop: 6 }}>
              {p.affected_routes.length ? p.affected_routes.map((r) => `${r.name} (${r.number})`).join(", ") : <span className="muted">No phone numbers route to this agent.</span>}
            </div>
          </div>
          <div>
            <div className="field-label">Untested</div>
            <ul className="small" style={{ margin: "6px 0 0", paddingLeft: 18, color: "var(--orange)" }}>
              {p.evaluation.untested.map((u) => (
                <li key={u}>{u}</li>
              ))}
            </ul>
          </div>
        </div>
      )}
      <label className="field">
        <span className="field-label">What changed and why</span>
        <textarea className="textarea" style={{ minHeight: 80 }} value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} placeholder="Recorded with the version for audit." />
      </label>
    </Sheet>
  );
}

function short(v: unknown): string {
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return s && s.length > 90 ? `${s.slice(0, 90)}…` : (s ?? "—");
}
