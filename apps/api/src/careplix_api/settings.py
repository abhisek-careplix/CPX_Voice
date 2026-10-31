from functools import cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    careplix_env: str = "dev"
    log_level: str = "INFO"

    # Temporary G0 admin bearer token; replaced by OIDC in G1. Empty means admin routes fail closed.
    admin_api_token: SecretStr = SecretStr("")

    database_url: str = ""

    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: SecretStr = SecretStr("")
    livekit_agent_name: str = "careplix-agent"
    livekit_outbound_trunk_id: str = ""

    gcp_project: str = ""
    vertex_location: str = ""
    gemini_live_model: str = ""

    check_timeout_s: float = 5.0

    @property
    def livekit_configured(self) -> bool:
        return bool(
            self.livekit_url and self.livekit_api_key and self.livekit_api_secret.get_secret_value()
        )


@cache
def get_settings() -> Settings:
    return Settings()
