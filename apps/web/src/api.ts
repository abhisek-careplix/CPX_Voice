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
  evaluation: { status: string; untested: string[] };
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
  status: "provisional" | "confirmed_by_counterpart" | "reviewed" | "conflicting";
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

export interface CallDetail extends Attempt {
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
}

export interface CallEvent {
  seq: number;
  type: string;
  source: string;
  occurred_at: string;
  payload: Record<string, unknown>;
}
