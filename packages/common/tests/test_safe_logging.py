import json
import logging

from careplix_common.safe_logging import (
    HttpxFailuresOnly,
    JsonFormatter,
    scrub,
    severity,
    tag_severity,
)


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


def _at(level, msg, name="t"):
    return logging.LogRecord(name, level, __file__, 1, msg, None, None)


def test_formatter_uses_cloud_logging_field_names():
    line = json.loads(JsonFormatter().format(_at(logging.ERROR, "claim failed")))
    assert line["severity"] == "ERROR"
    assert line["message"] == "claim failed"
    assert "time" in line
    assert "level" not in line and "msg" not in line


def test_severity_rounds_custom_levels_down():
    assert severity(logging.WARNING) == "WARNING"
    assert severity(logging.CRITICAL + 5) == "CRITICAL"
    assert severity(5) == "DEBUG"  # LiveKit TRACE


def test_httpx_filter_keeps_only_failed_responses_as_warnings():
    keep = HttpxFailuresOnly()
    ok = _at(logging.INFO, 'HTTP Request: GET https://nexhealth.info/a "HTTP/1.1 200 OK"', "httpx")
    bad = _at(logging.INFO, 'HTTP Request: POST https://x/y "HTTP/1.1 429 Too Many"', "httpx")
    assert keep.filter(ok) is False
    assert keep.filter(bad) is True
    assert bad.levelno == logging.WARNING
    line = json.loads(JsonFormatter().format(bad))
    assert line["severity"] == "WARNING"


def test_tag_severity_adds_attribute_once():
    before = logging.getLogRecordFactory()
    try:
        tag_severity()
        tag_severity()
        record = logging.getLogRecordFactory()("t", logging.ERROR, __file__, 1, "x", None, None)
        assert record.severity == "ERROR"
    finally:
        logging.setLogRecordFactory(before)
