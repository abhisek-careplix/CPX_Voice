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

    # Release D calling policy (FR05, SEC12): only approved synthetic test destinations.
    approved_test_destinations: str = ""
    max_concurrent_calls: int = 1
    max_call_duration_s: int = 900
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
