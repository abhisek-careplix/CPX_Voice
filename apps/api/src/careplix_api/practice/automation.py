"""Practice automation settings per workspace, with the deployment's env values as defaults.

A workspace (one clinic group) chooses how its front-desk automations behave; a practice
connection may override the sync interval (a busy office syncs more often). Anything a workspace
hasn't set follows the env setting of the same name, so labs and tests behave as before.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from careplix_api.errors import ApiError
from careplix_api.models import PayerConnection, Workspace
from careplix_api.settings import Settings

CancelMode = Literal["approval", "auto"]
ClearinghouseEnv = Literal["test", "production"]
MIN_SYNC_INTERVAL_S = 60.0  # NexHealth bills per call; a faster loop only burns the budget
MAX_SYNC_INTERVAL_S = 86400.0


@dataclass(frozen=True)
class Automation:
    receptionist_cancel_mode: CancelMode
    backfill_enabled: bool
    eligibility_autopilot: bool
    sync_interval_s: float
    clearinghouse_env: ClearinghouseEnv

    def view(self) -> dict[str, Any]:
        return asdict(self)


FIELDS = tuple(Automation.__dataclass_fields__)


def defaults(settings: Settings) -> Automation:
    return Automation(
        receptionist_cancel_mode="auto"
        if settings.receptionist_cancel_mode == "auto"
        else "approval",
        backfill_enabled=settings.backfill_enabled,
        eligibility_autopilot=settings.eligibility_autopilot,
        sync_interval_s=settings.practice_sync_interval_s,
        clearinghouse_env=settings.stedi_mode,  # type: ignore[arg-type]
    )


def effective(workspace: Workspace | None, settings: Settings) -> Automation:
    base = defaults(settings).view()
    chosen = {
        k: v for k, v in ((workspace.practice_automation if workspace else None) or {}).items()
    }
    return Automation(**{k: chosen.get(k, base[k]) for k in FIELDS})


def sync_interval(
    conn: PayerConnection, workspace: Workspace | None, settings: Settings, default: float
) -> float:
    """The connection's own interval, else the workspace's choice, else the connector's default
    (OpenEMR's local test clinic syncs faster than a billed NexHealth practice)."""
    own = ((conn.auth_ref or {}).get("automation") or {}).get("sync_interval_s")
    if own is not None:
        return float(own)
    chosen = ((workspace.practice_automation if workspace else None) or {}).get("sync_interval_s")
    return float(chosen) if chosen is not None else default


def validate(changes: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Check a partial update. ``None`` clears a choice (back to the env default)."""
    out: dict[str, Any] = {}
    for key, value in changes.items():
        if key not in FIELDS:
            raise ApiError(422, "unknown_setting", f"{key} isn't an automation setting.")
        if value is None:
            out[key] = None
            continue
        if key == "receptionist_cancel_mode" and value not in ("approval", "auto"):
            raise ApiError(422, "invalid_setting", "Cancel mode is approval or auto.")
        if key in ("backfill_enabled", "eligibility_autopilot") and not isinstance(value, bool):
            raise ApiError(422, "invalid_setting", f"{key} is on or off.")
        if key == "sync_interval_s":
            value = float(value)
            if value != 0 and not MIN_SYNC_INTERVAL_S <= value <= MAX_SYNC_INTERVAL_S:
                raise ApiError(
                    422,
                    "invalid_setting",
                    "Sync every 1 minute to 24 hours (or 0 to rely on webhooks only).",
                )
        if key == "clearinghouse_env":
            if value not in ("test", "production"):
                raise ApiError(422, "invalid_setting", "Clearinghouse mode is test or production.")
            if value == "production" and not settings.stedi_prod_api_key.get_secret_value():
                raise ApiError(
                    409,
                    "production_unavailable",
                    "This environment has no production clearinghouse key.",
                )
        out[key] = value
    return out


def apply(workspace: Workspace, changes: dict[str, Any]) -> None:
    merged = dict(workspace.practice_automation or {})
    for key, value in changes.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    workspace.practice_automation = merged or None
