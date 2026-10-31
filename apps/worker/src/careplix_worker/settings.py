from functools import cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_WORKER_TOKEN = "dev-only-worker-token"


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    careplix_env: str = "dev"
    livekit_agent_name: str = "careplix-agent"

    # CarePlix control plane the worker claims attempts from (ADR09). Hosted workers need a URL
    # reachable from LiveKit Cloud (for local development, a tunnel to the API).
    careplix_api_url: str = "http://localhost:8080"
    worker_service_token: SecretStr = SecretStr("")

    gcp_project: str = ""
    vertex_location: str = ""
    gemini_live_model: str = ""
    gemini_voice: str = "Puck"
    # Fallback credential path (BUILD_PLAN R2): restricted service-account JSON held as a LiveKit
    # Cloud agent secret. Empty means Application Default Credentials.
    google_service_account_json: SecretStr = SecretStr("")

    def missing_model_config(self) -> list[str]:
        return [
            name
            for name, value in [
                ("GCP_PROJECT", self.gcp_project),
                ("VERTEX_LOCATION", self.vertex_location),
                ("GEMINI_LIVE_MODEL", self.gemini_live_model),
            ]
            if not value
        ]

    def service_token(self) -> str:
        token = self.worker_service_token.get_secret_value()
        if token:
            return token
        if self.careplix_env == "dev":
            return DEV_WORKER_TOKEN
        raise RuntimeError("WORKER_SERVICE_TOKEN must be set outside dev")


@cache
def get_settings() -> WorkerSettings:
    return WorkerSettings()
