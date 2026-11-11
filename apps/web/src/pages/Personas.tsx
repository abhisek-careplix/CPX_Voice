import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Library, Plus, Trash2, UserRound } from "lucide-react";
import { useState } from "react";
import { api, ApiError, type Persona, type PersonaLibrary, type PersonaStyle } from "../api";
import { useCan } from "../App";
import { cleanStyle, LANGUAGE_LABEL, PersonaFields } from "../PersonaFields";
import { Banner, Card, Empty, ErrorState, ListSkeleton, Sheet, Toolbar, useToast } from "../ui";

const WARMTH_WORD = { warm: "Warm", neutral: "Even tone", brisk: "Brisk" } as const;
const REGISTER_WORD = { formal: "Formal", neutral: "Plain language", casual: "Casual" } as const;
const PACE_WORD = { slow: "Slow pace", normal: "Normal pace", fast: "Quick pace" } as const;

export default function Personas() {
  const canEdit = useCan("edit_draft");
  const toast = useToast();
  const qc = useQueryClient();
  const library = useQuery({ queryKey: ["personas"], queryFn: () => api.get<PersonaLibrary>("/personas") });
  const [editing, setEditing] = useState<Persona | "new" | null>(null);
  const starters = useMutation({
    mutationFn: () => api.post<{ added: number }>("/personas/starters"),
    onSuccess: (r) => {
      toast(r.added ? `Added ${r.added} starter personas` : "The starter personas are already here");
      qc.invalidateQueries({ queryKey: ["personas"] });
    },
  });
  const personas = library.data?.personas ?? [];

  return (
    <>
      <Toolbar
        eyebrow="Build"
        title="Personas"
        description="How your agents sound: name, voice, greeting, warmth, pace, languages and the words they never use. Apply a persona to an agent on its Voice tab; it goes live with the next published version."
        actions={
          canEdit && (
            <>
              <button className="btn btn-secondary" onClick={() => starters.mutate()} disabled={starters.isPending}>
                <Library size={15} aria-hidden /> Add starter personas
              </button>
              <button className="btn btn-primary" onClick={() => setEditing("new")}>
                <Plus size={16} aria-hidden /> New persona
              </button>
            </>
          )
        }
      />
      <div className="content">
        {library.isPending && (
          <Card>
            <ListSkeleton rows={3} />
          </Card>
        )}
        {library.error && (
          <Card>
            <ErrorState error={library.error} onRetry={() => library.refetch()} title="Couldn't load personas" />
          </Card>
        )}
        {library.data && personas.length === 0 && (
          <Card>
            <Empty
              icon={UserRound}
              title="No personas yet"
              action={
                canEdit && (
                  <button className="btn btn-primary" onClick={() => starters.mutate()}>
                    <Library size={15} aria-hidden /> Add starter personas
                  </button>
                )
              }
            >
              Start from three ready personas (a warm patient coordinator, an efficient payer caller and a bilingual front desk) or create your own.
            </Empty>
          </Card>
        )}
        {personas.length > 0 && (
          <div className="grid-3">
            {personas.map((p) => (
              <button key={p.id} type="button" className="card" onClick={() => setEditing(p)} disabled={!canEdit} style={{ textAlign: "left", cursor: canEdit ? "pointer" : "default", padding: 20, display: "grid", gap: 10, alignContent: "start" }}>
                <div className="row" style={{ gap: 12 }}>
                  <div className="avatar" aria-hidden style={{ width: 40, height: 40, fontSize: 15 }}>
                    {p.agent_name.slice(0, 1)}
                  </div>
                  <div style={{ minWidth: 0 }}>
                    <div className="card-title">{p.name}</div>
                    <div className="small muted">
                      “{p.agent_name}” · {p.voice} voice
                    </div>
                  </div>
                </div>
                {p.description && <p className="small muted" style={{ margin: 0 }}>{p.description}</p>}
                <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
                  <span className="chip">{WARMTH_WORD[p.warmth]}</span>
                  <span className="chip">{REGISTER_WORD[p.formality]}</span>
                  <span className="chip">{PACE_WORD[p.pace]}</span>
                  {p.empathy === "high" && <span className="chip">Extra empathy</span>}
                  {p.languages.map((l) => (
                    <span className="chip" key={l} style={{ background: "var(--accent-tint)", color: "var(--accent-text)" }}>
                      {LANGUAGE_LABEL[l] ?? l}
                    </span>
                  ))}
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
      {editing && library.data && <PersonaSheet persona={editing === "new" ? null : editing} voices={library.data.voices} onClose={() => setEditing(null)} />}
    </>
  );
}

interface Draft extends PersonaStyle {
  name: string;
  description: string;
  agent_name: string;
  role: string;
  voice: string;
  greeting: string;
}

const BLANK: Draft = {
  name: "",
  description: "",
  agent_name: "",
  role: "Assistant",
  voice: "Puck",
  greeting: "",
  warmth: "neutral",
  formality: "neutral",
  pace: "normal",
  empathy: "standard",
  languages: ["en-US"],
  avoid: [],
  signature_phrases: [],
  character: "",
};

export function PersonaSheet({ persona, voices, onClose, initial }: { persona: Persona | null; voices: string[]; onClose: () => void; initial?: Partial<Draft> }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [d, setD] = useState<Draft>(() =>
    persona
      ? { ...persona, description: persona.description ?? "", character: persona.character ?? "" }
      : { ...BLANK, ...initial },
  );
  const [confirmDelete, setConfirmDelete] = useState(false);
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setD({ ...d, [k]: v });
  const save = useMutation({
    mutationFn: () => {
      const body = { ...d, ...cleanStyle(d) };
      return persona ? api.put<Persona>(`/personas/${persona.id}`, body) : api.post<Persona>("/personas", body);
    },
    onSuccess: () => {
      toast(persona ? "Persona saved" : "Persona added to the library");
      qc.invalidateQueries({ queryKey: ["personas"] });
      onClose();
    },
  });
  const remove = useMutation({
    mutationFn: () => api.del(`/personas/${persona!.id}`),
    onSuccess: () => {
      toast("Persona deleted");
      qc.invalidateQueries({ queryKey: ["personas"] });
      onClose();
    },
  });
  const nameMissing = d.agent_name.trim() && d.greeting.trim() && !d.greeting.toLowerCase().includes(d.agent_name.trim().toLowerCase());

  return (
    <Sheet
      wide
      title={persona ? persona.name : "New persona"}
      subtitle="Style only. A persona never changes what an agent may do, share or disclose."
      onClose={onClose}
      footer={
        <>
          {persona &&
            (confirmDelete ? (
              <span className="row" style={{ marginRight: "auto", gap: 8 }}>
                <span className="small">Delete this persona? Agents that use it keep their copy.</span>
                <button className="btn btn-danger" onClick={() => remove.mutate()} disabled={remove.isPending}>
                  Delete
                </button>
                <button className="btn btn-plain" onClick={() => setConfirmDelete(false)}>
                  Keep
                </button>
              </span>
            ) : (
              <button className="btn btn-plain" style={{ marginRight: "auto", color: "var(--red)" }} onClick={() => setConfirmDelete(true)}>
                <Trash2 size={14} aria-hidden /> Delete
              </button>
            ))}
          <button className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" disabled={!d.name.trim() || !d.agent_name.trim() || !d.greeting.trim() || save.isPending} onClick={() => save.mutate()}>
            {save.isPending ? "Saving…" : persona ? "Save persona" : "Add to library"}
          </button>
        </>
      }
    >
      {save.error && <Banner tone="red" title={(save.error as ApiError).message} />}
      <div className="form-section">
        <h3 className="form-section-title">Who it is</h3>
        <div className="grid-2">
          <label className="field">
            <span className="field-label">Persona name</span>
            <input className="input" id="persona-name" value={d.name} maxLength={80} onChange={(e) => set("name", e.target.value)} placeholder="Warm patient coordinator" />
            <span className="field-help">How it appears in the library.</span>
          </label>
          <label className="field">
            <span className="field-label">Short description</span>
            <input className="input" id="persona-description" value={d.description} maxLength={240} onChange={(e) => set("description", e.target.value)} placeholder="Patient reminders and follow-ups" />
          </label>
          <label className="field">
            <span className="field-label">Name the agent uses</span>
            <input className="input" id="persona-agent-name" value={d.agent_name} maxLength={60} onChange={(e) => set("agent_name", e.target.value)} placeholder="Ava" />
            <span className="field-help">Never a real person’s name.</span>
          </label>
          <label className="field">
            <span className="field-label">Role</span>
            <input className="input" id="persona-role" value={d.role} maxLength={120} onChange={(e) => set("role", e.target.value)} />
          </label>
          <label className="field">
            <span className="field-label">Voice</span>
            <select className="select" id="persona-voice" value={d.voice} onChange={(e) => set("voice", e.target.value)}>
              {voices.map((v) => (
                <option key={v}>{v}</option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="field-label">Greeting</span>
            <input className="input" id="persona-greeting" value={d.greeting} maxLength={400} onChange={(e) => set("greeting", e.target.value)} placeholder="Hi, this is Ava, an automated assistant from your care team." />
            {nameMissing ? (
              <span className="field-help" style={{ color: "var(--orange)" }} role="status">
                The greeting doesn’t say “{d.agent_name.trim()}”.
              </span>
            ) : (
              <span className="field-help">The agent opens with these exact words.</span>
            )}
          </label>
        </div>
      </div>
      <div className="form-section">
        <h3 className="form-section-title">How it sounds</h3>
        <PersonaFields idPrefix="persona" value={d} onChange={(style) => setD({ ...d, ...style })} />
      </div>
    </Sheet>
  );
}
