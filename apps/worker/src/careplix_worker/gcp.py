"""Google credentials for Vertex AI from the worker.

Order of preference (doc 02, BUILD_PLAN ADR07/R2):
1. Supported workload federation, exposed to the process as Application Default Credentials.
2. A restricted, rotated service-account key supplied as GOOGLE_SERVICE_ACCOUNT_JSON.
The key is parsed in memory only and never written to disk or logged.
"""

from __future__ import annotations

import json

from google.auth.credentials import Credentials
from google.oauth2 import service_account

from careplix_worker.settings import WorkerSettings

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


class CredentialConfigError(ValueError):
    pass


def load_credentials(settings: WorkerSettings) -> Credentials | None:
    """Return explicit credentials, or None to let the Google SDK use ADC."""
    raw = settings.google_service_account_json.get_secret_value().strip()
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CredentialConfigError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON") from exc
    if info.get("type") != "service_account":
        raise CredentialConfigError("GOOGLE_SERVICE_ACCOUNT_JSON must be a service_account key")
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def credential_source(settings: WorkerSettings) -> str:
    """Non-secret description of the credential path, for evidence records."""
    return (
        "service_account_secret"
        if settings.google_service_account_json.get_secret_value().strip()
        else "application_default"
    )
