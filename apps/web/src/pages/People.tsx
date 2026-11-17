import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, CalendarClock, ChevronRight, FlaskConical, Link2, Plus, Search, ShieldCheck, Trash2, UserRound, Users } from "lucide-react";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api";
import { useCan } from "../App";
import { Banner, Card, Empty, ErrorState, ListSkeleton, Pill, Segmented, Sheet, Switch, Toolbar, humanize, timeAgo, useToast } from "../ui";

// ---------- Types (mirror apps/api people views; kept here so other workstreams' api.ts edits don't collide) ----------

type Kind = "patient" | "client" | "family" | "caregiver" | "staff" | "payer_contact";
type Channel = "voice" | "sms" | "email";
interface Window {
  days: number[];
  start: string;
  end: string;
}
interface Option {
  id: string;
  label: string;
}
interface Options {
  kinds: Option[];
  topics: Option[];
  relations: string[];
  credentials: Option[];
  skills: string[];
  identifiers: Option[];
  observation_topics: string[];
}
interface PersonRow {
  id: string;
  kind: Kind;
  kind_label: string;
  name: string;
  status: "active" | "inactive";
  synthetic: boolean;
  preferred_channel: Channel;
  do_not_contact: boolean;
  language: string;
  linked_to_ehr: boolean;
  skills: string[];
  credentials_expired: number;
  credentials_expiring: number;
  organisation: string | null;
}
interface Profile {
  first_name?: string | null;
  last_name?: string | null;
  preferred_name?: string | null;
  date_of_birth?: string | null;
  phone?: string | null;
  email?: string | null;
  address?: string | null;
  zip?: string | null;
  member_id?: string | null;
  lat?: number | null;
  lon?: number | null;
}
interface PersonDetail {
  id: string;
  kind: Kind;
  kind_label: string;
  status: "active" | "inactive";
  synthetic: boolean;
  name: string;
  profile: Profile;
  ehr: { connection_id: string; patient_ref: string; found: boolean; name: string | null; date_of_birth: string | null } | null;
  organisation: string | null;
  role_title: string | null;
  language: string;
  preferred_channel: Channel;
  do_not_contact: boolean;
  do_not_contact_reason: string | null;
  timezone: string;
  contact_windows: Window[];
  consent: Partial<Record<Channel, { state: "granted" | "revoked" | "unknown"; at?: string | null; source?: string | null }>>;
  skills: string[];
  availability: Window[];
  max_travel_km: number | null;
  reliability: Record<string, number>;
  credentials: { id: string; kind: string; label: string; identifier: string | null; issued_on: string | null; expires_on: string | null; state: "current" | "expiring" | "expired" }[];
  relationships: {
    id: string;
    direction: "for" | "has";
    other_id: string;
    other_name: string | null;
    relation: string;
    proxy: boolean;
    scope: string[];
    starts_on: string | null;
    expires_on: string | null;
    evidence: string | null;
    state: "active" | "revoked" | "expired" | "not_started";
  }[];
  observations: { id: string; topic: string; text: string | null; visibility: "agent" | "staff"; source: string; source_attempt_id: string | null; confirmed: boolean; created_at: string }[];
  checks: { id: string; check: string; outcome: "passed" | "failed" | "refused"; reason: string | null; identifiers: string[]; topic: string | null; attempt_id: string | null; as: "caller" | "subject"; created_at: string }[];
}
interface Policy {
  version: number;
  identifiers: string[];
  step_up_identifiers: string[];
  step_up_topics: string[];
  max_attempts: number;
  proxy_subject_identifiers: string[];
}
interface RankResult {
  eligible: { person_id: string; name: string | null; kind: string; score: number; channel: Channel; contact_now: boolean; contact_reason: string; distance_km: number | null; reliability: number; reasons: string[] }[];
  excluded: { person_id: string; name: string | null; reason: string; detail: string | null }[];
}

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const CHANNEL_LABEL: Record<Channel, string> = { voice: "Voice", sms: "Text", email: "Email" };
const EXCLUSION_LABEL: Record<string, string> = {
  inactive: "Inactive",
  do_not_contact: "Do not contact",
  cannot_contact: "No channel they can be reached on",
  declined_by_client: "Declined by this client",
  missing_skill: "Missing skill",
  missing_credential: "Missing credential",
  expired_credential: "Expired credential",
  unavailable: "Not available then",
  too_far: "Too far to travel",
};
const CHECK_LABEL: Record<string, string> = { identity: "Identity", step_up: "Extra verification", proxy: "Representative check", disclosure: "Memory shared" };

function useOptions() {
  return useQuery({ queryKey: ["people-options"], queryFn: () => api.get<Options>("/people/options"), staleTime: 300_000 });
}


export default function People() {
  const [view, setView] = useState<"directory" | "match" | "policy">("directory");
  return (
    <>
      <Toolbar
        eyebrow="Work"
        title="People"
        description="Patients, clients, families and their representatives, caregivers, staff and payer contacts. Agents read only what a verified caller may hear, write back what they learn, and never contact anyone marked do not contact."
        actions={<Segmented label="View" value={view} onChange={setView} options={[{ value: "directory", label: "Directory" }, { value: "match", label: "Shift match" }, { value: "policy", label: "Verification" }]} />}
      />
      <div className="content">
        {view === "directory" && <Directory />}
        {view === "match" && <ShiftMatch />}
        {view === "policy" && <PolicyCard />}
      </div>
    </>
  );
}

// ---------- Directory ----------

function Directory() {
  const canEdit = useCan("start_task");
  const qc = useQueryClient();
  const toast = useToast();
  const options = useOptions();
  const [params, setParams] = useSearchParams();
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState("");
  const [creating, setCreating] = useState(false);
  const openId = params.get("person");
  const open = (id: string | null) => setParams(id ? { person: id } : {}, { replace: true });
  const qs = new URLSearchParams({ q: query, ...(kind ? { kind } : {}) });
  const people = useQuery({ queryKey: ["people", qs.toString()], queryFn: () => api.get<PersonRow[]>(`/people?${qs}`) });
  const samples = useMutation({
    mutationFn: () => api.post<{ added: number; agency: string }>("/people/samples"),
    onSuccess: (r) => {
      toast(r.added ? `Added ${r.added} people from ${r.agency}` : `${r.agency} is already here`);
      qc.invalidateQueries({ queryKey: ["people"] });
    },
  });
  const link = useMutation({
    mutationFn: () => api.post<{ linked: number }>("/people/link-ehr", {}),
    onSuccess: (r) => {
      toast(r.linked ? `Linked ${r.linked} practice ${r.linked === 1 ? "patient" : "patients"}` : "Every practice patient already has a profile");
      qc.invalidateQueries({ queryKey: ["people"] });
    },
  });
  const filtered = query || kind;
  return (
    <>
      <div className="row" style={{ flexWrap: "wrap", gap: 10 }}>
        <div className="row" style={{ position: "relative", flex: "1 1 240px", minWidth: 0 }}>
          <Search size={16} style={{ position: "absolute", left: 12, color: "var(--label-3)" }} aria-hidden />
          <input className="input" style={{ paddingLeft: 36 }} placeholder="Search by name" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search people" />
        </div>
        <select className="select" style={{ width: "auto", minWidth: 190 }} value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Kind of person">
          <option value="">Everyone</option>
          {options.data?.kinds.map((k) => (
            <option key={k.id} value={k.id}>
              {k.label}
            </option>
          ))}
        </select>
        {canEdit && (
          <>
            <button className="btn btn-secondary" onClick={() => link.mutate()} disabled={link.isPending} title="Give every patient in the practice mirror a profile, linked rather than copied">
              <Link2 size={15} aria-hidden /> Link practice patients
            </button>
            <button className="btn btn-secondary" onClick={() => samples.mutate()} disabled={samples.isPending}>
              <FlaskConical size={15} aria-hidden /> Add sandbox agency
            </button>
            <button className="btn btn-primary" onClick={() => setCreating(true)}>
              <Plus size={16} aria-hidden /> New person
            </button>
          </>
        )}
      </div>
      <Card>
        {people.isPending && <ListSkeleton rows={6} />}
        {people.error && <ErrorState error={people.error} onRetry={() => people.refetch()} title="Couldn't load people" />}
        {people.data?.length === 0 &&
          (filtered ? (
            <Empty icon={Search} title="No one matches">
              Try another name or kind.
            </Empty>
          ) : (
            <Empty
              icon={Users}
              title="No people yet"
              action={
                canEdit && (
                  <button className="btn btn-primary" onClick={() => samples.mutate()} disabled={samples.isPending}>
                    <FlaskConical size={15} aria-hidden /> Add sandbox agency
                  </button>
                )
              }
            >
              Profiles come from your EHR and your team. Until real data is allowed, add the synthetic sandbox agency: caregivers, clients, families and their representatives.
            </Empty>
          ))}
        {!!people.data?.length && (
          <ul className="list">
            {people.data.map((p) => (
              <li key={p.id}>
                <button type="button" className="list-row" onClick={() => open(p.id)} style={{ width: "100%", textAlign: "left" }}>
                  <div className="row-main">
                    <div className="row-title">
                      {p.name} <span className="muted">· {p.kind_label}</span>
                    </div>
                    <div className="row-sub">
                      {[CHANNEL_LABEL[p.preferred_channel], p.language !== "en" && `language ${p.language}`, p.organisation, p.skills.length > 0 && p.skills.map(humanize).join(", "), p.linked_to_ehr && "Linked to the practice EHR"].filter(Boolean).join(" · ")}
                    </div>
                  </div>
                  {p.do_not_contact && (
                    <Pill tone="red" icon={Ban}>
                      Do not contact
                    </Pill>
                  )}
                  {p.credentials_expired > 0 && <Pill tone="red">Credential expired</Pill>}
                  {p.credentials_expiring > 0 && p.credentials_expired === 0 && <Pill tone="orange">Credential expiring</Pill>}
                  {p.status === "inactive" && <Pill tone="gray">Inactive</Pill>}
                  <ChevronRight size={16} className="chevron" aria-hidden />
                </button>
              </li>
            ))}
          </ul>
        )}
      </Card>
      {openId && <PersonSheet id={openId} onClose={() => open(null)} />}
      {creating && options.data && (
        <NewPersonSheet
          options={options.data}
          onClose={() => setCreating(false)}
          onCreated={(id) => {
            setCreating(false);
            open(id);
          }}
        />
      )}
    </>
  );
}

function NewPersonSheet({ options, onClose, onCreated }: { options: Options; onClose: () => void; onCreated: (id: string) => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [kind, setKind] = useState<Kind>("caregiver");
  const [profile, setProfile] = useState<Profile>({});
  const [channel, setChannel] = useState<Channel>("voice");
  const create = useMutation({
    mutationFn: () => api.post<PersonDetail>("/people", { kind, preferred_channel: channel, profile: Object.fromEntries(Object.entries(profile).filter(([, v]) => v !== "" && v !== null)) }),
    onSuccess: (p) => {
      toast(`Added ${p.name}`);
      qc.invalidateQueries({ queryKey: ["people"] });
      onCreated(p.id);
    },
  });
  const set = (k: keyof Profile) => (e: React.ChangeEvent<HTMLInputElement>) => setProfile((p) => ({ ...p, [k]: e.target.value }));
  return (
    <Sheet
      title="New person"
      subtitle="Contact rules, skills, credentials and representatives are added on their profile next."
      onClose={onClose}
      footer={
        <>
          <span className="spacer" />
          <button className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" onClick={() => create.mutate()} disabled={create.isPending}>
            {create.isPending ? "Adding…" : "Add person"}
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
          Use made-up people. A name must include “Synthetic”, “Sample”, “Example” or “Test”. Practice patients come from the EHR with Link practice patients.
        </Banner>
        <label className="field">
          <span className="field-label">Kind</span>
          <select className="select" value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
            {options.kinds.map((k) => (
              <option key={k.id} value={k.id}>
                {k.label}
              </option>
            ))}
          </select>
        </label>
        <div className="grid-2">
          <label className="field">
            <span className="field-label">First name</span>
            <input className="input" value={profile.first_name ?? ""} onChange={set("first_name")} required />
          </label>
          <label className="field">
            <span className="field-label">Last name</span>
            <input className="input" value={profile.last_name ?? ""} onChange={set("last_name")} required />
          </label>
          <label className="field">
            <span className="field-label">
              Date of birth <span className="muted">(optional)</span>
            </span>
            <input className="input" type="date" value={profile.date_of_birth ?? ""} onChange={set("date_of_birth")} />
          </label>
          <label className="field">
            <span className="field-label">
              Phone <span className="muted">(optional)</span>
            </span>
            <input className="input" type="tel" placeholder="+1 312 555 0100" value={profile.phone ?? ""} onChange={set("phone")} />
          </label>
        </div>
        <label className="field">
          <span className="field-label">Preferred channel</span>
          <select className="select" value={channel} onChange={(e) => setChannel(e.target.value as Channel)}>
            {(Object.keys(CHANNEL_LABEL) as Channel[]).map((c) => (
              <option key={c} value={c}>
                {CHANNEL_LABEL[c]}
              </option>
            ))}
          </select>
        </label>
        {create.error && <Banner tone="red" title={create.error instanceof ApiError ? create.error.message : "Couldn't add this person"} />}
        <button type="submit" hidden />
      </form>
    </Sheet>
  );
}

// ---------- One person ----------

function PersonSheet({ id, onClose }: { id: string; onClose: () => void }) {
  const canEdit = useCan("start_task");
  const qc = useQueryClient();
  const toast = useToast();
  const options = useOptions();
  const detail = useQuery({ queryKey: ["person", id], queryFn: () => api.get<PersonDetail>(`/people/${id}`) });
  const p = detail.data;
  const [draft, setDraft] = useState<Partial<PersonDetail> | null>(null);
  useEffect(() => setDraft(null), [id]);
  const value = <K extends keyof PersonDetail>(k: K): PersonDetail[K] | undefined => (draft && k in draft ? (draft[k] as PersonDetail[K]) : p?.[k]);
  const change = (patch: Partial<PersonDetail>) => setDraft((d) => ({ ...(d ?? {}), ...patch }));
  const applied = (next: PersonDetail) => {
    qc.setQueryData(["person", id], next);
    qc.invalidateQueries({ queryKey: ["people"] });
  };
  const save = useMutation({
    mutationFn: () => api.put<PersonDetail>(`/people/${id}`, draft),
    onSuccess: (next) => {
      applied(next);
      setDraft(null);
      toast("Saved");
    },
  });
  return (
    <Sheet
      wide
      title={p ? p.name : "Person"}
      subtitle={p ? [p.kind_label, p.role_title, p.organisation].filter(Boolean).join(" · ") : undefined}
      onClose={onClose}
      footer={
        canEdit &&
        p && (
          <>
            {save.error && <span className="field-error">{save.error instanceof ApiError ? save.error.message : "Couldn't save"}</span>}
            <span className="spacer" />
            <button className="btn btn-secondary" onClick={() => setDraft(null)} disabled={!draft}>
              Discard
            </button>
            <button className="btn btn-primary" onClick={() => save.mutate()} disabled={!draft || save.isPending}>
              {save.isPending ? "Saving…" : "Save changes"}
            </button>
          </>
        )
      }
    >
      {detail.isPending && <ListSkeleton rows={4} icon={false} />}
      {detail.error && <ErrorState error={detail.error} onRetry={() => detail.refetch()} title="Couldn't load this person" />}
      {p && options.data && (
        <div className="stack" style={{ gap: 24 }}>
          <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
            {p.do_not_contact ? (
              <Pill tone="red" icon={Ban}>
                Do not contact
              </Pill>
            ) : (
              <Pill tone="green">May be contacted</Pill>
            )}
            {p.status === "inactive" && <Pill tone="gray">Inactive</Pill>}
            {p.synthetic && <span className="sample-label">Synthetic</span>}
          </div>

          <section className="stack" style={{ gap: 8 }}>
            <h3 className="field-label">Identity</h3>
            {p.ehr ? (
              <p className="small" style={{ margin: 0 }}>
                <Link2 size={13} aria-hidden style={{ verticalAlign: "-2px" }} /> Linked to the practice EHR (patient <span className="mono">{p.ehr.patient_ref}</span>
                {p.ehr.found ? `): ${p.ehr.name}, born ${p.ehr.date_of_birth ?? "not given"}` : "), not in the mirror right now"}. Name and date of birth are read from the EHR, never copied.
              </p>
            ) : (
              <IdentityFields profile={{ ...p.profile, ...((value("profile") as Profile) ?? {}) }} disabled={!canEdit} onChange={(profile) => change({ profile })} />
            )}
          </section>

          <section className="stack" style={{ gap: 10 }}>
            <h3 className="field-label">Contact rules</h3>
            <div className="grid-3">
              <label className="field">
                <span className="field-label">Preferred channel</span>
                <select className="select" value={value("preferred_channel")} disabled={!canEdit} onChange={(e) => change({ preferred_channel: e.target.value as Channel })}>
                  {(Object.keys(CHANNEL_LABEL) as Channel[]).map((c) => (
                    <option key={c} value={c}>
                      {CHANNEL_LABEL[c]}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span className="field-label">Language</span>
                <input className="input" value={value("language") ?? ""} disabled={!canEdit} onChange={(e) => change({ language: e.target.value })} />
              </label>
              <label className="field">
                <span className="field-label">Time zone</span>
                <input className="input" value={value("timezone") ?? ""} disabled={!canEdit} onChange={(e) => change({ timezone: e.target.value })} />
              </label>
            </div>
            <div className="row" style={{ gap: 10 }}>
              <Switch checked={!!value("do_not_contact")} disabled={!canEdit} onChange={(v) => change({ do_not_contact: v })} label="Do not contact" />
              <span>Do not contact</span>
              {value("do_not_contact") && (
                <input className="input" style={{ flex: 1 }} placeholder="Why (optional)" value={value("do_not_contact_reason") ?? ""} disabled={!canEdit} onChange={(e) => change({ do_not_contact_reason: e.target.value })} aria-label="Do not contact reason" />
              )}
            </div>
            <div className="grid-3">
              {(Object.keys(CHANNEL_LABEL) as Channel[]).map((c) => {
                const consent = value("consent") ?? {};
                return (
                  <label key={c} className="field">
                    <span className="field-label">{CHANNEL_LABEL[c]} consent</span>
                    <select
                      className="select"
                      value={consent[c]?.state ?? "unknown"}
                      disabled={!canEdit}
                      onChange={(e) => change({ consent: { ...consent, [c]: { state: e.target.value as "granted", at: new Date().toISOString(), source: "staff" } } })}
                    >
                      <option value="unknown">Not asked</option>
                      <option value="granted">Granted</option>
                      <option value="revoked">Revoked</option>
                    </select>
                  </label>
                );
              })}
            </div>
            <WindowsEditor label="When they may be contacted" help="No windows means any reasonable hour." windows={value("contact_windows") ?? []} disabled={!canEdit} onChange={(contact_windows) => change({ contact_windows })} />
          </section>

          {(p.kind === "caregiver" || p.kind === "client" || p.kind === "patient") && (
            <section className="stack" style={{ gap: 10 }}>
              <h3 className="field-label">{p.kind === "caregiver" ? "Work" : "Visit preferences"}</h3>
              <WindowsEditor
                label={p.kind === "caregiver" ? "Availability" : "Preferred visit times"}
                help={p.kind === "caregiver" ? "Shifts outside these windows are never offered." : "Used to choose who to offer an earlier slot."}
                windows={value("availability") ?? []}
                disabled={!canEdit}
                onChange={(availability) => change({ availability })}
              />
              {p.kind === "caregiver" && (
                <>
                  <div className="field">
                    <span className="field-label">Skills</span>
                    <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
                      {options.data.skills.map((s) => {
                        const on = (value("skills") ?? []).includes(s);
                        return (
                          <button key={s} type="button" className={`chip${on ? " tone-blue" : ""}`} aria-pressed={on} disabled={!canEdit} onClick={() => change({ skills: on ? (value("skills") ?? []).filter((x) => x !== s) : [...(value("skills") ?? []), s] })}>
                            {humanize(s)}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                  <div className="grid-2">
                    <label className="field">
                      <span className="field-label">Farthest they'll travel (km)</span>
                      <input className="input" type="number" min={0} value={value("max_travel_km") ?? ""} disabled={!canEdit} onChange={(e) => change({ max_travel_km: e.target.value === "" ? null : Number(e.target.value) })} />
                    </label>
                    <div className="field">
                      <span className="field-label">Reliability</span>
                      <span className="small">
                        {Object.keys(p.reliability).length
                          ? `${p.reliability.accepted ?? 0} of ${p.reliability.offered ?? 0} shifts accepted · ${p.reliability.completed ?? 0} completed · ${p.reliability.no_shows ?? 0} no-shows · ${p.reliability.late ?? 0} late`
                          : "No shift history yet"}
                      </span>
                    </div>
                  </div>
                  <Credentials person={p} options={options.data} canEdit={canEdit} onChange={applied} />
                </>
              )}
            </section>
          )}

          <Relationships person={p} options={options.data} canEdit={canEdit} onChange={applied} />
          <Memory person={p} options={options.data} canEdit={canEdit} onChange={applied} />

          <section className="stack" style={{ gap: 6 }}>
            <h3 className="field-label">Verification evidence</h3>
            {p.checks.length === 0 ? (
              <p className="small muted" style={{ margin: 0 }}>
                No agent has checked this person's identity or authority yet.
              </p>
            ) : (
              <ul className="list small">
                {p.checks.map((v) => (
                  <li key={v.id} className="row" style={{ padding: "5px 0", gap: 10 }}>
                    <Pill tone={v.outcome === "passed" ? "green" : "red"}>{v.outcome === "passed" ? "Passed" : v.outcome === "failed" ? "Failed" : "Refused"}</Pill>
                    <span style={{ flex: 1, minWidth: 0 }}>
                      {CHECK_LABEL[v.check] ?? v.check}
                      {v.topic && ` · ${humanize(v.topic)}`}
                      {v.as === "subject" && " · asked by a representative"}
                      {v.reason && <span className="muted"> · {humanize(v.reason)}</span>}
                      {v.identifiers.length > 0 && <span className="muted"> · checked {v.identifiers.map(humanize).join(", ").toLowerCase()}</span>}
                    </span>
                    <span className="muted">{timeAgo(v.created_at)}</span>
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

function IdentityFields({ profile, disabled, onChange }: { profile: Profile; disabled: boolean; onChange: (p: Profile) => void }) {
  const field = (k: keyof Profile, label: string, type = "text") => (
    <label className="field">
      <span className="field-label">{label}</span>
      <input
        className="input"
        type={type}
        value={(profile[k] as string | number | null | undefined) ?? ""}
        disabled={disabled}
        onChange={(e) => onChange({ ...profile, [k]: type === "number" ? (e.target.value === "" ? null : Number(e.target.value)) : e.target.value })}
      />
    </label>
  );
  return (
    <div className="grid-3">
      {field("first_name", "First name")}
      {field("last_name", "Last name")}
      {field("preferred_name", "Goes by")}
      {field("date_of_birth", "Date of birth", "date")}
      {field("phone", "Phone", "tel")}
      {field("zip", "ZIP code")}
      {field("member_id", "Member ID")}
      {field("lat", "Home latitude", "number")}
      {field("lon", "Home longitude", "number")}
    </div>
  );
}

function WindowsEditor({ label, help, windows, disabled, onChange }: { label: string; help: string; windows: Window[]; disabled: boolean; onChange: (w: Window[]) => void }) {
  const update = (i: number, w: Window) => onChange(windows.map((x, j) => (j === i ? w : x)));
  return (
    <div className="field">
      <span className="field-label">{label}</span>
      <span className="field-help">{help}</span>
      <div className="stack" style={{ gap: 8 }}>
        {windows.map((w, i) => (
          <div key={i} className="row" style={{ flexWrap: "wrap", gap: 6 }}>
            {DAYS.map((d, day) => {
              const on = w.days.includes(day);
              return (
                <button key={d} type="button" className={`chip${on ? " tone-blue" : ""}`} aria-pressed={on} disabled={disabled} onClick={() => update(i, { ...w, days: on ? w.days.filter((x) => x !== day) : [...w.days, day].sort() })}>
                  {d}
                </button>
              );
            })}
            <input className="input" type="time" style={{ width: 120 }} value={w.start} disabled={disabled} onChange={(e) => update(i, { ...w, start: e.target.value })} aria-label="From" />
            <span className="muted">to</span>
            <input className="input" type="time" style={{ width: 120 }} value={w.end} disabled={disabled} onChange={(e) => update(i, { ...w, end: e.target.value })} aria-label="Until" />
            {!disabled && (
              <button type="button" className="btn btn-plain btn-icon" aria-label="Remove window" onClick={() => onChange(windows.filter((_, j) => j !== i))}>
                <Trash2 size={15} aria-hidden />
              </button>
            )}
          </div>
        ))}
        {!disabled && (
          <button type="button" className="btn btn-secondary" style={{ justifySelf: "start" }} onClick={() => onChange([...windows, { days: [0, 1, 2, 3, 4], start: "09:00", end: "17:00" }])}>
            <CalendarClock size={15} aria-hidden /> Add window
          </button>
        )}
        {windows.length === 0 && disabled && <span className="small muted">None</span>}
      </div>
    </div>
  );
}

function Credentials({ person, options, canEdit, onChange }: { person: PersonDetail; options: Options; canEdit: boolean; onChange: (p: PersonDetail) => void }) {
  const [kind, setKind] = useState(options.credentials[0]?.id ?? "");
  const [expires, setExpires] = useState("");
  const add = useMutation({
    mutationFn: () => api.post<PersonDetail>(`/people/${person.id}/credentials`, { kind, expires_on: expires || null }),
    onSuccess: (p) => {
      onChange(p);
      setExpires("");
    },
  });
  const remove = useMutation({ mutationFn: (cid: string) => api.del<PersonDetail>(`/people/${person.id}/credentials/${cid}`), onSuccess: onChange });
  return (
    <div className="field">
      <span className="field-label">Credentials</span>
      <span className="field-help">An expired licence, CPR or TB test stops a caregiver being offered any shift.</span>
      {person.credentials.length > 0 && (
        <ul className="list small">
          {person.credentials.map((c) => (
            <li key={c.id} className="row" style={{ padding: "5px 0", gap: 10 }}>
              <span style={{ flex: 1 }}>
                {c.label} {c.identifier && <span className="mono muted">{c.identifier}</span>}
              </span>
              <span className="muted">{c.expires_on ? `expires ${c.expires_on}` : "no expiry"}</span>
              <Pill tone={c.state === "expired" ? "red" : c.state === "expiring" ? "orange" : "green"}>{c.state === "expired" ? "Expired" : c.state === "expiring" ? "Expiring" : "Current"}</Pill>
              {canEdit && (
                <button type="button" className="btn btn-plain btn-icon" aria-label={`Remove ${c.label}`} onClick={() => remove.mutate(c.id)}>
                  <Trash2 size={15} aria-hidden />
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {canEdit && (
        <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
          <select className="select" style={{ width: "auto" }} value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Credential">
            {options.credentials.map((c) => (
              <option key={c.id} value={c.id}>
                {c.label}
              </option>
            ))}
          </select>
          <input className="input" type="date" style={{ width: 170 }} value={expires} onChange={(e) => setExpires(e.target.value)} aria-label="Expires on" />
          <button type="button" className="btn btn-secondary" onClick={() => add.mutate()} disabled={add.isPending}>
            <Plus size={15} aria-hidden /> Add credential
          </button>
        </div>
      )}
      {add.error && <span className="field-error">{(add.error as Error).message}</span>}
    </div>
  );
}

function Relationships({ person, options, canEdit, onChange }: { person: PersonDetail; options: Options; canEdit: boolean; onChange: (p: PersonDetail) => void }) {
  const everyone = useQuery({ queryKey: ["people", ""], queryFn: () => api.get<PersonRow[]>("/people?q=") });
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState({ related_id: "", relation: "child", proxy: true, scope: ["general"] as string[], expires_on: "", evidence: "" });
  const save = useMutation({
    mutationFn: () => api.post<PersonDetail>(`/people/${person.id}/relationships`, { ...form, expires_on: form.expires_on || null, evidence: form.evidence || null }),
    onSuccess: (p) => {
      onChange(p);
      setAdding(false);
    },
  });
  const revoke = useMutation({ mutationFn: (rid: string) => api.post<PersonDetail>(`/people/${person.id}/relationships/${rid}/revoke`), onSuccess: onChange });
  const fit = form.relation === "preferred_caregiver" || form.relation === "declined_caregiver";
  const topicLabel = (t: string) => (t === "all" ? "Everything" : options.topics.find((o) => o.id === t)?.label ?? humanize(t));
  return (
    <section className="stack" style={{ gap: 8 }}>
      <div className="row">
        <h3 className="field-label" style={{ flex: 1 }}>
          Representatives and relationships
        </h3>
        {canEdit && !adding && (person.kind === "patient" || person.kind === "client") && (
          <button type="button" className="btn btn-secondary" onClick={() => setAdding(true)}>
            <Plus size={15} aria-hidden /> Add
          </button>
        )}
      </div>
      {person.relationships.length === 0 && !adding && (
        <p className="small muted" style={{ margin: 0 }}>
          No one is on file. An agent shares nothing about this person with anyone else.
        </p>
      )}
      {person.relationships.length > 0 && (
        <ul className="list small">
          {person.relationships.map((r) => (
            <li key={r.id} className="row" style={{ padding: "6px 0", gap: 10, alignItems: "flex-start" }}>
              <UserRound size={15} aria-hidden style={{ color: "var(--label-3)", marginTop: 2 }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div>
                  {r.direction === "has" ? `${r.other_name ?? "Someone"} · ${humanize(r.relation)}` : `${humanize(r.relation)} of ${r.other_name ?? "someone"}`}
                  {r.proxy && " · authorised representative"}
                </div>
                {r.proxy && (
                  <div className="muted">
                    May act on {r.scope.map(topicLabel).join(", ").toLowerCase()}
                    {r.expires_on ? ` · until ${r.expires_on}` : " · no end date"}
                    {r.evidence ? ` · ${r.evidence}` : ""}
                  </div>
                )}
              </div>
              {r.proxy && <Pill tone={r.state === "active" ? "green" : r.state === "revoked" ? "gray" : "orange"}>{humanize(r.state)}</Pill>}
              {canEdit && r.state !== "revoked" && r.direction === "has" && (
                <button type="button" className="btn btn-plain" onClick={() => revoke.mutate(r.id)} disabled={revoke.isPending}>
                  Revoke
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {adding && (
        <div className="subcard stack" style={{ gap: 12 }}>
          <div className="grid-2">
            <label className="field">
              <span className="field-label">Person</span>
              <select className="select" value={form.related_id} onChange={(e) => setForm({ ...form, related_id: e.target.value })}>
                <option value="">Choose…</option>
                {everyone.data
                  ?.filter((o) => o.id !== person.id && (fit ? o.kind === "caregiver" : o.kind !== "caregiver"))
                  .map((o) => (
                    <option key={o.id} value={o.id}>
                      {o.name} · {o.kind_label}
                    </option>
                  ))}
              </select>
            </label>
            <label className="field">
              <span className="field-label">Relationship</span>
              <select className="select" value={form.relation} onChange={(e) => setForm({ ...form, relation: e.target.value, related_id: "" })}>
                {options.relations.map((r) => (
                  <option key={r} value={r}>
                    {humanize(r)}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {!fit && (
            <>
              <label className="checkbox-row">
                <input type="checkbox" checked={form.proxy} onChange={(e) => setForm({ ...form, proxy: e.target.checked })} />
                <span>
                  <ShieldCheck size={14} aria-hidden /> Authorised to act for {person.name}
                </span>
              </label>
              {form.proxy && (
                <>
                  <div className="field">
                    <span className="field-label">May act on</span>
                    <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
                      {[...options.topics.map((t) => t.id), "all"].map((t) => {
                        const on = form.scope.includes(t);
                        return (
                          <button key={t} type="button" className={`chip${on ? " tone-blue" : ""}`} aria-pressed={on} onClick={() => setForm({ ...form, scope: on ? form.scope.filter((x) => x !== t) : [...form.scope, t] })}>
                            {topicLabel(t)}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                  <div className="grid-2">
                    <label className="field">
                      <span className="field-label">What authorises them</span>
                      <input className="input" placeholder="Signed representative form, 2026-11" value={form.evidence} onChange={(e) => setForm({ ...form, evidence: e.target.value })} />
                    </label>
                    <label className="field">
                      <span className="field-label">
                        Until <span className="muted">(optional)</span>
                      </span>
                      <input className="input" type="date" value={form.expires_on} onChange={(e) => setForm({ ...form, expires_on: e.target.value })} />
                    </label>
                  </div>
                </>
              )}
            </>
          )}
          {save.error && <Banner tone="red" title={save.error instanceof ApiError ? save.error.message : "Couldn't save"} />}
          <div className="row" style={{ gap: 8 }}>
            <span className="spacer" />
            <button type="button" className="btn btn-secondary" onClick={() => setAdding(false)}>
              Cancel
            </button>
            <button type="button" className="btn btn-primary" disabled={!form.related_id || save.isPending} onClick={() => save.mutate()}>
              Save
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

function Memory({ person, options, canEdit, onChange }: { person: PersonDetail; options: Options; canEdit: boolean; onChange: (p: PersonDetail) => void }) {
  const [topic, setTopic] = useState("preference");
  const [text, setText] = useState("");
  const [visibility, setVisibility] = useState<"agent" | "staff">("agent");
  const add = useMutation({
    mutationFn: () => api.post<PersonDetail>(`/people/${person.id}/observations`, { topic, text, visibility }),
    onSuccess: (p) => {
      onChange(p);
      setText("");
    },
  });
  return (
    <section className="stack" style={{ gap: 8 }}>
      <h3 className="field-label">Memory</h3>
      {person.observations.length === 0 ? (
        <p className="small muted" style={{ margin: 0 }}>
          Nothing recorded yet. Agents add what they learn on calls; staff can add notes here.
        </p>
      ) : (
        <ul className="list small">
          {person.observations.map((o) => (
            <li key={o.id} className="row" style={{ padding: "6px 0", gap: 10, alignItems: "flex-start" }}>
              <span className="chip">{humanize(o.topic)}</span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div>{o.text}</div>
                <div className="muted">
                  {o.source === "call" ? "From a call" : o.source === "staff" ? "Staff note" : "Sample"}
                  {o.source_attempt_id && (
                    <>
                      {" "}
                      <a href={`/calls/${o.source_attempt_id}`} className="mono">
                        {o.source_attempt_id.slice(0, 8)}
                      </a>
                    </>
                  )}
                  {" · "}
                  {o.visibility === "staff" ? "staff only" : "agents can read"}
                  {!o.confirmed && " · not yet reviewed"} · {timeAgo(o.created_at)}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
      {canEdit && (
        <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
          <select className="select" style={{ width: "auto" }} value={topic} onChange={(e) => setTopic(e.target.value)} aria-label="Topic">
            {options.observation_topics.map((t) => (
              <option key={t} value={t}>
                {humanize(t)}
              </option>
            ))}
          </select>
          <input className="input" style={{ flex: "1 1 220px" }} placeholder="Prefers a call 30 minutes before a visit" value={text} onChange={(e) => setText(e.target.value)} aria-label="Note" maxLength={500} />
          <select className="select" style={{ width: "auto" }} value={visibility} onChange={(e) => setVisibility(e.target.value as "agent" | "staff")} aria-label="Who can read it">
            <option value="agent">Agents can read</option>
            <option value="staff">Staff only</option>
          </select>
          <button type="button" className="btn btn-secondary" disabled={text.trim().length < 3 || add.isPending} onClick={() => add.mutate()}>
            <Plus size={15} aria-hidden /> Add note
          </button>
        </div>
      )}
    </section>
  );
}

// ---------- Shift match (the ranking contact-many uses) ----------

function nextMonday() {
  const d = new Date();
  d.setDate(d.getDate() + ((8 - d.getDay()) % 7 || 7));
  return d.toISOString().slice(0, 10);
}

function ShiftMatch() {
  const options = useOptions();
  const clients = useQuery({ queryKey: ["people", "kind=client"], queryFn: () => api.get<PersonRow[]>("/people?kind=client") });
  const [form, setForm] = useState({ day: nextMonday(), start: "09:00", end: "13:00", client_id: "", skills: ["personal_care"] as string[], credentials: [] as string[], language: "" });
  const rank = useMutation({
    mutationFn: () => {
      const at = (t: string) => new Date(`${form.day}T${t}`).toISOString();
      return api.post<RankResult>("/people/rank", {
        starts_at: at(form.start),
        ends_at: at(form.end),
        skills: form.skills,
        credentials: form.credentials,
        client_id: form.client_id || null,
        language: form.language || null,
      });
    },
  });
  const toggle = (key: "skills" | "credentials", v: string) => setForm((f) => ({ ...f, [key]: f[key].includes(v) ? f[key].filter((x) => x !== v) : [...f[key], v] }));
  return (
    <>
      <Card title="Who would be offered this shift" subtitle="The same ranking contact-many uses: hard rules first (do not contact, skills, credentials current through the shift, availability, distance, the client's declines), then reliability, distance, the client's preferences and language. Nobody is contacted from here.">
        <div className="stack card-pad" style={{ gap: 14 }}>
          <div className="grid-3">
            <label className="field">
              <span className="field-label">Day</span>
              <input className="input" type="date" value={form.day} onChange={(e) => setForm({ ...form, day: e.target.value })} />
            </label>
            <label className="field">
              <span className="field-label">From</span>
              <input className="input" type="time" value={form.start} onChange={(e) => setForm({ ...form, start: e.target.value })} />
            </label>
            <label className="field">
              <span className="field-label">Until</span>
              <input className="input" type="time" value={form.end} onChange={(e) => setForm({ ...form, end: e.target.value })} />
            </label>
          </div>
          <div className="grid-2">
            <label className="field">
              <span className="field-label">Client</span>
              <select className="select" value={form.client_id} onChange={(e) => setForm({ ...form, client_id: e.target.value })}>
                <option value="">No client (distance not checked)</option>
                {clients.data?.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span className="field-label">
                Language <span className="muted">(optional)</span>
              </span>
              <input className="input" placeholder="es" value={form.language} onChange={(e) => setForm({ ...form, language: e.target.value })} />
            </label>
          </div>
          <div className="field">
            <span className="field-label">Skills needed</span>
            <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
              {options.data?.skills.map((s) => (
                <button key={s} type="button" className={`chip${form.skills.includes(s) ? " tone-blue" : ""}`} aria-pressed={form.skills.includes(s)} onClick={() => toggle("skills", s)}>
                  {humanize(s)}
                </button>
              ))}
            </div>
          </div>
          <div className="field">
            <span className="field-label">Credentials required</span>
            <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
              {options.data?.credentials.map((c) => (
                <button key={c.id} type="button" className={`chip${form.credentials.includes(c.id) ? " tone-blue" : ""}`} aria-pressed={form.credentials.includes(c.id)} onClick={() => toggle("credentials", c.id)}>
                  {c.label}
                </button>
              ))}
            </div>
          </div>
          <div className="row">
            <span className="spacer" />
            <button className="btn btn-primary" onClick={() => rank.mutate()} disabled={rank.isPending}>
              {rank.isPending ? "Ranking…" : "Rank caregivers"}
            </button>
          </div>
          {rank.error && <Banner tone="red" title={rank.error instanceof ApiError ? rank.error.message : "Couldn't rank"} />}
        </div>
      </Card>
      {rank.data && (
        <>
          <Card title={`Eligible (${rank.data.eligible.length})`} subtitle="Contacted in this order, on the channel shown.">
            {rank.data.eligible.length === 0 ? (
              <Empty icon={Users} title="No one is eligible">
                Everyone was left out by a hard rule. See why below.
              </Empty>
            ) : (
              <ul className="list">
                {rank.data.eligible.map((e, i) => (
                  <li key={e.person_id} className="list-row">
                    <span className="mono muted" style={{ width: 22 }}>
                      {i + 1}
                    </span>
                    <div className="row-main">
                      <div className="row-title">
                        {e.name} <span className="muted">· score {e.score}</span>
                      </div>
                      <div className="row-sub">{e.reasons.join(" · ")}</div>
                    </div>
                    <span className="chip">{CHANNEL_LABEL[e.channel]}</span>
                    {e.contact_now ? <Pill tone="green">Can contact now</Pill> : <Pill tone="orange">Outside contact hours</Pill>}
                  </li>
                ))}
              </ul>
            )}
          </Card>
          {rank.data.excluded.length > 0 && (
            <Card title={`Left out (${rank.data.excluded.length})`}>
              <ul className="list">
                {rank.data.excluded.map((e) => (
                  <li key={e.person_id} className="list-row">
                    <div className="row-main">
                      <div className="row-title">{e.name}</div>
                      <div className="row-sub">
                        {EXCLUSION_LABEL[e.reason] ?? humanize(e.reason)}
                        {e.detail && `: ${e.detail.split(", ").map((d) => options.data?.credentials.find((c) => c.id === d)?.label ?? humanize(d)).join(", ")}`}
                      </div>
                    </div>
                    <Pill tone={e.reason === "do_not_contact" || e.reason === "expired_credential" ? "red" : "gray"}>{e.reason === "do_not_contact" ? "Do not contact" : e.reason === "expired_credential" ? "Expired" : "Not eligible"}</Pill>
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </>
      )}
    </>
  );
}

// ---------- Verification policy ----------

function PolicyCard() {
  const isAdmin = useCan("admin");
  const qc = useQueryClient();
  const toast = useToast();
  const options = useOptions();
  const current = useQuery({ queryKey: ["people-policy"], queryFn: () => api.get<Policy>("/people/policy") });
  const [draft, setDraft] = useState<Policy | null>(null);
  const policy = draft ?? current.data;
  const save = useMutation({
    mutationFn: () => api.put<Policy>("/people/policy", draft),
    onSuccess: (p) => {
      qc.setQueryData(["people-policy"], p);
      setDraft(null);
      toast(`Saved version ${p.version}`);
    },
  });
  if (current.isPending || options.isPending) return <ListSkeleton rows={3} icon={false} />;
  if (current.error) return <ErrorState error={current.error} onRetry={() => current.refetch()} />;
  if (!policy || !options.data) return null;
  const set = (patch: Partial<Policy>) => setDraft({ ...policy, ...patch });
  const pick = (key: "identifiers" | "step_up_identifiers" | "proxy_subject_identifiers" | "step_up_topics", items: Option[], label: string, help: string) => (
    <div className="field">
      <span className="field-label">{label}</span>
      <span className="field-help">{help}</span>
      <div className="stack" style={{ gap: 4 }}>
        {items.map((o) => (
          <label key={o.id} className="checkbox-row">
            <input type="checkbox" disabled={!isAdmin} checked={policy[key].includes(o.id)} onChange={(e) => set({ [key]: e.target.checked ? [...policy[key], o.id] : policy[key].filter((x) => x !== o.id) })} />
            <span>{o.label}</span>
          </label>
        ))}
      </div>
    </div>
  );
  return (
    <Card
      title="Verification and representative policy"
      subtitle={`Version ${policy.version}. Every check an agent makes is kept as evidence on the person's profile.`}
      actions={
        isAdmin && (
          <button className="btn btn-primary" disabled={!draft || save.isPending} onClick={() => save.mutate()}>
            {save.isPending ? "Saving…" : "Save policy"}
          </button>
        )
      }
    >
      <div className="stack card-pad" style={{ gap: 18 }}>
        {!isAdmin && <Banner tone="blue" title="Only an administrator can change this policy." />}
        <div className="grid-2">
          {pick("identifiers", options.data.identifiers, "Every caller must match", "All of these, at least two. Calling from the phone on file can't be one of the two.")}
          {pick("step_up_identifiers", options.data.identifiers, "Extra verification: any one of", "Asked before a sensitive topic is discussed.")}
          {pick("step_up_topics", options.data.topics, "Topics that need extra verification", "The agent asks for one more identifier before reading or sharing these.")}
          {pick("proxy_subject_identifiers", options.data.identifiers, "A representative must also give the person's", "Checked before their authorisation is looked up.")}
        </div>
        <label className="field" style={{ maxWidth: 260 }}>
          <span className="field-label">Attempts per call</span>
          <select className="select" disabled={!isAdmin} value={policy.max_attempts} onChange={(e) => set({ max_attempts: Number(e.target.value) })}>
            {[1, 2, 3].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        {save.error && <Banner tone="red" title={save.error instanceof ApiError ? save.error.message : "Couldn't save"} />}
      </div>
    </Card>
  );
}
