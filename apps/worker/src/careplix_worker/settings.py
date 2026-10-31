from functools import cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    careplix_env: str = "dev"
    livekit_agent_name: str = "careplix-agent"

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


@cache
def get_settings() -> WorkerSettings:
    return WorkerSettings()
