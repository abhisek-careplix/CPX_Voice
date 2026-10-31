import json
import logging

from careplix_common.safe_logging import JsonFormatter, scrub


def test_scrub_removes_contact_and_identifier_shapes():
    text = "call +1 (202) 555-0142 or 202.555.0142, mail a.b@example.org, ssn 123-45-6789"
    out = scrub(text)
    assert "555" not in out
    assert "example.org" not in out
    assert "6789" not in out


def test_scrub_keeps_short_numbers_and_ids():
    assert scrub("attempt 42 took 812 ms") == "attempt 42 took 812 ms"


def _record(msg, **extra):
    record = logging.LogRecord("t", logging.INFO, __file__, 1, msg, None, None)
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def test_formatter_drops_non_allowlisted_extras():
    line = json.loads(
        JsonFormatter().format(_record("dial failed", attempt_id="a1", patient_name="X"))
    )
    assert line["attempt_id"] == "a1"
    assert "patient_name" not in line
