"""Field-level encryption for patient identifiers in the practice mirror (SEC08).

Names, dates of birth, phone numbers and member IDs are sealed with Fernet before they reach the
database; everything else in the mirror is an identifier, a code or a timestamp. Development
derives a key from the session secret; every other environment must set DATA_ENCRYPTION_KEY.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from careplix_api.settings import Settings


class PhiBox:
    def __init__(self, settings: Settings):
        key = settings.data_encryption_key.get_secret_value()
        if not key:
            if not settings.is_dev:
                raise RuntimeError("DATA_ENCRYPTION_KEY must be set outside dev")
            digest = hashlib.sha256(b"careplix-phi:" + settings.signing_key().encode()).digest()
            key = base64.urlsafe_b64encode(digest).decode()
        self._fernet = Fernet(key.encode())

    def seal(self, values: dict[str, Any]) -> str | None:
        clean = {k: v for k, v in values.items() if v not in (None, "")}
        if not clean:
            return None
        return self._fernet.encrypt(json.dumps(clean, sort_keys=True).encode()).decode()

    def open(self, token: str | None) -> dict[str, Any]:
        if not token:
            return {}
        try:
            return json.loads(self._fernet.decrypt(token.encode()))
        except InvalidToken:
            return {}


def initials(pii: dict[str, Any]) -> str:
    """The only patient identity the Studio shows by default."""
    first, last = (pii.get("first_name") or "")[:1], (pii.get("last_name") or "")[:1]
    return f"{first}. {last}." if first and last else "Patient"
