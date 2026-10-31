import pytest

from careplix_worker import main
from careplix_worker.gcp import CredentialConfigError, credential_source, load_credentials
from careplix_worker.settings import WorkerSettings


def _settings(**kw) -> WorkerSettings:
    return WorkerSettings(_env_file=None, **kw)


def test_adc_used_when_no_key_supplied():
    settings = _settings()
    assert load_credentials(settings) is None
    assert credential_source(settings) == "application_default"


@pytest.mark.parametrize("raw", ["not json", '{"type": "authorized_user"}'])
def test_bad_service_account_json_rejected_without_echoing_it(raw):
    with pytest.raises(CredentialConfigError) as exc:
        load_credentials(_settings(google_service_account_json=raw))
    assert raw not in str(exc.value)


def test_missing_model_config_is_reported():
    assert _settings().missing_model_config() == [
        "GCP_PROJECT",
        "VERTEX_LOCATION",
        "GEMINI_LIVE_MODEL",
    ]
    assert (
        _settings(
            gcp_project="p", vertex_location="l", gemini_live_model="m"
        ).missing_model_config()
        == []
    )


def test_metadata_logging_exposes_keys_only():
    assert main._metadata_keys('{"probe": "g0-browser", "attempt_id": "x"}') == [
        "attempt_id",
        "probe",
    ]
    assert main._metadata_keys("not json") == ["<invalid-json>"]
    assert main._metadata_keys("") == []


def test_spike_instructions_stay_synthetic_and_disclose_automation():
    text = main.G0_INSTRUCTIONS.lower()
    assert "automated assistant" in text
    assert "synthetic data only" in text
