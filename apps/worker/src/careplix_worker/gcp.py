"""Google credentials for Vertex AI from the worker (see careplix_common.gemini for the policy)."""

from __future__ import annotations

from google.auth.credentials import Credentials

from careplix_common.gemini import CredentialConfigError, credentials_from_json
from careplix_worker.settings import WorkerSettings

__all__ = ["CredentialConfigError", "credential_source", "load_credentials"]


def load_credentials(settings: WorkerSettings) -> Credentials | None:
    """Return explicit credentials, or None to let the Google SDK use ADC."""
    return credentials_from_json(settings.google_service_account_json.get_secret_value())


def credential_source(settings: WorkerSettings) -> str:
    """Non-secret description of the credential path, for evidence records."""
    return (
        "service_account_secret"
        if settings.google_service_account_json.get_secret_value().strip()
        else "application_default"
    )
