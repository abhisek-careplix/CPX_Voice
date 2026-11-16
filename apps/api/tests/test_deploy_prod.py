"""The production deploy script: its own project and keys, nothing synthetic, merged builds only,
and a typed confirmation before anything changes. Run against a fake gcloud; nothing real."""

from __future__ import annotations

import os
import pathlib
import re
import subprocess

import pytest

from careplix_api import environment
from careplix_api.settings import Settings

SCRIPT = pathlib.Path(__file__).parents[3] / "infra" / "gcp" / "deploy-prod.sh"
EXAMPLE = SCRIPT.parent / "env.production.example"
SCRIPT_TEXT = SCRIPT.read_text()
# The script ships with no production project; tests run it as if the owner had named one.
TEXT = SCRIPT_TEXT.replace('PROJECT=""', "PROJECT=careplix-prod", 1)

GOOD_ENV = [
    "LIVEKIT_URL=wss://prod.livekit.test",
    "LIVEKIT_API_KEY=lk",
    "LIVEKIT_API_SECRET=lks",
    "NEXHEALTH_ENV=production",
    "NEXHEALTH_API_KEY=nh-prod",
    "STEDI_ENV=production",
    "STEDI_PROD_API_KEY=stedi-prod",
]

FAKE_GCLOUD = """#!/bin/sh
echo "$*" >> "$GCLOUD_LOG"
case "$*" in
  *"versions add "*)
    name=$(echo "$*" | sed 's/.*versions add \\([^ ]*\\).*/\\1/')
    cat > "$SECRETS_DIR/$name" ;;
  *"artifacts docker images describe"*) [ -z "$IMAGE_MISSING" ] || exit 1 ;;
  *"secrets describe"* | *"sql instances describe"*) [ -z "$NO_SECRETS" ] || exit 1 ;;
  *"projects describe"*) echo 987654 ;;
  *"get-iam-policy"*) echo '{"bindings": []}' ;;
esac
exit 0
"""


def _git(root, *args) -> str:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )
    out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True)
    return out.stdout.decode().strip()


@pytest.fixture
def repo(tmp_path):
    """A repo whose main is pushed to a local origin, with the script and a production env."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    root = tmp_path / "repo"
    (root / "infra" / "gcp").mkdir(parents=True)
    (root / "infra" / "gcp" / "deploy-prod.sh").write_text(TEXT)
    (root / ".gitignore").write_text(".env\n.env.*\n.deploy-prod-tag\n")
    (root / ".env").write_text("LIVEKIT_URL=wss://labs.livekit.test\n")
    (root / ".env.production").write_text("\n".join(GOOD_ENV) + "\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".")
    head = _git(root, "commit", "-qm", "one")
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "-q", "origin", "main")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(FAKE_GCLOUD)
    (bin_dir / "gcloud").chmod(0o755)
    (tmp_path / "secrets").mkdir()
    return root, head


def run(root, command, *, env_lines=None, **extra):
    if env_lines is not None:
        (root / ".env.production").write_text("\n".join(env_lines) + "\n")
    log = root.parent / "gcloud.log"
    log.unlink(missing_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("TAG", "CONFIRM_PROJECT")}
    env.update(
        PATH=f"{root.parent / 'bin'}{os.pathsep}{os.environ['PATH']}",
        GCLOUD_LOG=str(log),
        SECRETS_DIR=str(root.parent / "secrets"),
        **extra,
    )
    result = subprocess.run(
        ["bash", "infra/gcp/deploy-prod.sh", command],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    return result, log.read_text() if log.exists() else ""


# --- preflight: the rules, before any gcloud command ----------------------------------------------


def test_the_project_is_named_once_and_is_not_labs():
    assert re.search(r'^PROJECT=""  #', SCRIPT_TEXT, re.M)  # no production project yet
    assert re.search(r"^PROJECT=careplix-prod\b", TEXT, re.M)
    assert len(re.findall(r"^PROJECT=", TEXT, re.M)) == 1
    assert 'G="gcloud --project $PROJECT --quiet"' in TEXT
    code = [line for line in TEXT.splitlines() if not line.lstrip().startswith("#")]
    assert [line for line in code if "careplix-labs" in line] == ["LABS_PROJECT=careplix-labs"]


@pytest.mark.parametrize("command", ["preflight", "setup", "build", "deploy", "status"])
def test_nothing_runs_until_a_production_project_is_named(repo, command):
    root, _ = repo
    (root / "infra" / "gcp" / "deploy-prod.sh").write_text(SCRIPT_TEXT)
    result, calls = run(root, command, CONFIRM_PROJECT="careplix-prod")
    assert result.returncode == 1 and "No production project yet" in result.stderr
    assert calls == ""


def test_preflight_passes_a_production_env(repo):
    root, _ = repo
    result, calls = run(root, "preflight")
    assert result.returncode == 0, result.stderr
    assert calls == ""  # read-only


@pytest.mark.parametrize(
    ("line", "says"),
    [
        ("NEXHEALTH_ENV=sandbox", "NEXHEALTH_ENV"),
        ("STEDI_ENV=test", "STEDI_ENV"),
        ("STEDI_TEST_API_KEY=tk", "STEDI_TEST_API_KEY"),
        ("STEDI_API_KEY=old", "STEDI_API_KEY"),
        ("OPENEMR_BASE_URL=https://10.0.0.5", "OPENEMR_BASE_URL"),
        ("SANDBOX_NUMBER=+13127558589", "SANDBOX_NUMBER"),
        ("PAYER_SANDBOX_URL=http://x/fhir", "PAYER_SANDBOX_URL"),
        ("PLATFORM_ADMINS=someone@gmail.com", "PLATFORM_ADMINS"),
        ("CAREPLIX_ENV=labs", "CAREPLIX_ENV"),
        ("LOG_LEVEL=DEBUG", "LOG_LEVEL"),
        ("LIVEKIT_URL=wss://labs.livekit.test", "labs LiveKit project"),
    ],
)
@pytest.mark.parametrize("command", ["setup", "build", "deploy"])
def test_every_command_refuses_synthetic_settings_before_touching_anything(
    repo, line, says, command
):
    root, _ = repo
    key = line.split("=")[0]
    lines = [entry for entry in GOOD_ENV if not entry.startswith(key + "=")] + [line]
    result, calls = run(root, command, env_lines=lines, CONFIRM_PROJECT="careplix-prod")
    assert result.returncode == 1
    assert says in result.stderr and "Nothing was changed" in result.stderr
    assert calls == ""


@pytest.mark.parametrize("missing", ["STEDI_PROD_API_KEY", "NEXHEALTH_API_KEY", "LIVEKIT_API_KEY"])
def test_preflight_needs_the_production_keys(repo, missing):
    root, _ = repo
    lines = [entry for entry in GOOD_ENV if not entry.startswith(missing + "=")]
    result, _ = run(root, "preflight", env_lines=lines)
    assert result.returncode == 1 and missing in result.stderr


def test_no_env_file_no_deploy(repo):
    root, _ = repo
    (root / ".env.production").unlink()
    result, calls = run(root, "setup")
    assert result.returncode == 1 and "No .env.production" in result.stderr and calls == ""


def test_the_example_env_lists_what_must_stay_empty():
    text = EXAMPLE.read_text()
    for key in ("STEDI_TEST_API_KEY", "OPENEMR_BASE_URL", "SANDBOX_NUMBER", "PAYER_SANDBOX_URL"):
        assert not re.search(rf"^{key}=", text, re.M) and key in text
    assert re.search(r"^NEXHEALTH_ENV=production$", text, re.M)


# --- setup ----------------------------------------------------------------------------------------


def test_setup_stores_production_keys_under_their_own_names(repo):
    root, _ = repo
    result, calls = run(root, "setup", NO_SECRETS="1")  # a new project: nothing stored yet
    assert result.returncode == 0, result.stderr
    secrets = root.parent / "secrets"
    assert (secrets / "cpx-stedi-prod-api-key").read_text() == "stedi-prod"
    assert (secrets / "cpx-nexhealth-api-key").read_text() == "nh-prod"
    assert not (secrets / "cpx-stedi-api-key").exists()  # the labs (test key) name
    assert len((secrets / "cpx-data-key").read_text()) == 44  # generated here, never copied
    assert all("--project careplix-prod" in line for line in calls.splitlines())
    # Production database: HA, PITR, deletion protection; data access audit logs; log retention.
    for flag in (
        "--availability-type regional",
        "--enable-point-in-time-recovery",
        "--deletion-protection",
        "--ssl-mode ENCRYPTED_ONLY",
    ):
        assert flag in calls
    assert "set-iam-policy" in calls and "logging buckets update _Default" in calls
    assert "keys create" not in calls  # no service-account keys


def test_each_service_account_gets_only_its_own_secrets(repo):
    root, _ = repo
    _, calls = run(root, "setup")

    def granted(sa: str) -> set[str]:
        return set(
            re.findall(
                rf"secrets add-iam-policy-binding (\S+) --member serviceAccount:{sa}@", calls
            )
        )

    assert granted("careplix-worker") == {
        "cpx-worker-token",
        "cpx-livekit-api-key",
        "cpx-livekit-api-secret",
    }
    assert granted("careplix-webhooks") == {"cpx-database-url", "cpx-data-key"}
    assert granted("careplix-browser") == {"cpx-browser-token"}
    assert "cpx-browser-token" not in granted("careplix-internal")
    roles = re.findall(r"add-iam-policy-binding careplix-prod --member (\S+) --role (\S+)", calls)
    assert ("serviceAccount:careplix-worker@careplix-prod.iam.gserviceaccount.com",
            "roles/cloudsql.client") not in roles  # fmt: skip
    assert not any(role in ("roles/owner", "roles/editor") for _, role in roles)


# --- build: merged code only ----------------------------------------------------------------------


def test_build_records_the_tag_of_a_merged_commit(repo):
    root, head = repo
    result, calls = run(root, "build")
    assert result.returncode == 0, result.stderr
    assert f"_TAG={head}" in calls and f"built {head}" in result.stdout
    assert (root / ".deploy-prod-tag").read_text().strip() == head


def test_build_refuses_uncommitted_changes(repo):
    root, _ = repo
    (root / "infra" / "gcp" / "deploy-prod.sh").write_text(TEXT + "\n# local edit\n")
    result, calls = run(root, "build")
    assert result.returncode == 1 and "Uncommitted changes" in result.stderr
    assert "builds submit" not in calls


def test_build_refuses_a_commit_that_is_not_on_main(repo):
    root, _ = repo
    (root / "feature.txt").write_text("unmerged")
    _git(root, "add", "feature.txt")
    unmerged = _git(root, "commit", "-qm", "unmerged")
    result, calls = run(root, "build")
    assert result.returncode == 1 and f"{unmerged} is not on origin/main" in result.stderr
    assert "builds submit" not in calls


# --- deploy ---------------------------------------------------------------------------------------


def _deploys(calls: str) -> dict[str, str]:
    return dict(re.findall(r"run deploy (\S+) (.*)", calls))


def test_deploy_needs_the_typed_project_id(repo):
    root, _ = repo
    run(root, "build")
    for answer in ({}, {"CONFIRM_PROJECT": "careplix-labs"}):
        result, calls = run(root, "deploy", **answer)
        assert result.returncode == 1 and "Not confirmed" in result.stderr
        assert "run deploy" not in calls


def test_deploy_ships_the_built_tag_as_production_with_nothing_synthetic(repo):
    root, built = repo
    run(root, "build")
    (root / "later.txt").write_text("another session merged something")
    _git(root, "add", "later.txt")
    _git(root, "commit", "-qm", "two")
    result, calls = run(root, "deploy", CONFIRM_PROJECT="careplix-prod")
    assert result.returncode == 0, result.stderr
    assert f"deploying {built}" in result.stdout
    deploys = _deploys(calls)
    assert set(deploys) == {
        "careplix-webhooks",
        "careplix-browser",
        "careplix-studio",
        "careplix-internal",
        "careplix-worker",
    }  # no payer sandbox, no OpenEMR proxy
    for name, command in deploys.items():
        assert f":{built} " in command, name
        if name != "careplix-browser":
            assert "CAREPLIX_ENV=production" in command, name
    for name in ("careplix-studio", "careplix-internal"):
        command = deploys[name]
        assert "PAYER_SANDBOX_URL=," in command and "SEED_DEMO_DATA=false" in command
        assert "STEDI_ENV=production" in command and "NEXHEALTH_ENV=production" in command
        assert "STEDI_PROD_API_KEY=cpx-stedi-prod-api-key:latest" in command
        assert "STEDI_TEST_API_KEY" not in command and "OPENEMR" not in command
    assert "--service-account careplix-internal@" in deploys["careplix-internal"]
    assert "--ingress internal" in deploys["careplix-worker"]


def test_deploy_stops_before_changing_anything_when_images_or_secrets_are_missing(repo):
    root, head = repo
    result, calls = run(root, "deploy", CONFIRM_PROJECT="careplix-prod", IMAGE_MISSING="1")
    assert result.returncode == 1 and f"No images tagged {head}" in result.stderr
    assert "run deploy" not in calls
    run(root, "build")
    result, calls = run(root, "deploy", CONFIRM_PROJECT="careplix-prod", NO_SECRETS="1")
    assert result.returncode == 1 and "Missing in Secret Manager" in result.stderr
    assert "run deploy" not in calls


def test_deploy_refuses_a_dirty_build(repo):
    root, _ = repo
    result, calls = run(root, "deploy", CONFIRM_PROJECT="careplix-prod", TAG="abc1234-dirty")
    assert result.returncode == 1 and "uncommitted changes" in result.stderr
    assert "run deploy" not in calls


def test_deploy_checks_every_secret_it_mounts():
    required = set(re.search(r'REQUIRED_SECRETS="([^"]*)"', TEXT).group(1).split())
    referenced = set(re.findall(r"=(cpx-[a-z-]+):latest", TEXT))
    assert referenced - {"cpx-anthropic-api-key", "cpx-resend-api-key"} <= required


# --- the script and the app agree -----------------------------------------------------------------


def _assignment(name: str) -> str:
    match = re.search(rf'^\s*{name}="([^"]*)"', TEXT, re.M)
    assert match, name
    return match.group(1)


def test_the_settings_the_studio_gets_pass_the_production_rules():
    env_text = ",".join(_assignment(n) for n in ("common_env", "practice_env"))
    env_text = re.sub(r"\$\([^)]*\)|\$\{[^}]*\}|\$\w+", "x", env_text)
    values = dict(item.split("=", 1) for item in env_text.split(",") if "=" in item)
    settings = Settings(
        _env_file=None,
        **{k.lower(): v for k, v in values.items() if k.lower() in Settings.model_fields},
        session_secret="from-secret-manager",
        data_encryption_key="from-secret-manager",
        stedi_prod_api_key="from-secret-manager",
    )
    assert settings.careplix_env == "production"
    assert environment.problems(settings) == []
