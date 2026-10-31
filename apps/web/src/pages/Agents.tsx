import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, ChevronRight, PhoneIncoming, PhoneOutgoing, Plus } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type AgentDetail, type AgentSummary, type Template } from "../api";
import { useCan } from "../App";
import { Card, Empty, Pill, Sheet, Toolbar, timeAgo } from "../ui";

export default function Agents() {
  const agents = useQuery({ queryKey: ["agents"], queryFn: () => api.get<AgentSummary[]>("/agents") });
  const canEdit = useCan("edit_draft");
  const [creating, setCreating] = useState(false);

  return (
    <>
      <Toolbar
        title="Agents"
        eyebrow="Reusable voice configurations"
        actions={
          canEdit && (
            <button className="btn btn-primary" onClick={() => setCreating(true)}>
              <Plus size={16} aria-hidden /> New Agent
            </button>
          )
        }
      />
      <div className="content">
        <Card>
          {agents.data?.length === 0 ? (
            <Empty icon={Bot} title="No agents yet">
              Create an agent from a task template.
            </Empty>
          ) : (
            <ul className="list">
              {agents.data?.map((a) => (
                <li key={a.id}>
                  <Link to={`/agents/${a.id}`} className="list-row" style={{ minHeight: 68 }}>
                    <div className="row-icon" style={{ background: a.template.direction === "inbound" ? "#30b0c7" : "#0071e3", width: 40, height: 40, borderRadius: 11 }}>
                      {a.template.direction === "inbound" ? <PhoneIncoming size={19} /> : <PhoneOutgoing size={19} />}
                    </div>
                    <div className="row-main">
                      <div className="row-title">{a.name}</div>
                      <div className="row-sub">
                        {a.template.name} · edited {timeAgo(a.updated_at)}
                      </div>
                    </div>
                    <div className="row">
                      {a.current ? <Pill tone="green">Published v{a.current.number}</Pill> : <Pill tone="gray">Not published</Pill>}
                      {a.draft && <Pill tone="orange">Draft v{a.draft.number}</Pill>}
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
      title="New Agent"
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
        <div className="card" style={{ boxShadow: "none" }}>
          {templates.data?.map((t) => (
            <button key={t.id} type="button" className="list-row" onClick={() => setTemplateId(t.id)} aria-pressed={templateId === t.id} style={{ background: templateId === t.id ? "var(--accent-tint)" : undefined }}>
              <div className="row-icon" style={{ background: t.direction === "inbound" ? "#30b0c7" : "#0071e3" }}>
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
