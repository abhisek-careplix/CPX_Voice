import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, CircleDashed, CheckCircle2, ChevronRight, PencilLine, PhoneIncoming, PhoneOutgoing, Plus } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type AgentDetail, type AgentSummary, type Template } from "../api";
import { useCan } from "../App";
import { Card, Empty, ErrorState, ListSkeleton, Pill, Sheet, Toolbar, timeAgo } from "../ui";

export default function Agents() {
  const agents = useQuery({ queryKey: ["agents"], queryFn: () => api.get<AgentSummary[]>("/agents") });
  const canEdit = useCan("edit_draft");
  const [creating, setCreating] = useState(false);

  return (
    <>
      <Toolbar
        eyebrow="Build"
        title="Agents"
        description="Each agent is a reusable voice configuration: who it is, what it may do and say, and the version your calls use. Test a draft, pass its regression suite, then publish."
        actions={
          canEdit && (
            <button className="btn btn-primary" onClick={() => setCreating(true)}>
              <Plus size={16} aria-hidden /> New agent
            </button>
          )
        }
      />
      <div className="content">
        <Card>
          {agents.isPending && <ListSkeleton rows={3} />}
          {agents.error && <ErrorState error={agents.error} onRetry={() => agents.refetch()} title="Couldn't load agents" />}
          {agents.data?.length === 0 ? (
            <Empty
              icon={Bot}
              title="No agents yet"
              action={
                canEdit && (
                  <button className="btn btn-primary" onClick={() => setCreating(true)}>
                    <Plus size={16} aria-hidden /> Create your first agent
                  </button>
                )
              }
            >
              An agent starts from a task template, which sets the procedure, the tools it may use and what it may disclose.
            </Empty>
          ) : (
            <ul className="list">
              {agents.data?.map((a) => (
                <li key={a.id}>
                  <Link to={`/agents/${a.id}`} className="list-row" style={{ minHeight: 68 }}>
                    <div className="row-icon" style={{ background: a.template.direction === "inbound" ? "var(--tile-teal)" : "var(--tile-blue)", width: 40, height: 40 }}>
                      {a.template.direction === "inbound" ? <PhoneIncoming size={19} /> : <PhoneOutgoing size={19} />}
                    </div>
                    <div className="row-main">
                      <div className="row-title">{a.name}</div>
                      <div className="row-sub">
                        {a.template.name} · edited {timeAgo(a.updated_at)}
                      </div>
                    </div>
                    <div className="row">
                      {a.draft && (
                        <Pill tone="orange" icon={PencilLine}>
                          Draft v{a.draft.number}
                        </Pill>
                      )}
                      {a.current ? (
                        <Pill tone="green" icon={CheckCircle2}>
                          Published v{a.current.number}
                        </Pill>
                      ) : (
                        <Pill tone="gray" icon={CircleDashed}>
                          Not published
                        </Pill>
                      )}
                    </div>
                    <ChevronRight size={16} className="chevron" aria-hidden />
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
      {creating && <NewAgent onClose={() => setCreating(false)} />}
    </>
  );
}

function NewAgent({ onClose }: { onClose: () => void }) {
  const templates = useQuery({ queryKey: ["templates"], queryFn: () => api.get<Template[]>("/templates") });
  const [name, setName] = useState("");
  const [templateId, setTemplateId] = useState<string | null>(null);
  const navigate = useNavigate();
  const qc = useQueryClient();
  const create = useMutation({
    mutationFn: () => api.post<AgentDetail>("/agents", { name, template_id: templateId }),
    onSuccess: (agent) => {
      qc.invalidateQueries({ queryKey: ["agents"] });
      navigate(`/agents/${agent.id}`);
    },
  });
  return (
    <Sheet
      title="New agent"
      subtitle="Start from a task template. Templates set the procedure, tools and what may be disclosed."
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" disabled={!name || !templateId || create.isPending} onClick={() => create.mutate()}>
            Create draft
          </button>
        </>
      }
    >
      <label className="field">
        <span className="field-label">Agent name</span>
        <input className="input" autoFocus value={name} maxLength={60} placeholder="e.g. Avery" onChange={(e) => setName(e.target.value)} />
        <span className="field-help">Spoken to callers. Never a real person's name.</span>
      </label>
      <div className="field">
        <span className="field-label">Template</span>
        <div className="card" style={{ boxShadow: "none" }} role="group" aria-label="Templates">
          {templates.isPending && <ListSkeleton rows={2} />}
          {templates.data?.map((t) => (
            <button key={t.id} type="button" className="list-row" onClick={() => setTemplateId(t.id)} aria-pressed={templateId === t.id}>
              <div className="row-icon" style={{ background: t.direction === "inbound" ? "var(--tile-teal)" : "var(--tile-blue)" }}>
                {t.direction === "inbound" ? <PhoneIncoming size={17} /> : <PhoneOutgoing size={17} />}
              </div>
              <div className="row-main">
                <div className="row-title">{t.name}</div>
                <div className="row-sub" style={{ whiteSpace: "normal" }}>
                  {t.description}
                </div>
              </div>
            </button>
          ))}
        </div>
      </div>
    </Sheet>
  );
}
