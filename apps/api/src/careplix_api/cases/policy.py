"""Approval policy (operating plan D5, P6): what an agent may decide alone, what a person approves,
and what a clinician signs.

One table answers "who decides this?" for every kind of approval the platform raises. The review
inbox shows each item with its approver, and playbooks ask it before acting on their own.

- ``agent``: the agent acts alone (the action is logged as evidence).
- ``staff``: a person with the review permission approves.
- ``admin``: a workspace administrator approves (EHR writes, as the practice API already requires).
- ``supervisor``: the person supervising a browser run approves its steps.
- ``clinician``: a clinician signs off. Until identity (workstream A) adds a clinician role, the
  review permission stands in for it; :func:`can_decide` is the one place to change that.

A workspace override (by product line, later by customer) plugs into :func:`rules_for`.
"""

from __future__ import annotations

from typing import Any

from careplix_api.auth import Principal

APPROVERS = ("agent", "staff", "admin", "supervisor", "clinician")

DEFAULT_RULES: dict[str, str] = {
    # Review inbox kinds
    "result_review": "staff",
    "ehr_write": "admin",
    "browser_step": "supervisor",
    "browser_plan": "supervisor",
    "clinician_signoff": "clinician",
    "hand_off": "staff",
    "auth_packet": "staff",
    "escalation": "staff",
    # What a playbook may do on its own
    "playbook.contact": "agent",  # approved destinations only; consent lives in the channel
    "playbook.ehr_write.appointment_confirm": "agent",  # the patient just confirmed it
    "playbook.ehr_write.appointment_book": "agent",  # the patient said yes to the offered slot
    # Submitting a prior auth the packet supports (synthetic payer sandbox only until the PHI gate).
    "playbook.prior_auth.submit": "agent",
    "playbook.ehr_write": "admin",  # any other write a playbook proposes waits for a person
    "playbook.clinical_message": "clinician",  # clinical words come from a clinician
}


def rules_for(workspace_id: str | None = None) -> dict[str, str]:
    """The rules in force for a workspace. Per-workspace overrides land here."""
    return dict(DEFAULT_RULES)


def approver(action: str, workspace_id: str | None = None) -> str:
    """Who decides ``action``; falls back from ``a.b.c`` to ``a.b`` to ``a``, then to staff."""
    rules = rules_for(workspace_id)
    key = action
    while key:
        if key in rules:
            return rules[key]
        key = key.rpartition(".")[0]
    return "staff"


def agent_may(action: str, workspace_id: str | None = None) -> bool:
    return approver(action, workspace_id) == "agent"


def can_decide(principal: Principal, who: str, context: dict[str, Any] | None = None) -> bool:
    if who == "agent":
        return True
    if who == "admin":
        return principal.can("admin")
    if who == "supervisor":
        return principal.can("browser_supervise")
    if who in ("staff", "clinician"):
        return principal.can("review")
    return False


def view(workspace_id: str | None = None) -> list[dict[str, str]]:
    return [{"action": k, "approver": v} for k, v in rules_for(workspace_id).items()]
