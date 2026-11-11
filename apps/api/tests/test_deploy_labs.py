"""The careplix-labs deploy script gives each Cloud Run service what its code needs at runtime."""

import os
import pathlib
import re
import subprocess

import pytest

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
    # Deployed only when present: the hosted OpenEMR's secrets and the optional Anthropic key.
    optional = {s for s in referenced if s.startswith("cpx-openemr-")} | {"cpx-anthropic-api-key"}
    assert referenced - optional <= set(required)
    first_deploy = text.index("run deploy", text.index("deploy() {"))
    assert text.index("REQUIRED_SECRETS", text.index("deploy() {")) < first_deploy


def test_the_internal_api_stays_warm():
    # Every call begins with a claim on careplix-internal; a cold start made claims time out.
    assert "--min-instances 1" in _deploy_command("careplix-internal")


def test_the_studio_gets_the_anthropic_key_only_when_it_is_stored():
    text = SCRIPT.read_text()
    guard = text.index("secrets describe cpx-anthropic-api-key", text.index("deploy() {"))
    assert guard < text.index("ANTHROPIC_API_KEY=cpx-anthropic-api-key:latest")
    assert "$studio_secrets" in _deploy_command("careplix-studio")


def test_labs_gates_publishing_on_the_regression_suite():
    # Studio runs the suite loop and the judge; outbound agents publish only after a passed suite.
    command = _deploy_command("careplix-studio")
    assert "PUBLISH_REQUIRES_EVAL=true" in command and "RUN_BACKGROUND=true" in command


def test_the_webhook_ingress_can_open_the_sealed_endpoint_secret():
    # NexHealth deliveries are verified with the endpoint's secret_key, stored sealed.
    assert "DATA_ENCRYPTION_KEY=cpx-data-key:latest" in _deploy_command("careplix-webhooks")
    assert "for secret in cpx-database-url cpx-data-key; do" in SCRIPT.read_text()


def test_labs_still_refuses_a_production_nexhealth_key():
    assert 'if [ "$(env_value NEXHEALTH_ENV)" = "production" ]; then' in SCRIPT.read_text()


# --- Stedi: careplix-labs takes a test key only ---------------------------------------------------

FAKE_GCLOUD = """#!/bin/sh
echo "$*" >> "$GCLOUD_LOG"
case "$*" in
  *"versions add cpx-stedi-api-key"*) cat > "$STEDI_SECRET" ;;
  *"versions add"*) cat > /dev/null ;;
  *"projects describe"*) echo 123456 ;;
esac
exit 0
"""


def _run_setup(tmp_path, env_lines: list[str]):
    """Run ``deploy-labs.sh setup`` in a copy of the repo layout with a fake gcloud."""
    root = tmp_path / "repo"
    (root / "infra" / "gcp").mkdir(parents=True)
    script = root / "infra" / "gcp" / "deploy-labs.sh"
    script.write_text(SCRIPT.read_text())
    base = ["LIVEKIT_API_KEY=lk", "LIVEKIT_API_SECRET=lks", "NEXHEALTH_API_KEY=nh"]
    (root / ".env").write_text("\n".join(base + env_lines) + "\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(FAKE_GCLOUD)
    gcloud.chmod(0o755)
    log, secret = tmp_path / "gcloud.log", tmp_path / "stedi.secret"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GCLOUD_LOG": str(log),
        "STEDI_SECRET": str(secret),
        "TAG": "test",
    }
    result = subprocess.run(
        ["bash", str(script), "setup"], env=env, capture_output=True, text=True, check=False
    )
    calls = log.read_text() if log.exists() else ""
    return result, calls, secret.read_text() if secret.exists() else None


@pytest.mark.parametrize("line", ["STEDI_ENV=production", "STEDI_PROD_API_KEY=prod-key-not-here"])
def test_labs_refuses_a_production_stedi_key_before_touching_anything(tmp_path, line):
    result, calls, secret = _run_setup(tmp_path, ["STEDI_TEST_API_KEY=test-key", line])
    assert result.returncode == 1
    assert "Stedi test key only" in result.stderr
    assert "secrets" not in calls and "run deploy" not in calls  # nothing was changed
    assert secret is None


def test_labs_stores_the_stedi_test_key_and_accepts_the_legacy_name(tmp_path):
    result, _, secret = _run_setup(tmp_path, ["STEDI_ENV=test", "STEDI_TEST_API_KEY=test-key"])
    assert result.returncode == 0, result.stderr
    assert secret == "test-key"
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    result, _, secret = _run_setup(legacy, ["STEDI_API_KEY=old-test-key"])
    assert result.returncode == 0, result.stderr
    assert secret == "old-test-key"


def test_labs_runs_stedi_in_test_mode_under_the_test_key_name():
    for name in ("practice_env", "practice_secrets"):
        value = _variable(name)
        assert "STEDI_PROD_API_KEY" not in value and "STEDI_ENV=production" not in value
    assert "STEDI_ENV=test," in _variable("practice_env")
    assert "STEDI_TEST_API_KEY=cpx-stedi-api-key:latest" in _variable("practice_secrets")


# --- build and deploy use the same image tag ------------------------------------------------------
# careplix-labs, 11 Nov 2026: `build` pushed api:ac4cf38, another session then moved main to
# 468b747, and `deploy` recomputed the tag from HEAD and failed on images that were never built.

FAKE_GCLOUD_DEPLOY = """#!/bin/sh
echo "$*" >> "$GCLOUD_LOG"
case "$*" in
  *"artifacts docker images describe"*) [ -z "$IMAGE_MISSING" ] || exit 1 ;;
  *"projects describe"*) echo 123456 ;;
  *"versions add"*) cat > /dev/null ;;
esac
exit 0
"""


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True)
    return out.stdout.decode().strip()


def _repo(tmp_path):
    root = tmp_path / "repo"
    (root / "infra" / "gcp").mkdir(parents=True)
    (root / "infra" / "gcp" / "deploy-labs.sh").write_text(SCRIPT.read_text())
    (root / ".env").write_text("LIVEKIT_URL=wss://lk.test\n")
    (root / ".gitignore").write_text(".env\n.deploy-labs-tag\n")
    _git(root, "init", "-q")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    head = _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "one")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(FAKE_GCLOUD_DEPLOY)
    (bin_dir / "gcloud").chmod(0o755)
    return root, bin_dir, head


def _run(root, bin_dir, command, **extra):
    log = root.parent / "gcloud.log"
    log.unlink(missing_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "TAG"}
    env.update(PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", GCLOUD_LOG=str(log), **extra)
    result = subprocess.run(
        ["bash", "infra/gcp/deploy-labs.sh", command],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, log.read_text() if log.exists() else ""


def _first_image(calls: str) -> str:
    match = re.search(r"run deploy \S+ .*?--image \S+:(\S+)", calls)
    assert match, calls[-2000:]
    return match.group(1)


def test_build_records_the_tag_it_pushed(tmp_path):
    root, bin_dir, head = _repo(tmp_path)
    result, calls = _run(root, bin_dir, "build")
    assert result.returncode == 0, result.stderr
    assert f"_TAG={head}" in calls and f"built {head}" in result.stdout
    assert (root / ".deploy-labs-tag").read_text().strip() == head


def test_deploy_uses_the_built_tag_even_after_the_checkout_moves(tmp_path):
    root, bin_dir, built = _repo(tmp_path)
    _run(root, bin_dir, "build")
    (root / "later.txt").write_text("another session merged something")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "add", "later.txt")
    moved = _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "two")
    assert moved != built

    result, calls = _run(root, bin_dir, "deploy")
    registry = "us-central1-docker.pkg.dev/careplix-labs/careplix-voice"
    assert f"artifacts docker images describe {registry}/api:{built}" in calls
    assert _first_image(calls) == built
    assert f"Deploying {built}" in result.stderr and moved in result.stderr


def test_an_explicit_tag_wins(tmp_path):
    root, bin_dir, _ = _repo(tmp_path)
    _run(root, bin_dir, "build")
    _, calls = _run(root, bin_dir, "deploy", TAG="pinned1")
    assert _first_image(calls) == "pinned1"


def test_deploy_stops_before_changing_anything_when_the_images_are_missing(tmp_path):
    root, bin_dir, head = _repo(tmp_path)  # never built here, and the registry has no such tag
    result, calls = _run(root, bin_dir, "deploy", IMAGE_MISSING="1")
    assert result.returncode == 1
    assert f"No images tagged {head}" in result.stderr and "nothing was deployed" in result.stderr
    assert "run deploy" not in calls
