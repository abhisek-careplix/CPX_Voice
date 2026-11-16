// Onboarding a clinic: add a NexHealth practice, follow its setup, run the go-live checklist and
// choose the workspace's automation settings. Shown inside the EHR hub.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CircleAlert,
  CircleCheck,
  Loader,
  Plus,
  RotateCw,
  Rocket,
} from "lucide-react";
import { useState } from "react";
import {
  api,
  ApiError,
  type AutomationSettings,
  type AutomationValues,
  type GoLiveItem,
  type HubProfile,
  type PracticeSetup,
  type SetupOptions,
} from "../api";
import { Banner, Card, Pill, Segmented, Sheet, Switch, useToast } from "../ui";

const STEP_LABEL: Record<string, string> = {
  created: "Practice added",
  verified: "NexHealth reaches the location",
  webhooks: "Signed webhook registered",
  first_sync: "First full sync",
};

function Mark({ ok }: { ok: boolean | null }) {
  if (ok === true)
    return <CircleCheck size={16} color="var(--green)" aria-label="Done" />;
  if (ok === false)
    return (
      <CircleAlert size={16} color="var(--red)" aria-label="Needs attention" />
    );
  return <Loader size={16} color="var(--label-3)" aria-label="Waiting" />;
}

function Row({
  label,
  ok,
  detail,
}: {
  label: string;
  ok: boolean | null;
  detail?: string | null;
}) {
  return (
    <li className="list-row" style={{ alignItems: "flex-start" }}>
      <Mark ok={ok} />
      <div className="row-main">
        <div className="row-title">{label}</div>
        {detail && <div className="row-sub">{detail}</div>}
      </div>
    </li>
  );
}

function invalidate(qc: ReturnType<typeof useQueryClient>) {
  for (const key of ["hub-catalog", "practice-setups", "practice"])
    qc.invalidateQueries({ queryKey: [key] });
}

/** The Add practice sheet. `profile` preselects the EHR picked in the catalog. */
export function AddPracticeSheet({
  profile,
  onClose,
  onAdded,
}: {
  profile?: HubProfile | null;
  onClose: () => void;
  onAdded: (id: string) => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const options = useQuery({
    queryKey: ["setup-options"],
    queryFn: () => api.get<SetupOptions>("/practices/setup/options"),
  });
  const [form, setForm] = useState({
    profile: profile?.path === "nexhealth" ? profile.id : "",
    subdomain: "",
    location_id: "",
    env: "sandbox",
    name: "",
  });
  const add = useMutation({
    mutationFn: () =>
      api.post<PracticeSetup>("/practices/setup", {
        ...form,
        name: form.name || null,
      }),
    onSuccess: (r) => {
      invalidate(qc);
      toast(
        r.onboarding.steps.some((s) => s.ok === false)
          ? "Practice added; a step needs attention"
          : "Practice added; the first sync is running",
      );
      onAdded(r.connection.connection_id);
    },
  });
  const o = options.data;
  const groups: HubProfile["category"][] = ["medical", "dental", "ortho"];
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) =>
    setForm({ ...form, [k]: e.target.value });
  const ready =
    form.profile &&
    form.subdomain.trim() &&
    /^\d+$/.test(form.location_id.trim());

  return (
    <Sheet
      title="Add a practice"
      subtitle="Connect a clinic's EHR through NexHealth."
      onClose={onClose}
      footer={
        <button
          className="btn btn-primary"
          disabled={!ready || add.isPending}
          onClick={() => add.mutate()}
        >
          <Plus size={15} aria-hidden />{" "}
          {add.isPending ? "Connecting…" : "Add and verify"}
        </button>
      }
    >
      <div className="card-pad" style={{ display: "grid", gap: 14 }}>
        <p className="small muted" style={{ margin: 0 }}>
          The practice first authorises CarePlix in NexHealth. Then copy the
          subdomain and location id NexHealth shows for it. CarePlix checks the
          location, registers a signed webhook and runs the first full sync.
        </p>
        {o && !o.webhooks_ready && (
          <Banner
            tone="orange"
            title="Webhooks aren't set up in this environment."
          >
            Changes arrive only with the next sync and writes can't be
            confirmed, so a real clinic can't go live here.
          </Banner>
        )}
        <label className="field">
          <span className="field-label">EHR</span>
          <select
            className="input"
            value={form.profile}
            onChange={set("profile")}
          >
            <option value="">Choose the practice's EHR</option>
            {groups.map((g) => (
              <optgroup
                key={g}
                label={
                  g === "ortho"
                    ? "Orthodontic"
                    : g[0].toUpperCase() + g.slice(1)
                }
              >
                {(o?.profiles ?? [])
                  .filter((p) => p.category === g)
                  .map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
              </optgroup>
            ))}
          </select>
          {form.profile && (
            <span className="field-help">
              {o?.profiles.find((p) => p.id === form.profile)?.connects_by}
            </span>
          )}
        </label>
        <label className="field">
          <span className="field-label">NexHealth subdomain</span>
          <input
            className="input"
            value={form.subdomain}
            onChange={set("subdomain")}
            placeholder="e.g. absolute-health"
            autoComplete="off"
          />
        </label>
        <label className="field">
          <span className="field-label">Location id</span>
          <input
            className="input"
            value={form.location_id}
            onChange={set("location_id")}
            inputMode="numeric"
            placeholder="e.g. 312345"
            autoComplete="off"
          />
        </label>
        <div className="field">
          <span className="field-label">Environment</span>
          <Segmented
            label="Environment"
            value={form.env as "sandbox" | "production"}
            onChange={(v) => setForm({ ...form, env: v })}
            options={(
              o?.envs ?? [{ env: "sandbox", available: true, reason: null }]
            )
              .filter((e) => e.available)
              .map((e) => ({
                value: e.env,
                label: e.env === "sandbox" ? "Sandbox" : "Production",
              }))}
          />
          {o?.envs
            .filter((e) => !e.available)
            .map((e) => (
              <span key={e.env} className="field-help">
                {e.env === "production" ? "Production" : "Sandbox"}: {e.reason}
              </span>
            ))}
        </div>
        <label className="field">
          <span className="field-label">Name (optional)</span>
          <input
            className="input"
            value={form.name}
            onChange={set("name")}
            placeholder="e.g. Absolute Health · Main St"
            maxLength={80}
          />
        </label>
        {add.error && (
          <div className="field-error">{(add.error as ApiError).message}</div>
        )}
      </div>
    </Sheet>
  );
}

/** Setup progress and the go-live checklist for one practice. */
export function PracticeSetupSheet({
  id,
  isAdmin,
  onClose,
}: {
  id: string;
  isAdmin: boolean;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const [patient, setPatient] = useState("");
  const setup = useQuery({
    queryKey: ["practice-setups", id],
    queryFn: () => api.get<PracticeSetup>(`/practices/setup/${id}`),
    // Poll while something runs in the background (first sync, a go-live test).
    refetchInterval: (q) => {
      const d = q.state.data;
      const running =
        d &&
        (d.onboarding.steps.some((s) => s.ok === null) ||
          ["book", "confirm", "cancel"].includes(d.golive.test?.step ?? ""));
      return running ? 5000 : false;
    },
  });
  const retry = useMutation({
    mutationFn: () => api.post<PracticeSetup>(`/practices/setup/${id}/retry`),
    onSuccess: () => {
      invalidate(qc);
      toast("Checked again");
    },
  });
  const test = useMutation({
    mutationFn: () =>
      api.post<PracticeSetup>(`/practices/setup/${id}/golive-test`, {
        patient_id: patient.trim(),
      }),
    onSuccess: () => {
      invalidate(qc);
      toast(
        "Test visit booked; confirm and cancel follow when the EHR confirms each step",
      );
    },
  });
  const d = setup.data;
  const g = d?.golive;
  const testRunning = ["book", "confirm", "cancel"].includes(
    g?.test?.step ?? "",
  );

  return (
    <Sheet
      wide
      title={d?.connection.name ?? "Practice"}
      subtitle={
        d
          ? `${d.connection.profile_name ?? "EHR not reported yet"} · ${d.connection.env ?? ""} · ${d.connection.subdomain ?? ""} / ${d.connection.location_id ?? ""}`
          : undefined
      }
      onClose={onClose}
      footer={
        isAdmin &&
        d?.connection.kind === "nexhealth" &&
        d.connection.source === "studio" ? (
          <button
            className="btn btn-secondary"
            disabled={retry.isPending}
            onClick={() => retry.mutate()}
          >
            <RotateCw size={15} aria-hidden />{" "}
            {retry.isPending ? "Checking…" : "Verify and register again"}
          </button>
        ) : undefined
      }
    >
      {setup.error && (
        <Banner tone="red" title={(setup.error as ApiError).message} />
      )}
      {retry.error && (
        <Banner tone="red" title={(retry.error as ApiError).message} />
      )}
      {d && g && (
        <div className="card-pad" style={{ display: "grid", gap: 16 }}>
          {d.onboarding.steps.length > 0 && (
            <section>
              <div className="small muted" style={{ marginBottom: 4 }}>
                Setup
              </div>
              <ul className="list">
                {d.onboarding.steps.map((s) => (
                  <Row
                    key={s.key}
                    label={STEP_LABEL[s.key] ?? s.key}
                    ok={s.ok}
                    detail={s.detail}
                  />
                ))}
              </ul>
            </section>
          )}
          <section>
            <div className="row" style={{ gap: 8, marginBottom: 4 }}>
              <span className="small muted" style={{ flex: 1 }}>
                Go-live checklist
              </span>
              {g.required ? (
                g.automations_allowed ? (
                  <Pill tone="green">Live: automations on</Pill>
                ) : (
                  <Pill tone="orange">
                    Automations off until every item passes
                  </Pill>
                )
              ) : (
                <Pill tone="blue">Not required (synthetic data)</Pill>
              )}
            </div>
            <ul className="list">
              {g.items.map((i: GoLiveItem) => (
                <Row key={i.key} label={i.label} ok={i.ok} detail={i.detail} />
              ))}
            </ul>
            {(g.test?.notes ?? []).length > 0 && (
              <ul
                className="small"
                style={{ margin: "8px 0 0", paddingLeft: 18 }}
              >
                {g.test!.notes!.map((n) => (
                  <li key={n}>{n}</li>
                ))}
              </ul>
            )}
          </section>
          {isAdmin && (
            <section style={{ display: "grid", gap: 8 }}>
              <div className="small muted">
                Test writes on the practice's own test patient
              </div>
              <p className="small" style={{ margin: 0 }}>
                Ask the practice for its test patient's NexHealth id (a chart
                named “Test”). CarePlix books a visit at least a day out,
                confirms it, then cancels it, waiting each time for the EHR to
                confirm.
                {d.connection.profile &&
                  " Cloud EHRs confirm only while the office is signed in."}
              </p>
              <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                <input
                  className="input"
                  style={{ maxWidth: 220 }}
                  value={patient}
                  onChange={(e) => setPatient(e.target.value)}
                  placeholder="Test patient id"
                  inputMode="numeric"
                  aria-label="Test patient id"
                />
                <button
                  className="btn btn-primary"
                  disabled={!patient.trim() || test.isPending || testRunning}
                  onClick={() => test.mutate()}
                >
                  <Rocket size={15} aria-hidden />{" "}
                  {testRunning
                    ? `Running: ${g.test?.step}`
                    : g.test?.step === "done"
                      ? "Run the test again"
                      : "Run the go-live test"}
                </button>
              </div>
              {test.error && (
                <div className="field-error">
                  {(test.error as ApiError).message}
                </div>
              )}
            </section>
          )}
          <SyncInterval id={id} setup={d} isAdmin={isAdmin} />
        </div>
      )}
    </Sheet>
  );
}

const INTERVALS = [
  { value: "", label: "Workspace setting" },
  { value: "300", label: "Every 5 minutes" },
  { value: "900", label: "Every 15 minutes" },
  { value: "3600", label: "Every hour" },
  { value: "0", label: "Webhooks only" },
];

function minutes(s: number) {
  return s === 0
    ? "webhooks only"
    : s < 3600
      ? `every ${Math.round(s / 60)} min`
      : `every ${Math.round(s / 3600)} h`;
}

function SyncInterval({
  id,
  setup,
  isAdmin,
}: {
  id: string;
  setup: PracticeSetup;
  isAdmin: boolean;
}) {
  const qc = useQueryClient();
  const save = useMutation({
    mutationFn: (v: string) =>
      api.put<PracticeSetup>(`/practices/setup/${id}/sync-interval`, {
        sync_interval_s: v === "" ? null : Number(v),
      }),
    onSuccess: () => invalidate(qc),
  });
  return (
    <label className="field" style={{ maxWidth: 320 }}>
      <span className="field-label">Sync this practice</span>
      <select
        className="input"
        disabled={!isAdmin || save.isPending}
        value={
          setup.sync_interval_s.own === null
            ? ""
            : String(setup.sync_interval_s.own)
        }
        onChange={(e) => save.mutate(e.target.value)}
      >
        {INTERVALS.map((i) => (
          <option key={i.value} value={i.value}>
            {i.label}
          </option>
        ))}
      </select>
      <span className="field-help">
        Now {minutes(setup.sync_interval_s.effective)}. Webhooks bring changes
        in between; each NexHealth call is billed.
      </span>
    </label>
  );
}

/** Workspace automation settings (env values are the defaults). */
export function AutomationCard({ isAdmin }: { isAdmin: boolean }) {
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery({
    queryKey: ["practice-automation"],
    queryFn: () => api.get<AutomationSettings>("/practices/automation"),
  });
  const save = useMutation({
    mutationFn: (body: Partial<Record<keyof AutomationValues, unknown>>) =>
      api.put<AutomationSettings>("/practices/automation", body),
    onSuccess: (r) => {
      qc.setQueryData(["practice-automation"], r);
      invalidate(qc);
      toast("Saved");
    },
  });
  const d = q.data;
  if (!d) return null;
  const e = d.effective;
  const disabled = !isAdmin || save.isPending;
  return (
    <Card
      title="Automation settings"
      subtitle="For this workspace. A real clinic's automations also wait for its go-live checklist."
    >
      <ul className="list">
        <li className="list-row">
          <div className="row-main">
            <div className="row-title">Cancellations from the receptionist</div>
            <div className="row-sub">
              Approval: each cancel waits for one click in the write queue.
              Automatic: cancelled on the call, and the time goes to backfill.
            </div>
          </div>
          <Segmented
            label="Cancellations"
            value={e.receptionist_cancel_mode}
            onChange={(v) =>
              !disabled && save.mutate({ receptionist_cancel_mode: v })
            }
            options={[
              { value: "approval", label: "Approval" },
              { value: "auto", label: "Automatic" },
            ]}
          />
        </li>
        <li className="list-row">
          <div className="row-main">
            <div className="row-title">Cancellation backfill</div>
            <div className="row-sub">
              Offer freed times to patients on the ASAP list.
            </div>
          </div>
          <Switch
            label="Cancellation backfill"
            checked={e.backfill_enabled}
            disabled={disabled}
            onChange={(v) => save.mutate({ backfill_enabled: v })}
          />
        </li>
        <li className="list-row">
          <div className="row-main">
            <div className="row-title">Eligibility autopilot</div>
            <div className="row-sub">
              Check insurance 72 and 24 hours before every visit.
            </div>
          </div>
          <Switch
            label="Eligibility autopilot"
            checked={e.eligibility_autopilot}
            disabled={disabled}
            onChange={(v) => save.mutate({ eligibility_autopilot: v })}
          />
        </li>
        <li className="list-row">
          <div className="row-main">
            <div className="row-title">Default sync interval</div>
            <div className="row-sub">Each practice can set its own.</div>
          </div>
          <select
            className="input"
            style={{ maxWidth: 200 }}
            disabled={disabled}
            value={String(e.sync_interval_s)}
            onChange={(ev) =>
              save.mutate({ sync_interval_s: Number(ev.target.value) })
            }
          >
            {[
              ...new Set([
                String(e.sync_interval_s),
                "300",
                "900",
                "3600",
                "0",
              ]),
            ].map((v) => (
              <option key={v} value={v}>
                {minutes(Number(v))}
              </option>
            ))}
          </select>
        </li>
        <li className="list-row">
          <div className="row-main">
            <div className="row-title">Clearinghouse</div>
            <div className="row-sub">
              {d.clearinghouse_production_available
                ? "Test mode checks mock payers only; production sends real checks."
                : "Test mode only in this environment."}
            </div>
          </div>
          <Segmented
            label="Clearinghouse mode"
            value={e.clearinghouse_env}
            onChange={(v) =>
              !disabled &&
              (v === "test" || d.clearinghouse_production_available) &&
              save.mutate({ clearinghouse_env: v })
            }
            options={[
              { value: "test", label: "Test" },
              { value: "production", label: "Production" },
            ]}
          />
        </li>
      </ul>
      {save.error && (
        <div className="card-pad field-error">
          {(save.error as ApiError).message}
        </div>
      )}
    </Card>
  );
}
