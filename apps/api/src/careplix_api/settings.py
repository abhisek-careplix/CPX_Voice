from functools import cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    careplix_env: str = "dev"
    log_level: str = "INFO"

    # Temporary admin bearer token for scripts; replaced by OIDC in G1. Empty means fail closed.
    admin_api_token: SecretStr = SecretStr("")
    # Signs session cookies and attempt-scoped worker tokens. Must be set outside dev.
    session_secret: SecretStr = SecretStr("")
    # Shared credential the agent worker exchanges for an attempt-scoped token (ADR09).
    worker_service_token: SecretStr = SecretStr("")

    # SQLite keeps local development dependency-free; Postgres (+ RLS) is the deployed store.
    database_url: str = "sqlite+aiosqlite:///./careplix-dev.db"
    seed_demo_data: bool = True

    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: SecretStr = SecretStr("")
    livekit_agent_name: str = "careplix-agent"
    livekit_outbound_trunk_id: str = ""

    gcp_project: str = ""
    vertex_location: str = ""
    gemini_live_model: str = ""
    # Optional restricted service-account JSON for the API's own model test. Empty uses ADC.
    google_service_account_json: SecretStr = SecretStr("")
    # VS3 AI judge: a Gemini text model on Vertex (same project; region defaults to the workspace).
    judge_model: str = "gemini-2.5-flash"
    judge_location: str = ""
    judge_interval_s: float = 20.0  # how often finished Voice Studio runs are auto-scored
    # With ANTHROPIC_API_KEY set the judge is Claude on the Anthropic API (careplix-labs serves no
    # Gemini text model); without it, JUDGE_MODEL on Vertex.
    judge_claude_model: str = "claude-opus-5-5"
    # Regression suites (pre-publish evaluation): simulations of an agent version against its
    # scenarios. They run in their own lane, beside (not counted against) max_concurrent_calls.
    eval_concurrency: int = 3
    eval_interval_s: float = 10.0
    eval_case_timeout_s: float = 1800.0  # a simulation that hasn't ended by now is an error
    eval_judge_wait_s: float = 900.0  # how long a finished simulation waits for its judgement
    # Publishing an outbound agent needs a passed suite on the exact draft (admins may override).
    publish_requires_eval: bool = False
    # Supervised browser agent (BROWSER_AGENT_PLAN). The runtime is the private careplix-browser
    # service; empty URL leaves the feature off.
    browser_runtime_url: str = ""
    browser_runtime_auth: str = "none"  # none | google_id_token (private Cloud Run)
    browser_service_token: SecretStr = SecretStr("")
    browser_default_model: str = "gemini"  # gemini | claude (workspace default; per run override)
    browser_gemini_model: str = "gemini-2.5-computer-use-preview-10-2025"
    browser_gemini_location: str = ""  # empty: the workspace's Vertex region
    browser_claude_model: str = "claude-opus-5-5"
    browser_claude_region: str = "global"  # Vertex only
    # Set: Claude runs on the Anthropic API with this key. Empty: Claude on Vertex AI.
    anthropic_api_key: SecretStr = SecretStr("")
    browser_max_steps: int = 60
    browser_presence_timeout_s: float = 60.0  # supervisor's live view idle → the run pauses
    browser_profile_days: int = 14  # saved sign-ins expire

    # Development payer endpoint seeded as a workspace connection (synthetic). Empty disables.
    payer_sandbox_url: str = "http://localhost:8091/fhir"
    payer_sandbox_auth: str = "none"  # none | google_id_token (private Cloud Run sandbox)

    # Cloud deployment (BUILD_PLAN §9): IAP sign-in, bundled Studio, background loop placement.
    iap_audience: str = ""  # e.g. /projects/<number>/locations/<region>/services/<service>
    iap_allowed_domain: str = "careplix.com"
    iap_default_role: str = "admin"  # role granted on first sign-in to the single lab workspace
    studio_dist: str = ""  # path to the built Studio (apps/web/dist) to serve at /
    run_background: bool = True  # reconciler, dial runner and integration worker

    # Practice EHR via NexHealth (docs/solution/EMR_LAYER_SOLUTION_DESIGN.md). Set by
    # scripts/connect-nexhealth.sh; an empty key leaves the practice layer off.
    nexhealth_api_key: SecretStr = SecretStr("")
    nexhealth_env: str = "sandbox"  # sandbox | production
    nexhealth_base_url: str = "https://nexhealth.info"
    nexhealth_subdomain: str = ""
    nexhealth_institution_id: str = ""
    nexhealth_location_id: str = ""
    # Local OpenEMR (infra/openemr, scripts/openemr_setup.py): a real EHR for end-to-end testing.
    openemr_base_url: str = ""
    openemr_client_id: str = ""
    openemr_client_secret: SecretStr = SecretStr("")
    openemr_username: str = ""
    openemr_password: SecretStr = SecretStr("")
    openemr_timezone: str = "America/New_York"
    openemr_verify_tls: bool = True  # the local container uses a self-signed certificate
    openemr_facility_id: str = ""
    openemr_ui_url: str = ""  # where staff open OpenEMR screens; defaults to the API base URL
    openemr_sync_interval_s: float = 30.0  # local and free, so cancellations show up quickly
    # Clearinghouse via Stedi. Set by scripts/connect-stedi.sh.
    stedi_api_key: SecretStr = SecretStr("")
    stedi_env: str = "test"  # test (mock payers only) | production
    stedi_base_url: str = "https://healthcare.us.stedi.com"
    stedi_webhook_secret: SecretStr = SecretStr("")  # Standard Webhooks secret (whsec_…)
    # Billing provider sent on eligibility checks (practice NPI). Test mode uses Stedi's mock NPI.
    practice_npi: str = ""
    practice_name: str = "CarePlix Practice"
    # Encrypts patient identifiers in the practice mirror (Fernet key). Dev derives one.
    data_encryption_key: SecretStr = SecretStr("")
    # Public base URL of the webhook ingress (careplix-webhooks); empty means webhooks are off.
    webhook_public_url: str = ""
    practice_sync_interval_s: float = 900.0  # incremental sync cadence; 0 disables
    eligibility_autopilot: bool = True
    # FO-2 cancellation backfill: slots starting sooner than the lead time aren't worth offering;
    # far-future ones fill through normal scheduling.
    backfill_enabled: bool = True
    backfill_lead_minutes: int = 120
    backfill_horizon_days: int = 14
    contact_hours: str = "08:00-20:00"  # practice-local window for patient outreach
    # FO-3 receptionist. Patient-requested bookings and reschedules are L2 (automatic). A plain
    # cancellation is L3 in the EMR design, so by default it waits for one click in the write
    # queue; "auto" cancels at once (and the freed slot goes straight to backfill).
    receptionist_cancel_mode: str = "approval"  # approval | auto
    receptionist_lead_minutes: int = 60  # earliest bookable time from now
    receptionist_search_days: int = 14

    # Release D calling policy (FR05, SEC12): only approved synthetic test destinations.
    approved_test_destinations: str = ""
    # Shared sandbox line for trials: one CarePlix Twilio number. Inbound callers key in their
    # workspace code; trial workspaces may place outbound test calls with it as caller ID.
    sandbox_number: str = ""  # E.164, e.g. +13127558589
    sandbox_outbound_trunk_id: str = ""  # the LiveKit outbound trunk that owns sandbox_number
    sandbox_daily_calls: int = 20  # per workspace, inbound and outbound, rolling 24 hours
    sandbox_max_call_s: int = 300
    sandbox_max_destinations: int = 3
    max_concurrent_calls: int = 1
    max_call_duration_s: int = 900  # talk time; hold time is extra, up to max_hold_s (ADR10)
    max_hold_s: int = 2700  # longest single payer hold a call may sit through
    worker_claim_timeout_s: float = 20.0
    heartbeat_stale_s: float = 45.0

    check_timeout_s: float = 5.0

    @property
    def is_dev(self) -> bool:
        return self.careplix_env == "dev"

    @property
    def livekit_configured(self) -> bool:
        return bool(
            self.livekit_url and self.livekit_api_key and self.livekit_api_secret.get_secret_value()
        )

    @property
    def nexhealth_configured(self) -> bool:
        return bool(self.nexhealth_api_key.get_secret_value() and self.nexhealth_subdomain)

    @property
    def openemr_configured(self) -> bool:
        return bool(self.openemr_base_url and self.openemr_client_id and self.openemr_username)

    @property
    def stedi_configured(self) -> bool:
        return bool(self.stedi_api_key.get_secret_value())

    @property
    def approved_destinations(self) -> set[str]:
        return {d.strip() for d in self.approved_test_destinations.split(",") if d.strip()}

    def signing_key(self) -> str:
        key = self.session_secret.get_secret_value()
        if key:
            return key
        if self.is_dev:
            return "dev-only-insecure-session-secret-change-me"
        raise RuntimeError("SESSION_SECRET must be set outside dev")


@cache
def get_settings() -> Settings:
    return Settings()
