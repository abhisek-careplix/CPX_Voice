"""The careplix-labs deploy script gives each Cloud Run service what its code needs at runtime."""

import pathlib
import re

SCRIPT = pathlib.Path(__file__).parents[3] / "infra" / "gcp" / "deploy-labs.sh"


def _deploy_command(service: str) -> str:
    """The full ``gcloud run deploy <service> …`` command (continuation lines joined)."""
    text = SCRIPT.read_text().replace("\\\n", " ")
    match = re.search(rf"run deploy {service} [^\n]*", text)
    assert match, f"{service} is not deployed by the script"
    return match.group(0)


def _variable(name: str) -> str:
    match = re.search(rf'^\s*{name}="([^"]*)"', SCRIPT.read_text(), re.M)
    assert match, name
    return match.group(1)


def test_practice_secrets_carry_the_data_key():
    assert "DATA_ENCRYPTION_KEY=cpx-data-key" in _variable("practice_secrets")


def test_every_api_service_that_opens_phi_gets_the_data_key():
    # careplix-labs, 4 Nov 2026: careplix-internal had no DATA_ENCRYPTION_KEY, so every call claim
    # (calls_service._claim_payload, counterpart_claim) and receptionist lookup raised in PhiBox
    # and the worker hung up: Simulate, Rehearse and live calls all failed.
    for service in ("careplix-studio", "careplix-internal"):
        command = _deploy_command(service)
        assert "$practice_secrets" in command and "$practice_env" in command, service
        assert "$VPC_FLAGS" in command, service  # the hosted OpenEMR is private


def test_deploy_checks_every_secret_before_changing_anything():
    text = SCRIPT.read_text()
    required = _variable("REQUIRED_SECRETS").split()
    referenced = set(re.findall(r"=(cpx-[a-z-]+):latest", text))
    openemr = {s for s in referenced if s.startswith("cpx-openemr-")}  # deployed only when present
    assert referenced - openemr <= set(required)
    first_deploy = text.index("run deploy", text.index("deploy() {"))
    assert text.index("REQUIRED_SECRETS", text.index("deploy() {")) < first_deploy
