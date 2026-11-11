// Typed client for the CarePlix Studio API. Session cookie is HttpOnly and same-origin.

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public detail?: { issues?: Issue[] } & Record<string, unknown>,
    public requestId?: string,
  ) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown, headers?: Record<string, string>): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method,
    credentials: "same-origin",
    headers: { ...(body !== undefined ? { "Content-Type": "application/json" } : {}), ...headers },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    let payload: { error?: { code: string; message: string; detail?: never; request_id?: string } } = {};
    try {
      payload = await response.json();
    } catch {
      /* non-JSON error */
    }
    const e = payload.error;
    throw new ApiError(response.status, e?.code ?? "http_error", e?.message ?? response.statusText, e?.detail, e?.request_id);
  }
  return response.json() as Promise<T>;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown, headers?: Record<string, string>) => request<T>("POST", path, body ?? {}, headers),
  put: <T>(path: string, body: unknown) => request<T>("PUT", path, body),
  del: <T>(path: string) => request<T>("DELETE", path),
};

// ---------- Types (mirror apps/api views) ----------

export interface Me {
  user_id: string;
  display_name: string;
  role: string;
  workspace_id: string;
  workspace_name: string;
  permissions: string[];
  emergency_stop: boolean;
}

export type CheckStatus = "ready" | "blocked" | "unverified";
export interface Check {
  name: string;
  status: CheckStatus;
  message: string;
  detail: Record<string, unknown>;
}
export interface Readiness {
  checked_at: string;
  overall: CheckStatus;
  checks: Check[];
}

export interface Issue {
  field: string;
  message: string;
}

export interface VersionRef {
  id: string;
  number: number;
  state: "draft" | "published" | "retired";
  config_hash: string | null;
  change_note: string | null;
  updated_at: string;
  published_at: string | null;
  policy_version: string | null;
  suite?: SuiteResult;
}

export interface AgentConfig {
  identity: { name: string; role: string; language: string; greeting: string; automated_identity_disclosure: string };
  task: { template_id: string; template_version: number; objective: string; required_context: string[]; output_schema_id: string };
  conversation: {
    pipeline: "native_audio";
    voice: string;
    silence_timeout_s?: number;
    hold_policy?: { mode?: "suspend_model" | "keep_model"; max_hold_s?: number };
    ivr_policy?: { max_menu_repeats?: number; wait_for_full_menu?: boolean };
  };
  tools: { name: string; version: string }[];
  guardrails: { allowed_disclosures?: string[]; escalation_triggers?: string[]; max_duration_s: number };
  model: { provider: "google_vertex"; model_id: string; connection_id: string };
  outcome: { success_conditions: string[]; critical_fields: string[] };
  instructions: { agent_text: string };
  persona?: PersonaStyle;
}

/** VS4: how an agent sounds. Style only; never tools, disclosures or authority. */
export interface PersonaStyle {
  source_id?: string;
  source_name?: string;
  warmth?: "warm" | "neutral" | "brisk";
  formality?: "formal" | "neutral" | "casual";
  pace?: "slow" | "normal" | "fast";
  empathy?: "standard" | "high";
  languages?: string[];
  avoid?: string[];
  signature_phrases?: string[];
  character?: string;
}

export interface Persona extends Required<Pick<PersonaStyle, "warmth" | "formality" | "pace" | "empathy" | "languages" | "avoid" | "signature_phrases">> {
  id: string;
  name: string;
  description: string | null;
  agent_name: string;
  role: string;
  voice: string;
  greeting: string;
  character: string | null;
  config: { identity: { name: string; role: string; greeting: string }; voice: string; persona: PersonaStyle };
  updated_at: string;
}

export interface PersonaLibrary {
  personas: Persona[];
  voices: string[];
  languages: { code: string; label: string }[];
}

export interface AgentSummary {
  id: string;
  name: string;
  template: { id: string; name: string; direction: "outbound" | "inbound" };
  draft: VersionRef | null;
  current: VersionRef | null;
  updated_at: string | null;
}

export interface AgentDetail extends AgentSummary {
  versions: VersionRef[];
  draft_config: AgentConfig | null;
  draft_issues: Issue[];
  approved_model: string | null;
}

export interface Template {
  id: string;
  version: number;
  name: string;
  description: string;
  direction: "outbound" | "inbound";
  objective: string;
  output_fields: Record<string, string>;
  disclose_allowlist: string[];
  required_context: string[];
  tools: string[];
}

export interface Tool {
  name: string;
  version: string;
  description: string;
  classification: "read" | "write" | "call_control";
  execution: string;
  release: "D" | "P" | "S";
  enabled_in_demo: boolean;
  limits?: Record<string, unknown>;
}

export interface PublishPreview {
  draft_version: number;
  current_version: number | null;
  issues: Issue[];
  changes: { path: string; before: unknown; after: unknown }[];
  privilege_changes: { tools_added: string[]; tools_removed: string[]; disclosures_added: string[]; disclosures_removed: string[] };
  affected_routes: { id: string; name: string; number: string; direction: string }[];
  evaluation: SuiteResult & { required: boolean; untested: string[] };
}

/** A version's regression-suite result. "outdated": the draft changed after the run. */
export interface SuiteResult {
  status: "not_run" | "passed" | "failed" | "outdated";
  run_id?: string;
  passed?: number;
  total?: number;
  repeats?: number;
  finished_at?: string;
}

/** A Voice Studio scenario's pass rules, checked from the run's record before the AI judge. */
export interface ScenarioExpect {
  outcome: "achieved" | "achieved_or_partly" | "any";
  findings: { field: string; pattern: string | null }[];
  max_duration_s: number | null;
  max_menu_presses: number | null;
  no_refused_actions: boolean;
}

export type SuiteRunState = "queued" | "running" | "passed" | "failed" | "stale" | "cancelled";

export interface SuiteScenarioResult {
  scenario_id: string;
  name: string;
  role: "candidate" | "baseline";
  passes: number;
  runs: number;
  required: number;
  status: "pass" | "flaky" | "fail" | "error";
}

export interface SuiteCase {
  id: string;
  scenario_id: string;
  role: "candidate" | "baseline";
  repeat: number;
  state: "queued" | "running" | "judging" | "done" | "cancelled";
  verdict: "pass" | "fail" | "error" | null;
  attempt_id: string | null;
  rules: { rule: string; ok: boolean; detail: string }[];
  judge: { state: string; verdict?: string | null; score?: number | null; summary?: string | null; error?: string | null } | null;
  error: string | null;
}

export interface SuiteRun {
  id: string;
  agent_id: string;
  version: { number: number; state: string };
  baseline_version: number | null;
  repeats: number;
  required_passes: number;
  state: SuiteRunState;
  scenarios: { id: string; name: string }[];
  summary: SuiteScenarioResult[] | null;
  error: string | null;
  created_at: string;
  finished_at: string | null;
  progress?: { done: number; total: number };
  live_summary?: SuiteScenarioResult[];
  cases?: SuiteCase[];
}

export interface AgentSuite {
  scenarios: { id: string; name: string; category: string; expect: ScenarioExpect; checks: number }[];
  required_to_publish: boolean;
  draft: SuiteResult | null;
  published: SuiteResult | null;
  runs: SuiteRun[];
  max_repeats: number;
}

export interface CaseItem {
  id: string;
  case_ref: string;
  synthetic: boolean;
  context: Record<string, string | number | null>;
}

export interface Route {
  id: string;
  name: string;
  provider: string;
  direction: string;
  number: string;
  agent_id: string | null;
  capabilities: Record<string, boolean>;
  verified_at: string | null;
  provider_refs: Record<string, unknown>;
  last_call_at: string | null;
}

export type AttemptState = "Created" | "Queued" | "Dialing" | "Connected" | "Ending" | "Ended" | "Failed" | "Cancelled";

export interface Attempt {
  id: string;
  task_id: string | null;
  direction: "outbound" | "inbound" | "browser";
  sandbox: boolean;
  state: AttemptState;
  substate: string | null;
  disposition: string | null;
  end_reason: string | null;
  destination: string | null;
  caller_reported: string | null;
  agent: { id: string; name: string; version: number; version_state: string };
  case_ref: string | null;
  pins: Record<string, string | null>;
  created_at: string;
  connected_at: string | null;
  ended_at: string | null;
}

export interface Finding {
  id: string;
  attempt_id: string;
  field: string;
  value: string | null;
  status: "provisional" | "confirmed_by_counterpart" | "system_verified" | "reviewed" | "conflicting";
  source: "call" | "system";
  integration_call_id: string | null;
  evidence_segment_ids: string[];
  original_phrasing: string | null;
  null_reason: string | null;
  created_at: string;
  revisions: { prior_value: string | null; new_value: string | null; prior_status: string; new_status: string; reason: string; actor: string; at: string }[];
}

export interface Segment {
  id: string;
  attempt_id: string;
  speaker: "agent" | "counterpart";
  text: string;
  playout: string;
  at: string;
}

/** What a run's agent was told at claim: persona at its pinned version, who it acted for, its
 * objective and the facts it could share ("•••" for people who can't edit agents). */
export interface ToldBrief {
  recorded: boolean;
  persona: { name: string | null; greeting: string | null };
  acting_for: string | null;
  objective: string | null;
  scenario_id: string | null;
  facts: Record<string, string | number | null>;
}

export interface CallDetail extends Attempt {
  told: ToldBrief;
  findings: Finding[];
  transcript: Segment[];
  tools: { tool: string; state: string; rejection_code: string | null; at: string }[];
}

export type TaskState = "Draft" | "Ready" | "Running" | "Review" | "Completed" | "Partial" | "Unsuccessful" | "Cancelled";

export interface Task {
  id: string;
  kind: "outbound" | "inbound";
  state: TaskState;
  objective: string;
  additional_instructions: string | null;
  destination: string | null;
  agent: { id: string; name: string; version: number };
  case: { id: string; case_ref: string; synthetic: boolean } | null;
  outcome_reason: string | null;
  next_action: string | null;
  electronic_check: ElectronicCheck | null;
  created_at: string;
  updated_at: string;
}

export interface TaskResult {
  task: Task;
  attempts: Attempt[];
  findings: Finding[];
  transcript: Segment[];
  expected_fields: Record<string, string>;
  critical_fields: string[];
}

export interface WorkspaceSummary {
  active_calls: Attempt[];
  needs_attention: Task[];
  recent_outcomes: Task[];
  agents: { total: number; published: number };
}

export interface BrowserSession {
  attempt_id: string;
  url: string;
  token: string;
  version: { number: number; state: string };
  case_ref: string | null;
  /** Who the agent says it represents: the scenario's party, the case's provider, or nobody. */
  acting_for: string;
  persona: string;
  /** A simulation: hear both agents, microphone off. */
  listen_only: boolean;
}

export interface CallEvent {
  seq: number;
  type: string;
  source: string;
  occurred_at: string;
  payload: Record<string, unknown>;
}

export interface LiveKitInventory {
  active_rooms: number;
  outbound_trunks: { id: string; name: string; address: string; numbers: string[]; has_auth: boolean }[];
  inbound_trunks: { id: string; name: string; numbers: string[] }[];
  dispatch_rules: { id: string; name: string; numbers: string[]; trunk_ids: string[]; agents: string[] }[];
}

export interface ProbeStatus {
  id: string;
  status: "waiting" | "answered" | "failed" | "no_worker";
  created_at: string;
  acked_at: string | null;
  runtime_build: string | null;
  error: string | null;
  latency_ms: number | null;
}

export interface ModelCheck {
  model_id: string;
  location: string;
  ok: boolean;
  connect_ms: number | null;
  first_audio_ms: number | null;
  reply_transcript: string | null;
  resumption_handle_received: boolean;
  error: string | null;
  error_detail: string | null;
  at: string;
}

export interface ConnectionsOverview {
  livekit: { configured: boolean; project: string | null; agent_name: string; inventory?: LiveKitInventory; error?: string };
  outbound: {
    trunk_id: string | null;
    trunk_from_env: boolean;
    verified: boolean;
    verified_at: string | null;
    evidence_attempt_id: string | null;
    approved_destinations: string[];
    env_destinations: string[];
  };
  model: {
    approved_model: string | null;
    gcp_project: string | null;
    vertex_location: string | null;
    known_models: string[];
    check: ModelCheck | null;
    verified: boolean;
    credential_source: string;
  };
  worker_probe: ProbeStatus | null;
  sandbox: SandboxLine;
}

/** The shared CarePlix sandbox line for trials: call it and key in the workspace code. */
export interface SandboxLine {
  available: boolean;
  number: string | null;
  code: string | null;
  agent_id: string | null;
  outbound_available: boolean;
  outbound: boolean;
  calls_today: number;
  daily_limit: number;
  max_call_s: number;
  max_destinations: number;
}

export interface PasStatus {
  found: boolean;
  outcome: string | null;
  review_action: string | null;
  reported_status: string;
  final: boolean;
  authorization_reference: string | null;
  reason: string | null;
  synthetic: boolean;
}

export interface CheckPhase {
  state: "pending" | "done" | "failed" | "skipped";
  at?: string;
  queued_at?: string;
  reason?: string;
  connection?: { id: string; name: string; synthetic: boolean };
  integration_call_id?: string;
  duration_ms?: number;
  error?: string | null;
  status?: PasStatus | null;
  comparison?: Record<string, "match" | "conflict" | "call_missing" | "system_missing">;
}

export interface ReadinessCheck {
  capability: string;
  status: "confirmed" | "invalid" | "unverifiable" | "skipped" | "error";
  subject: string;
  detail: { display?: string; reason?: string | null; suggestions?: string[]; name?: string; taxonomy?: string; policies?: { id: string; type: string; title: string; url: string; contractor?: string }[] };
  duration_ms: number;
  source: string;
  system?: string;
}

export interface ReadinessPhase {
  state: "pending" | "done" | "skipped";
  at?: string;
  reason?: string;
  checks?: ReadinessCheck[];
  summary?: Record<string, number>;
}

export type ElectronicCheck = Partial<Record<"pre_call" | "post_call" | "manual", CheckPhase>> & { readiness?: ReadinessPhase };

export interface PayerConnection {
  id: string;
  name: string;
  kind: string;
  base_url: string;
  ig_version: string;
  auth_type: "none" | "smart_backend" | "google_id_token" | "api_key";
  auth_ref: Record<string, string>;
  payer_names: string[];
  synthetic: boolean;
  enabled: boolean;
  verified_at: string | null;
  capability: { fhir_version?: string; software?: string; operations?: string[]; synthetic?: boolean } | null;
  last_error: string | null;
}

export interface IntegrationCallRow {
  id: string;
  connection_id: string;
  operation: string;
  phase: string | null;
  task_id: string | null;
  ok: boolean;
  status: string;
  http_status: number | null;
  duration_ms: number;
  summary: Record<string, unknown>;
  error: string | null;
  at: string;
}

export interface IntegrationsOverview {
  connections: PayerConnection[];
  recent_calls: IntegrationCallRow[];
  supported: { kind: string; ig_version: string; operations: string[]; status: string }[];
}

// ---------- Practice layer (NexHealth + Stedi) ----------

export interface PracticeConnection {
  id: string;
  kind: "nexhealth" | "openemr" | "stedi";
  name: string;
  env: string | null;
  subdomain?: string | null;
  location_id?: string | null;
  enabled: boolean;
  verified_at: string | null;
  last_error: string | null;
  capability: {
    location?: { id: string; name: string } | null;
    emrs?: string[];
    reads?: Record<string, { ok: boolean; has_data?: boolean; http_status?: number; error?: string }>;
    sync?: { emr: string | null; read_status: string | null; write_status: string | null }[];
    writes_confirmable?: boolean;
    writes?: Record<string, string>;
    last_sync?: { at: string; full: boolean; api_calls: number };
    probed_at?: string;
  } | null;
  webhook: {
    registered: boolean;
    events: string[];
    signed?: boolean;
    health?: { status: string; message?: string; checked_at?: string } | null;
  };
}

export interface WriteIntentRow {
  id: string;
  kind: string;
  level: string;
  target_resource: string;
  target_id: string;
  reason: string;
  source: string;
  state: "proposed" | "approved" | "sending" | "accepted" | "written" | "needs_review" | "manual" | "rejected" | "failed";
  confirmation: string | null;
  remote_id: string | null;
  last_error: string | null;
  created_at: string;
  resolved_at: string | null;
}

export interface FrontDeskMessage {
  id: string;
  reference: string;
  topic: "refill" | "records" | "billing" | "scheduling" | "new_patient" | "insurance" | "other";
  callback_requested: boolean;
  verified: boolean;
  patient_id: string | null;
  patient: string | null;
  caller_name: string | null;
  caller_phone: string | null;
  message: string | null; // null in the list; opened (and audited) on demand
  attempt_id: string | null;
  state: "open" | "done";
  created_at: string;
  handled_at: string | null;
}

export interface PracticeOverview {
  ehr: PracticeConnection | null;
  practices: { id: string; kind: string; name: string; env: string | null }[];
  links: { calendar: string; patient: string; home: string } | null;
  clearinghouse: PracticeConnection | null;
  location: { name: string; institution_name: string; emrs: string[] } | null;
  mirror: Record<string, number>;
  cursors: { resource: string; rows: number; api_calls: number; last_run_at: string | null; last_error: string | null }[];
  api_calls_total: number;
  webhooks: { public_url_set: boolean; received: number; pending: number; last_at: string | null };
  eligibility: { autopilot: boolean; states: Record<string, number>; coverage: Record<string, number> };
  intents: WriteIntentRow[];
}

export interface Accumulator {
  total: string | null;
  remaining: string | null;
}

export interface CoverageRedirect {
  kind: "medicare_advantage" | "managed_care" | "contact_payer";
  name: string | null;
  payer_id: string | null;
  phone: string | null;
  bill_option?: string;
  contract?: string | null;
}

export interface EligibilitySummary {
  coverage: "active" | "inactive" | "unknown" | "error" | "none_on_file" | "payer_unmapped" | "invalid_mbi";
  plan?: string | null;
  plan_dates?: { start: string | null; end: string | null } | null;
  ehr_plan?: string;
  office_copay?: string | null;
  office_copay_out_of_network?: string | null;
  office_coinsurance_pct?: string | null;
  deductible?: Accumulator;
  deductible_family?: Accumulator;
  deductible_out_of_network?: Accumulator;
  out_of_pocket?: Accumulator;
  auth_required_services?: string[];
  payer?: string | null;
  medicare?: { advantage: boolean; bill_option: string | null; secondary: string[]; mbi_returned: boolean; qmb?: boolean; part_d?: string | null };
  medicaid?: { cost_containment: string | null; spend_down: string | null; card_reported_stolen: boolean };
  redirect?: CoverageRedirect | null;
  error_class?: "retry" | "correct" | "not_found" | null;
  message?: string | null;
  mock?: boolean;
}

export interface EligibilityView {
  id: string;
  trigger: string;
  state: "pending" | "done" | "failed" | "skipped";
  mock: boolean;
  summary: EligibilitySummary | null;
  reason: string | null;
  reused: boolean;
  attempts: number;
  next_attempt_at: string | null;
  completed_at: string | null;
}

export interface EligibilitySettings {
  mode: "test" | "production" | null;
  billing_npi: string | null;
  practice_npi_fallback: string | null;
  effective_npi: string | null;
  test_scenario: string;
  scenarios: { key: string; label: string }[];
}

export interface PayerMappingRow {
  id: string;
  ehr_payer_id: string | null;
  ehr_name: string | null;
  state: "resolved" | "confirmed" | "ambiguous" | "not_found" | "error" | "new";
  plan_kind: string | null;
  stedi_payer_id: string | null;
  stedi_name: string | null;
  programs: string[];
  enrollment_required: boolean;
  enrollment: { status: string; detail: string | null } | null;
  candidates: { payer_id: string; name: string | null; programs: string[]; states: string[]; enrollment_required: boolean }[];
  has_pin: boolean;
  last_error: string | null;
}

export interface HuddleRow {
  id: string;
  start: string;
  status: string;
  provider: string | null;
  type: string | null;
  patient_id: string | null;
  patient: string;
  coverage: { plan: string | null; payer_id: string | null; priority: number | null; insurance_type: string | null } | null;
  eligibility: EligibilityView | null;
  flags: string[];
}

export interface Huddle {
  date: string;
  days: number;
  timezone: string;
  appointments: HuddleRow[];
  counts: { scheduled: number; cancelled: number; verified_active: number; needs_attention: number };
}

// ---------- FO-2 cancellation backfill ----------

export interface BackfillOfferRow {
  id: string;
  rank: number;
  tier: "asap" | "move_up" | "recent_cancel";
  score: number;
  patient_id: string;
  patient: string;
  current_appointment_id: string | null;
  current_start: string | null;
  reasons: string[];
  state: "queued" | "accepted" | "declined" | "no_answer" | "booked" | "failed" | "superseded";
  responded_at: string | null;
}

export interface BackfillOpeningRow {
  id: string;
  state: "open" | "booking" | "filled" | "needs_review" | "taken" | "expired" | "closed" | "no_candidates";
  state_reason: string | null;
  start: string;
  minutes: number;
  timezone: string | null;
  provider: string | null;
  type: string | null;
  source_appointment_id: string;
  within_contact_hours: boolean;
  next_offer_id: string | null;
  filled_appointment_id: string | null;
  detected_at: string;
  filled_at: string | null;
  timeline: { at: string; event: string; [k: string]: unknown }[];
  offers: BackfillOfferRow[];
}

export interface BackfillBoard {
  settings: { lead_minutes: number; horizon_days: number; contact_hours: string; enabled: boolean };
  metrics: { states: Record<string, number>; fill_rate: number | null; median_minutes_to_fill: number | null; visit_minutes_recovered: number };
  active: BackfillOpeningRow[];
  recent: BackfillOpeningRow[];
}

// --- CarePlix Connect (EHR hub) --------------------------------------------------------------

export type HubSupport = "yes" | "no" | "verify" | "existing_patients";

export interface HubProfile {
  id: string;
  name: string;
  path: "direct" | "nexhealth";
  category: "medical" | "dental" | "ortho" | "general";
  deployment: "cloud" | "server" | "self-hosted";
  connects_by: string;
  confirm: "immediate" | "async" | "async_office_hours";
  ops: Record<string, HubSupport>;
  notes: string[];
}

export interface HubConnection {
  connection_id: string;
  kind: "openemr" | "nexhealth";
  name: string;
  env: string | null;
  profile: string | null;
  profile_name: string | null;
  emulating: string | null;
  links: Record<string, string> | null;
  verified_at: string | null;
  writes_confirmable: boolean | null;
  support: Record<string, HubSupport>;
}

export interface HubCatalog {
  operations: { id: string; label: string }[];
  profiles: HubProfile[];
  connections: HubConnection[];
}

// ---------- Shadow mode (switching kit) ----------

export type ShadowOutcome = "match" | "mismatch" | "ours_only" | "theirs_only" | "neither";
export type ShadowVerdict = "ours" | "theirs" | "both" | "neither";

export interface ShadowSummary {
  items: number;
  finished: number;
  errors: number;
  open: number;
  agreement: number | null;
  disagreements: number;
  resolution: { ours: number | null; theirs: number | null };
  median_duration_s: { ours: number | null; theirs: number | null };
  adjudicated: Record<ShadowVerdict | "open", number>;
  fields: ({ field: string; agreement: number | null } & Record<ShadowOutcome, number>)[];
}

export interface ShadowRun {
  id: string;
  name: string;
  incumbent: string;
  agent_id: string;
  version: { number: number; state: string };
  scenario_id: string;
  mode: "replay" | "live";
  fields: string[];
  state: "queued" | "running" | "done" | "cancelled";
  summary: ShadowSummary | null;
  created_at: string;
  finished_at: string | null;
}

export interface ShadowItem {
  id: string;
  seq: number;
  external_ref: string;
  destination: string | null;
  state: "queued" | "running" | "done" | "cancelled";
  attempt_id: string | null;
  theirs: Record<string, string>;
  ours: Record<string, string | null>;
  incumbent_metrics: { duration_s?: number | null; resolved?: boolean | null };
  our_metrics: { duration_s?: number | null; resolved?: boolean | null };
  comparison: { field: string; outcome: ShadowOutcome; adjudication: ShadowVerdict | null }[] | null;
  error: string | null;
}

export interface ShadowRunDetail extends ShadowRun {
  summary: ShadowSummary;
  items: ShadowItem[];
}

export interface ShadowPreview {
  columns: string[];
  sample: Record<string, string>[];
  total: number;
  max_items: number;
}
