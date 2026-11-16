"""Which environment a deployment is, and what each one refuses (G4 PHI gate).

``CAREPLIX_ENV`` names it:

- ``dev``: a laptop. No extra rules (tests use production-shaped settings here).
- ``labs``: careplix-labs, synthetic data only. Refuses production keys (NexHealth production,
  Stedi production key or mode), so a real patient's record can't reach it.
- ``production``: real clinics and real PHI. Refuses every synthetic-only shortcut: the payer
  sandbox, seeded demo data, the OpenEMR test EHR and its emulation of other EHRs, Stedi test mode,
  the NexHealth sandbox, the shared trial line, dev-derived keys and DEBUG logging. CarePlix staff
  sign in with their careplix.com Google account (2-Step Verification is enforced there), never by
  an email code, and are not added to a customer workspace automatically.
- ``staging``: no extra rules.

dev, labs and staging are sandbox deployments: no workspace or connection there may be production
(``Settings.production_allowed`` is false; ``refuse_production_here`` raises). Only a production
deployment, in its own Google Cloud project (``infra/gcp/deploy-prod.sh``, not created yet), may.

Any other value is refused: a typo such as ``prod`` must not quietly skip the production rules.

Startup (``install``) refuses a misconfigured process before it serves anything. At runtime in
production the ORM hook refuses synthetic claims, synthetic connections and EHR emulation, and
writes an audit event for every change of state of a write intent (each EHR write's proposal,
approval, sending and outcome).

Other modules ask ``is_production()`` / ``refuse_in_production()`` rather than reading
``careplix_env`` themselves.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from careplix_api.errors import ApiError
from careplix_api.settings import Settings, get_settings

DEV, LABS, STAGING, PRODUCTION = "dev", "labs", "staging", "production"
KNOWN = (DEV, LABS, STAGING, PRODUCTION)

_env: str | None = None  # set by install(); processes that never call it read their settings
_listening = False


class EnvironmentRefused(RuntimeError):
    """The settings break this environment's rules; the process must not start."""


def problems(settings: Settings) -> list[str]:
    """Every rule the settings break, as one line each (empty means the settings are fine)."""
    env = settings.careplix_env
    if env not in KNOWN:
        return [f"CAREPLIX_ENV={env!r} is not one of {', '.join(KNOWN)}."]
    found: list[str] = []
    if env == LABS:
        if settings.nexhealth_env == "production":
            found.append("careplix-labs takes the NexHealth sandbox key only (NEXHEALTH_ENV).")
        if settings.stedi_env == "production":
            found.append("careplix-labs runs the clearinghouse in test mode only (STEDI_ENV).")
        if settings.stedi_prod_api_key.get_secret_value():
            found.append(
                "careplix-labs must not hold a production clearinghouse key (STEDI_PROD_API_KEY)."
            )
    if env == PRODUCTION:
        if settings.seed_demo_data:
            found.append("SEED_DEMO_DATA must be false: no demo workspace or sample data.")
        if settings.payer_sandbox_url:
            found.append("PAYER_SANDBOX_URL must be empty: the payer sandbox is synthetic.")
        if settings.openemr_base_url:
            found.append("OPENEMR_BASE_URL must be empty: OpenEMR is the test EHR (and emulator).")
        if settings.nexhealth_env != "production":
            found.append("NEXHEALTH_ENV must be production: the sandbox holds synthetic patients.")
        if settings.stedi_env != "production":
            found.append("STEDI_ENV must be production: test mode answers for mock members only.")
        if settings.nexhealth_api_key.get_secret_value():
            found.append(
                "NEXHEALTH_API_KEY is the sandbox key; production uses NEXHEALTH_PROD_API_KEY."
            )
        if (
            settings.stedi_test_api_key.get_secret_value()
            or settings.stedi_api_key.get_secret_value()
        ):
            found.append(
                "Clearinghouse test keys (STEDI_TEST_API_KEY, STEDI_API_KEY) must be unset."
            )
        if settings.sandbox_number or settings.sandbox_outbound_trunk_id:
            found.append("SANDBOX_NUMBER and SANDBOX_OUTBOUND_TRUNK_ID are the trial line; unset.")
        if not settings.session_secret.get_secret_value():
            found.append("SESSION_SECRET must be set.")
        if not settings.data_encryption_key.get_secret_value():
            found.append("DATA_ENCRYPTION_KEY must be set: no key derived from the session secret.")
        if settings.log_level.upper() == "DEBUG":
            found.append("LOG_LEVEL must not be DEBUG: library debug lines can carry PHI.")
        if settings.platform_admins.strip():
            found.append(
                "PLATFORM_ADMINS must be empty: staff sign in with their "
                f"{settings.iap_allowed_domain} account, where 2-Step Verification is enforced."
            )
    return found


def enforce(settings: Settings) -> None:
    found = problems(settings)
    if found:
        lines = "\n  - ".join(found)
        raise EnvironmentRefused(f"CAREPLIX_ENV={settings.careplix_env} refuses:\n  - {lines}")


def install(settings: Settings) -> None:
    """Check the settings and remember the environment (the API calls this before serving)."""
    global _env
    enforce(settings)
    _env = settings.careplix_env
    listen()


def listen() -> None:
    """Register the ORM hook once per process (every Database does, webhook ingress included)."""
    global _listening
    if not _listening:
        event.listen(Session, "before_flush", _before_flush)
        _listening = True


def current() -> str:
    return _env if _env is not None else get_settings().careplix_env


def is_production(settings: Settings | None = None) -> bool:
    return (settings.careplix_env if settings is not None else current()) == PRODUCTION


def refuse_production_here(what: str, settings: Settings) -> None:
    """Raise a 409 when ``what`` (a production key, connection or mode) is chosen in a sandbox
    deployment. Workspaces and connections there can never be production."""
    if not settings.production_allowed:
        raise ApiError(
            409,
            "production_not_allowed",
            f"{what} needs the production environment; this one is sandbox only.",
        )


def refuse_in_production(what: str, settings: Settings | None = None) -> None:
    """Raise a 409 when ``what`` (a synthetic-only shortcut) is attempted in production."""
    if is_production(settings):
        raise ApiError(409, "not_in_production", f"{what} isn't available in production.")


def check_sign_in(settings: Settings, provider: str, email: str) -> None:
    """Production: CarePlix staff sign in with their Google account, never an email code.

    An email code proves only that someone can read the mailbox. Staff accounts at the staff
    domain enforce 2-Step Verification in Google Workspace, so staff use Google (or IAP).
    """
    if is_production(settings) and provider == "email" and settings.is_platform_admin(email):
        raise ApiError(
            403,
            "mfa_required",
            f"CarePlix staff sign in with their {settings.iap_allowed_domain} Google account "
            "in production, not an email code.",
        )


def auto_provision_staff(settings: Settings) -> bool:
    """Outside production, staff with no workspace join the first one (the lab workspace).

    In production the first workspace is a clinic's: staff join one explicitly (Platform → Join),
    which is audited and shows up in the access review.
    """
    return not is_production(settings)


# --- ORM hook -----------------------------------------------------------------------------------


def _write_intent_event(obj: Any, new: bool) -> Any | None:
    from careplix_api.db import new_id
    from careplix_api.models import AuditEvent

    history = inspect(obj).attrs.state.history
    if not new and not history.has_changes():
        return None
    if obj.id is None:
        obj.id = new_id()
    state = obj.state or "proposed"
    actor = (obj.approved_by if state != "proposed" else None) or obj.requested_by or "system"
    return AuditEvent(
        workspace_id=obj.workspace_id,
        actor=actor,
        action="production.write_intent",
        target=obj.id,
        detail={
            "kind": obj.kind,
            "level": obj.level,
            "state": state,
            "from": None if new else (history.deleted or [None])[0],
            "source": obj.source,
            "connection_id": obj.connection_id,
            "target_resource": obj.target_resource,
        },
    )


def _before_flush(session: Session, _context: Any, _instances: Any) -> None:
    if current() != PRODUCTION:
        return
    from careplix_api.claims.models import Claim
    from careplix_api.models import PayerConnection, WriteIntent

    added = []
    for obj in [*session.new, *session.dirty]:
        new = obj in session.new
        if isinstance(obj, Claim) and (obj.source == "sample" or obj.synthetic is not False):
            raise ApiError(409, "not_in_production", "Synthetic claims aren't kept in production.")
        if isinstance(obj, PayerConnection) and (
            obj.synthetic or (obj.auth_ref or {}).get("emulate")
        ):
            raise ApiError(
                409, "not_in_production", "Synthetic or emulated connections aren't allowed."
            )
        if isinstance(obj, WriteIntent) and (audit := _write_intent_event(obj, new)) is not None:
            added.append(audit)
    session.add_all(added)
