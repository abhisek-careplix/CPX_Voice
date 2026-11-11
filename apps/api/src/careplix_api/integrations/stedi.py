"""Stedi clearinghouse client: eligibility (270/271), payer search and webhook verification.

Checked against Stedi's docs (stedi.com/docs/healthcare, Nov 2026):

- The 2026-06-01 eligibility API groups benefits under ``plans[].benefits`` (``statuses``,
  ``coPayment``, ``coInsurance``, ``deductible``, ``outOfPocket``, ``costContainment``, …). Any
  status starting with ``ACTIVE`` is active coverage, and a benefit line without a status line
  also means active coverage (eligibility-active-coverage-benefits).
- Payer rejections are AAA errors in ``errors[]`` (``code``, ``followupAction``, ``location``) on
  HTTP 200, 400 and 429. 42 (unless the follow-up is C, N or S), 80 and HTTP 429 are worth
  retrying; the rest need a corrected request (eligibility-troubleshooting).
- Stedi holds a check open for up to 120 s while it retries the payer, so the client waits 125 s.
  The real-time endpoints share one pool of 50 requests in flight per account.
- Test keys answer only Stedi's published mock requests; production keys reach real payers. A
  client is built for one mode and refuses the other mode's requests: a mock member never goes
  out on a production key, and a patient never goes out on a test key.
"""

from __future__ import annotations

import asyncio
import base64
import calendar
import hashlib
import hmac
import re
import time
import weakref
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

ELIGIBILITY_PATH = "/2026-06-01/eligibility-check"
PAYER_SEARCH_PATH = "/2024-04-01/payers/search"
USER_AGENT = "CarePlix-Voice/1.0"
TIMEOUT_S = 125.0  # Stedi gives up on the payer after 120 s
TEST, PRODUCTION = "test", "production"
CMS = "CMS"  # Medicare fee-for-service (HETS); never Medicare Advantage

# --- Stedi's published mock requests (api-reference/mock-requests-eligibility-checks) -----------
# Test keys answer only these exact subscribers; any other value is an error.
MOCK_PROVIDER_NPI = "1999999984"
MOCK_PROVIDER_NAME = "CarePlix Test Provider"


@dataclass(frozen=True)
class Mock:
    label: str
    payer_id: str
    first_name: str
    last_name: str
    member_id: str
    date_of_birth: str


MOCKS: dict[str, Mock] = {
    "aetna_active": Mock(
        "Aetna · active coverage", "60054", "Jane", "Doe", "AETNA12345", "2004-04-04"
    ),
    "cms_active": Mock(
        "Medicare (CMS) · active coverage", CMS, "Jane", "Doe", "1AA2CC3DD45", "1955-05-05"
    ),
    "uhc_inactive": Mock(
        "UnitedHealthcare · inactive coverage", "87726", "Jane", "Doe", "UHCINACTIVE", "1971-01-01"
    ),
    "humana_medicare_advantage": Mock(
        "Humana · Medicare Advantage", "61101", "Jane", "Doe", "HUMANA123", "1975-05-05"
    ),
    "aaa_42": Mock(
        "AAA 42 · payer can't respond", "87726", "Jane", "Doe", "UHCAAA42", "2001-01-01"
    ),
    "aaa_43": Mock(
        "AAA 43 · provider ID invalid", "87726", "Jane", "Doe", "UHCAAA43", "1970-01-01"
    ),
    "aaa_72": Mock("AAA 72 · member ID invalid", "87726", "John", "Doe", "UHCAAA72", "1990-01-01"),
    "aaa_73": Mock(
        "AAA 73 · member name invalid", "87726", "John", "Doe", "UHCAAA73", "1990-01-01"
    ),
    "aaa_75": Mock("AAA 75 · member not found", "87726", "Jane", "Doe", "UHCAAA75", "1990-01-01"),
    "aaa_79": Mock(
        "AAA 79 · participant invalid", "87726", "John", "Doe", "UHCAAA79", "1970-01-01"
    ),
}
AUTO = "auto"  # test mode picks the mock that matches the plan (Medicare, Medicare Advantage, …)
_MOCK_KEYS = {(m.payer_id, m.member_id, m.date_of_birth) for m in MOCKS.values()}


def mock_for_member(member_id: str | None) -> str | None:
    """The mock scenario whose member ID a tester typed (``AETNA12345``), if any."""
    key = re.sub(r"[\s-]", "", str(member_id or "")).upper()
    return next((k for k, m in MOCKS.items() if m.member_id.upper() == key), None)


def mock_request(scenario: str) -> dict[str, Any]:
    """Stedi's published mock request, unchanged (mocks support STC 30 and no service date)."""
    m = MOCKS[scenario]
    return {
        "payerId": m.payer_id,
        "provider": {"name": {"organization": MOCK_PROVIDER_NAME}, "npi": MOCK_PROVIDER_NPI},
        "subscriber": {
            "name": {"person": {"firstName": m.first_name, "lastName": m.last_name}},
            "memberId": m.member_id,
            "dateOfBirth": m.date_of_birth,
        },
        "encounter": {"services": [{"value": "30", "system": "STC"}]},
    }


def is_mock(request: dict[str, Any]) -> bool:
    """Exactly one of Stedi's published mock members, and nobody else."""
    subscriber = request.get("subscriber") or {}
    return (
        not request.get("dependent")
        and (
            str(request.get("payerId")),
            str(subscriber.get("memberId")),
            str(subscriber.get("dateOfBirth")),
        )
        in _MOCK_KEYS
    )


def looks_mock(request: dict[str, Any]) -> bool:
    """Any trace of mock data: a mock member or Stedi's mock NPI."""
    subscriber = request.get("subscriber") or {}
    return (
        is_mock(request)
        or (request.get("provider") or {}).get("npi") == MOCK_PROVIDER_NPI
        or mock_for_member(subscriber.get("memberId")) is not None
    )


# --- identifiers ---------------------------------------------------------------------------------

# CMS MBI format: 11 characters; letters exclude S, L, O, I, B and Z.
_A, _AN = "[AC-HJKMNP-RT-Y]", "[AC-HJKMNP-RT-Y0-9]"
_MBI = re.compile(f"[1-9]{_A}{_AN}[0-9]{_A}{_AN}[0-9]{_A}{_A}[0-9][0-9]")
_HICN = re.compile(r"\d{9}[A-Z]{1,2}\d?")  # retired SSN-based Medicare number
_RRB = re.compile(r"[A-Z]{1,3}\d{6}(\d{3})?")  # retired Railroad Retirement Board number


class InvalidMbi(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def normalize_mbi(value: Any) -> str:
    """Uppercase, no dashes or spaces, 11 characters in the CMS MBI format."""
    text = re.sub(r"[\s-]", "", str(value or "")).upper()
    if _MBI.fullmatch(text):
        return text
    if _HICN.fullmatch(text) or _RRB.fullmatch(text):
        raise InvalidMbi(
            "legacy_medicare_number",
            "That is an old Medicare number. CMS needs the 11-character Medicare Beneficiary "
            "Identifier from the patient's current Medicare card.",
        )
    raise InvalidMbi(
        "invalid_mbi",
        "That isn't a valid Medicare Beneficiary Identifier (11 characters from the Medicare "
        "card, for example 1EG4-TE5-MK73).",
    )


def valid_npi(value: Any) -> bool:
    """10 digits with a valid check digit (Luhn over the 80840 prefix)."""
    text = str(value or "")
    if not re.fullmatch(r"\d{10}", text):
        return False
    total = 0
    for i, ch in enumerate(reversed("80840" + text)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def service_date(visit: date | None, today: date, payer_id: str) -> str | None:
    """The visit date to ask about, when payers accept it: today or later, up to the end of this
    month (CMS: the end of next month). Otherwise omitted, so the payer answers for today."""
    if visit is None or visit <= today:
        return None
    year, month = today.year, today.month
    if payer_id == CMS:
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    return visit.isoformat() if visit <= last else None


def patient_request(
    *,
    payer_id: str,
    provider_npi: str,
    subscriber: dict[str, str],
    dependent: dict[str, str] | None = None,
    service_type: str = "30",
    service_date: str | None = None,
    pin: str | None = None,
) -> dict[str, Any]:
    """A real patient's check. ``provider.name`` is left out on purpose: Stedi fills in the NPI's
    NPPES name, which is what payers match on (send-eligibility-checks#provider-name)."""
    if payer_id == CMS:
        subscriber = {**subscriber, "member_id": normalize_mbi(subscriber["member_id"])}
    if provider_npi == MOCK_PROVIDER_NPI:
        raise ValueError("Stedi's mock NPI is for test mode only.")
    provider: dict[str, Any] = {"npi": provider_npi}
    if pin:
        provider["additionalInformation"] = {"personalIdentificationNumber": pin}
    body: dict[str, Any] = {
        "payerId": payer_id,
        "provider": provider,
        "subscriber": _person(subscriber),
        "encounter": {"services": [{"value": service_type, "system": "STC"}]},
    }
    if subscriber.get("member_id"):
        body["subscriber"]["memberId"] = subscriber["member_id"]
    if dependent:
        body["dependent"] = _person(dependent)
        if dependent.get("relation") in ("SPOUSE", "CHILD", "OTHER_ADULT"):
            body["dependent"]["relationToSubscriber"] = dependent["relation"]
    if service_date:
        body["encounter"]["dates"] = {"service": {"start": service_date}}
    return body


def _person(values: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": {"person": {"firstName": values["first_name"], "lastName": values["last_name"]}}
    }
    if values.get("date_of_birth"):
        out["dateOfBirth"] = str(values["date_of_birth"])[:10]
    return out


# --- AAA errors ----------------------------------------------------------------------------------

RETRY, CORRECT, NOT_FOUND = "retry", "correct", "not_found"
_FOLLOWUP = {
    "pleasecorrectandresubmit": "C",
    "resubmissionnotallowed": "N",
    "pleaseresubmitoriginaltransaction": "P",
    "resubmissionallowed": "R",
    "donotresubmitinquiryinitiatedtoathirdparty": "S",
    "pleasewait30daysandresubmit": "W",
    "pleasewait10daysandresubmit": "X",
    "donotresubmitwewillholdyourrequestandrespondagainshortly": "Y",
}
_NOT_FOUND = {"67", "68", "75", "76"}  # payer can't find (or can't single out) the member
_AAA_TEXT = {
    "04": "Too many patients in one request.",
    "15": "The payer needs information the request didn't include.",
    "41": "The payer restricts access for this NPI: the provider may need to enrol with it.",
    "42": "The payer can't respond right now.",
    "43": "The payer doesn't recognise the billing NPI.",
    "44": "The payer doesn't recognise the provider name.",
    "50": "The provider isn't eligible for eligibility checks with this payer.",
    "51": "The provider isn't on file with this payer.",
    "56": "The payer rejected the date of service.",
    "57": "The date of service is missing or invalid.",
    "58": "The date of birth is missing or invalid.",
    "62": "The date of service is outside what the payer allows.",
    "63": "The payer doesn't accept a future date of service.",
    "64": "The patient ID is missing or invalid.",
    "65": "The payer didn't match the patient's name.",
    "67": "The payer couldn't find the patient.",
    "68": "The payer found more than one matching patient.",
    "71": "The date of birth doesn't match the payer's record.",
    "72": "The member ID is missing or invalid.",
    "73": "The payer didn't match the member's name.",
    "75": "The payer couldn't find the member.",
    "76": "The payer found more than one member with this ID.",
    "79": "Stedi couldn't route the request, or the payer couldn't identify a participant.",
    "80": "The payer didn't respond.",
    "97": "The payer rejected the provider's address.",
    "T4": "The payer ID is missing or invalid.",
}


def _key(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def parse_errors(body: Any) -> list[dict[str, Any]]:
    """``errors[]`` from any status: code, description, follow-up (label and X12 code), location.
    ``possibleResolutions`` is left out: Stedi says its wording changes and isn't for logic."""
    out = []
    for e in (body or {}).get("errors") or [] if isinstance(body, dict) else []:
        if not isinstance(e, dict):
            continue
        followup = str(e.get("followupAction") or "")
        code = followup if len(followup) == 1 else _FOLLOWUP.get(_key(followup))
        out.append(
            {
                "code": str(e.get("code") or ""),
                "description": e.get("description"),
                "followup": followup or None,
                "followup_code": code,
                "location": e.get("location"),
            }
        )
    return out


def classify(
    errors: list[dict[str, Any]], http_status: int | None = None, status: str | None = None
) -> str | None:
    """retry | correct | not_found, or None when nothing went wrong."""
    if http_status == 429 or status in ("timeout", "network", "busy"):
        return RETRY
    if http_status is not None and http_status >= 500:
        return RETRY
    for e in errors:
        if e["code"] == "80" or (e["code"] == "42" and e["followup_code"] not in ("C", "N", "S")):
            return RETRY  # also when 79 comes with a retryable 42
    if any(e["code"] in _NOT_FOUND for e in errors):
        return NOT_FOUND
    if errors or status in ("auth", "invalid", "http", "refused"):
        return CORRECT
    return None


def error_message(errors: list[dict[str, Any]], payer_id: str | None) -> str | None:
    """One sentence for the front desk, the most actionable error first."""
    codes = [e["code"] for e in errors]
    if payer_id == CMS and "41" in codes:
        return "CMS attestation/enrollment required for this NPI."
    if payer_id == CMS and "97" in codes:
        return (
            "CMS suspended this NPI's attestation (its attestation email bounced): the provider "
            "must update the email with their Medicare Administrative Contractor."
        )
    for e in errors:
        if e["code"] == "42" and e["followup_code"] in ("C", "N", "S"):
            return "The payer rejected the request format (AAA 42): contact Stedi support."
    for e in errors:
        text = _AAA_TEXT.get(e["code"]) or e.get("description")
        if text:
            return f"{text} (AAA {e['code']})"
    return None


class StediError(Exception):
    def __init__(
        self,
        status: str,
        message: str,
        http_status: int | None = None,
        errors: list[dict[str, Any]] | None = None,
    ):
        super().__init__(message)
        self.status, self.message, self.http_status = status, message, http_status
        self.errors = errors or []

    @property
    def outcome(self) -> str:
        return classify(self.errors, self.http_status, self.status) or CORRECT


# --- client --------------------------------------------------------------------------------------

_gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _gate(limit: int) -> asyncio.Semaphore:
    """One cap on Stedi requests in flight per process (its pool is shared per account)."""
    loop = asyncio.get_running_loop()
    gate = _gates.get(loop)
    if gate is None:
        gate = _gates[loop] = asyncio.Semaphore(max(1, limit))
    return gate


class StediClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        mode: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = TIMEOUT_S,
        max_in_flight: int = 8,
    ):
        if mode not in (TEST, PRODUCTION):
            raise ValueError(f"unknown Stedi mode {mode!r}")
        self.base_url, self.mode = base_url.rstrip("/"), mode
        self.calls, self._max_in_flight = 0, max_in_flight
        self._http = httpx.AsyncClient(
            timeout=timeout_s,
            transport=transport,
            headers={
                "Authorization": f"Key {api_key}",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> tuple[dict[str, Any], int, int]:
        started = time.perf_counter()
        self.calls += 1
        try:
            async with _gate(self._max_in_flight):
                response = await self._http.request(method, f"{self.base_url}{path}", **kwargs)
        except httpx.TimeoutException:
            raise StediError("timeout", f"{method} {path} timed out") from None
        except httpx.HTTPError as exc:
            raise StediError("network", type(exc).__name__) from None
        ms = round((time.perf_counter() - started) * 1000)
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {}
        return (body if isinstance(body, dict) else {}), response.status_code, ms

    @staticmethod
    def _raise(body: dict[str, Any], http_status: int, reason: str) -> None:
        message = str(body.get("message") or reason or "Stedi error")
        errors = parse_errors(body)
        if http_status in (401, 403):
            raise StediError("auth", message, http_status)
        if http_status == 429:
            raise StediError("busy", message, http_status, errors)
        if errors:
            raise StediError("aaa", message, http_status, errors)
        raise StediError("invalid" if http_status < 500 else "http", message, http_status)

    async def eligibility(
        self, request: dict[str, Any], *, forwarded_for: str | None = None
    ) -> tuple[dict[str, Any], int]:
        """One real-time check. A 200 can still carry AAA errors; 400/429 raise with them."""
        if self.mode == PRODUCTION and looks_mock(request):
            raise StediError("refused", "A mock member can't be sent with a production key.")
        if self.mode == TEST and not is_mock(request):
            raise StediError("refused", "A test key only sends Stedi's published mock requests.")
        headers = {"X-Forwarded-For": forwarded_for} if forwarded_for else None
        body, http_status, ms = await self._request(
            "POST", ELIGIBILITY_PATH, json=request, headers=headers
        )
        if http_status >= 400:
            self._raise(body, http_status, "")
        return body, ms

    async def search_payers(
        self,
        query: str,
        *,
        eligibility_only: bool = True,
        programs: list[str] | None = None,
        states: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"query": query}
        if eligibility_only:
            params["eligibilityCheck"] = "SUPPORTED"
        if programs:
            params["programs"] = programs
        if states:
            params["operatingStates"] = states
        body, http_status, _ = await self._request("GET", PAYER_SEARCH_PATH, params=params)
        if http_status >= 400:
            self._raise(body, http_status, "")
        return [item.get("payer") or {} for item in body.get("items") or []]


# --- response summary ----------------------------------------------------------------------------

BENEFIT_GROUPS = (
    "coPayment",
    "coInsurance",
    "deductible",
    "outOfPocket",
    "limitations",
    "unlimited",
    "costContainment",
    "spendDown",
)
INDIVIDUAL = ("INDIVIDUAL", None, "EMPLOYEE_ONLY")
FAMILY = ("FAMILY", "EMPLOYEE_AND_SPOUSE", "EMPLOYEE_AND_CHILDREN", "SPOUSE_AND_CHILDREN")
TOTAL_PERIODS = ("CALENDAR_YEAR", "SERVICE_YEAR", "YEARS", "CONTRACT")
IN_NETWORK = ("IN_NETWORK", "IN_AND_OUT_OF_NETWORK", None)
OUT_OF_NETWORK = ("OUT_OF_NETWORK", "IN_AND_OUT_OF_NETWORK")
MEDICARE_AB = {
    "MEDICARE_PART_A",
    "MEDICARE_PART_B",
    "HEALTH_MAINTENANCE_ORGANIZATION_HMO_MEDICARE_RISK",
}
_MA_BILL = re.compile(r"MA\s+Bill\s+Option\s+Code\s*[-:]?\s*([A-Z0-9])\b", re.I)


def _lines(response: dict[str, Any], group: str) -> list[dict[str, Any]]:
    return [
        line
        for plan in response.get("plans") or []
        for line in (plan.get("benefits") or {}).get(group) or []
        if isinstance(line, dict)
    ]


def _svc(line: dict[str, Any]) -> str | None:
    return (line.get("service") or {}).get("value")


def _pick(
    lines: list[dict[str, Any]],
    services: tuple[str | None, ...],
    *,
    periods: tuple[str, ...] | None = None,
    networks: tuple[str | None, ...] = IN_NETWORK,
    levels: tuple[str | None, ...] | None = INDIVIDUAL,
) -> dict[str, Any] | None:
    """The best line: earlier services, networks, coverage levels and periods win."""
    for service in services:
        for network in networks:
            for level in levels or (Ellipsis,):
                for period in periods or (Ellipsis,):
                    for line in lines:
                        if (
                            _svc(line) == service
                            and (line.get("network") or {}).get("indicator") == network
                            and (level is Ellipsis or line.get("coverageLevel") == level)
                            and (period is Ellipsis or line.get("timePeriod") == period)
                        ):
                            return line
    return None


def _accumulator(
    lines: list[dict[str, Any]],
    services: tuple[str | None, ...],
    networks: tuple[str | None, ...],
    levels: tuple[str | None, ...],
) -> dict[str, Any]:
    total = _pick(lines, services, periods=TOTAL_PERIODS, networks=networks, levels=levels)
    left = _pick(lines, services, periods=("REMAINING",), networks=networks, levels=levels)
    return {
        "total": (total or {}).get("amount"),
        "remaining": (left or {}).get("amount"),
    }


def _auth_flags(node: Any, found: list[str], service: str | None = None) -> None:
    """Collect services the payer marks as needing prior authorization, in any response layout."""
    if isinstance(node, dict):
        service = (node.get("service") or {}).get("definition") or service
        for key, value in node.items():
            lowered = key.lower()
            flagged = ("auth" in lowered or "certification" in lowered) and value in (
                True,
                "Y",
                "YES",
                "REQUIRED",
            )
            if flagged and service and service not in found:
                found.append(service)
            _auth_flags(value, found, service)
    elif isinstance(node, list):
        for item in node:
            _auth_flags(item, found, service)


def _entity(entity: dict[str, Any]) -> dict[str, Any]:
    name = entity.get("name") or {}
    if isinstance(name, dict) and name.get("person"):
        person = name["person"]
        label = " ".join(x for x in (person.get("firstName"), person.get("lastName")) if x)
    else:
        label = name.get("organization") if isinstance(name, dict) else name
    phone = next(
        (p for c in entity.get("contacts") or [] for p in c.get("phoneNumbers") or []), None
    )
    return {
        "name": label or None,
        "payer_id": entity.get("payerId"),
        "phone": phone,
        "type": entity.get("type"),
    }


def _dates(person: dict[str, Any]) -> dict[str, Any] | None:
    dates = person.get("dates") or {}
    span = dates.get("plan") or dates.get("eligibility") or dates.get("policy")
    return {"start": span.get("start"), "end": span.get("end")} if span else None


def _mbi_returned(response: dict[str, Any]) -> bool:
    people = [response.get("subscriber") or {}, response.get("dependent") or {}]
    if any((p.get("additionalInformation") or {}).get("mbi") for p in people):
        return True
    return any(
        (line.get("additionalInformation") or {}).get("mbi")
        for group in ("statuses", *BENEFIT_GROUPS)
        for line in _lines(response, group)
    )


def _percent(value: Any) -> str | None:
    """X12 percentages are fractions ("0.2" is 20 %); report them as percent."""
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number * 100 if number <= 1 else number:g}"


def _medicare_part(line: dict[str, Any]) -> str | None:
    """CMS keys Part A and Part B lines on insuranceType, or names the part in the description
    (QMB deductible lines say "Medicare Part A" there)."""
    kind = line.get("insuranceType")
    text = str(line.get("planCoverageDescription") or "").lower()
    if kind == "MEDICARE_PART_A" or "part a" in text:
        return "a"
    if kind == "MEDICARE_PART_B" or "part b" in text:
        return "b"
    return None


def _medicare_parts(response: dict[str, Any], service_type: str) -> dict[str, Any]:
    """Part A (deductible per benefit period: EPISODE) and Part B (CALENDAR_YEAR), each with its
    REMAINING accumulator and coinsurance. Detailed utilisation (days, therapy, preventive
    next-eligible dates) is a later step: add it here, one part at a time."""
    out: dict[str, Any] = {}
    for part, period in (("a", "EPISODE"), ("b", "CALENDAR_YEAR")):
        statuses = [
            s
            for s in _lines(response, "statuses")
            if _medicare_part(s) == part and _svc(s) in (service_type, "30")
        ]
        deductible = [d for d in _lines(response, "deductible") if _medicare_part(d) == part]
        coins = [c for c in _lines(response, "coInsurance") if _medicare_part(c) == part]
        if not (statuses or deductible or coins):
            continue
        periods = (period, *TOTAL_PERIODS)
        total = next((d.get("amount") for d in deductible if d.get("timePeriod") in periods), None)
        left = next(
            (d.get("amount") for d in deductible if d.get("timePeriod") == "REMAINING"), None
        )
        since = next(
            (((s.get("dates") or {}).get("plan") or {}).get("start") for s in statuses), None
        )
        out[f"part_{part}"] = {
            "status": next((str(s.get("status")) for s in statuses if s.get("status")), None),
            "since": since,
            "deductible": total,
            "deductible_remaining": left,
            "coinsurance_pct": _percent(coins[0].get("percent")) if coins else None,
        }
    return out


def _payer_name(response: dict[str, Any]) -> str | None:
    payer = response.get("payer") or {}
    name = payer.get("name")
    return name.get("organization") if isinstance(name, dict) else name


def summarize_eligibility(
    response: dict[str, Any], *, service_type: str = "30", payer_id: str | None = None
) -> dict[str, Any]:
    """Outcome fields only (status, plan, amounts, redirects): no member identifiers (SEC08)."""
    payer_id = payer_id or response.get("payerId")
    statuses = _lines(response, "statuses")
    wanted = [s for s in statuses if _svc(s) == service_type] or statuses
    values = [str(s.get("status") or "") for s in wanted]
    status_values = sorted({str(s.get("status")) for s in statuses if s.get("status")})
    if not status_values:  # legacy layout
        status_values = sorted(
            {
                "ACTIVE_COVERAGE" if s.get("statusCode") == "1" else str(s.get("status"))
                for s in response.get("planStatus") or []
            }
        )
        values = status_values
    errors = parse_errors(response)
    has_benefits = any(_lines(response, g) for g in BENEFIT_GROUPS)
    if any(v.startswith("ACTIVE") for v in values):
        coverage = "active"
    elif any(v.startswith("INACTIVE") for v in values):
        coverage = "inactive"
    elif has_benefits:
        coverage = "active"  # a benefit for the service implies active coverage
    elif errors:
        coverage = "error"
    else:
        coverage = "unknown"

    plan = next((p.get("name") for p in response.get("plans") or [] if p.get("name")), None)
    plan = plan or next(
        (s.get("planCoverageDescription") for s in wanted if s.get("planCoverageDescription")),
        None,
    )
    office = tuple(dict.fromkeys(("98", service_type, "30")))
    every = (service_type, "30", None)
    copays, coins = _lines(response, "coPayment"), _lines(response, "coInsurance")
    deductible, oop = _lines(response, "deductible"), _lines(response, "outOfPocket")
    copay = _pick(copays, office, levels=None)
    copay_oon = _pick(copays, office, networks=OUT_OF_NETWORK, levels=None)
    coinsurance = _pick(coins, office, levels=None)
    auth: list[str] = []
    _auth_flags(response.get("plans") or response.get("benefitsInformation") or [], auth)

    # Medicare: Advantage, secondary payer rules, plan dates and a returned MBI.
    types = {
        str(line.get("insuranceType"))
        for group in ("statuses", *BENEFIT_GROUPS)
        for line in _lines(response, group)
        if line.get("insuranceType")
    }
    contacts = _lines(response, "contactFollowingEntityForInformation")
    whole_plan = [c for c in contacts if _svc(c) in (service_type, "30", None)]
    bill_option, ma_plan = None, None
    for c in whole_plan:
        for message in c.get("messages") or []:
            match = _MA_BILL.search(str(message))
            if match:
                bill_option = match.group(1).upper()
                plans = [_entity(e) for e in c.get("relatedEntities") or []]
                ma_plan = next((p for p in plans if p["type"] == "PRIMARY_PAYER"), None) or (
                    plans[0] if plans else {}
                )
                # CMS also names the MA contract and plan benefit package (not member IDs).
                extra = (c.get("additionalInformation") or {}).get("plan") or {}
                ma_plan = {
                    **ma_plan,
                    "contract": extra.get("number"),
                    "plan_benefit_package": (c.get("network") or {}).get("id"),
                    "insurance_type": c.get("insuranceType"),
                }
    others = _lines(response, "otherOrAdditionalPayer")
    qmb = any(o.get("insuranceType") == "QUALIFIED_MEDICARE_BENEFICIARY" for o in others)
    part_d = next(
        (
            _entity(e)["name"]
            for o in others
            if _svc(o) == "88"
            for e in o.get("relatedEntities") or []
            if _entity(e)["name"]
        ),
        None,
    )
    mbi = _mbi_returned(response)
    from_cms = payer_id == CMS
    # From a commercial payer, Medicare Part A/B or an MBI means Medicare Advantage; Medicaid
    # also returns an MBI for dual-eligible patients, so that alone isn't enough there.
    dual = "MEDICAID" in types
    advantage = bool(bill_option) or (
        not from_cms and (bool(types & MEDICARE_AB) or (mbi and not dual))
    )

    # Medicaid: cost containment, spend down, stolen card and managed-care redirects.
    redirect = None
    if from_cms and bill_option:
        redirect = {"kind": "medicare_advantage", **(ma_plan or {}), "bill_option": bill_option}
    else:
        mco = [
            _entity(e)
            for line in _lines(response, "managedCareCoordinator")
            for e in line.get("relatedEntities") or []
        ] + [
            _entity(e)
            for line in wanted + whole_plan
            for e in line.get("relatedEntities") or []
            if e.get("type") == "MANAGED_CARE_ORGANIZATION"
        ]
        named = [
            _entity(e)
            for line in whole_plan
            for e in line.get("relatedEntities") or []
            if e.get("type") in ("PAYER", "PRIMARY_PAYER")
        ]
        found = next((m for m in mco if m["name"]), None)
        if found:
            redirect = {"kind": "managed_care", **found}
        elif named and named[0]["name"]:
            redirect = {"kind": "contact_payer", **named[0]}
    cost = _lines(response, "costContainment")
    spend = _lines(response, "spendDown")
    patient = response.get("dependent") or response.get("subscriber") or {}
    parts = _medicare_parts(response, service_type) if from_cms else {}
    deductible_in = _accumulator(deductible, every, IN_NETWORK, INDIVIDUAL)
    if parts.get("part_b"):  # an office visit falls under Part B's calendar-year deductible
        deductible_in = {
            "total": parts["part_b"]["deductible"],
            "remaining": parts["part_b"]["deductible_remaining"],
        }
    plan_dates = _dates(patient) or _dates(response.get("subscriber") or {})
    if plan_dates is None:
        span = next((((s.get("dates") or {}).get("plan")) for s in wanted if s.get("dates")), None)
        plan_dates = {"start": span.get("start"), "end": span.get("end")} if span else None
    coinsurance_pct = _percent((coinsurance or {}).get("percent"))
    if coinsurance_pct is None and parts.get("part_b"):
        coinsurance_pct = parts["part_b"]["coinsurance_pct"]

    return {
        "coverage": coverage,
        "statuses": status_values,
        "service_type": service_type,
        "plan": plan,
        "plan_dates": plan_dates,
        "insurance_type": next(
            (s.get("insuranceType") for s in wanted if s.get("insuranceType")), None
        ),
        "office_copay": copay.get("amount") if copay else None,
        "office_copay_out_of_network": copay_oon.get("amount") if copay_oon else None,
        "office_coinsurance_pct": coinsurance_pct,
        "deductible": deductible_in,
        "deductible_family": _accumulator(deductible, every, IN_NETWORK, FAMILY),
        "deductible_out_of_network": _accumulator(deductible, every, OUT_OF_NETWORK, INDIVIDUAL),
        "out_of_pocket": _accumulator(oop, every, IN_NETWORK, INDIVIDUAL),
        "out_of_pocket_family": _accumulator(oop, every, IN_NETWORK, FAMILY),
        "out_of_pocket_out_of_network": _accumulator(oop, every, OUT_OF_NETWORK, INDIVIDUAL),
        "auth_required_services": auth,
        "payer": _payer_name(response),
        "medicare": {
            "advantage": advantage,
            "bill_option": bill_option,
            # Medicare Secondary Payer: CMS lists the primary payer under otherOrAdditionalPayer.
            "secondary": sorted(
                {
                    str(line.get("insuranceType"))
                    for line in [*others, *_lines(response, "statuses")]
                    if str(line.get("insuranceType") or "").startswith("MEDICARE_SECONDARY")
                }
            ),
            "mbi_returned": mbi,
            "qmb": qmb,  # Qualified Medicare Beneficiary: no Medicare cost sharing to collect
            "part_d": part_d,
            **parts,
        },
        "medicaid": {
            "cost_containment": cost[0].get("amount") if cost else None,
            "spend_down": spend[0].get("amount") if spend else None,
            "card_reported_stolen": bool(_lines(response, "cardReportedStolen")),
        },
        "redirect": redirect,
        "other_payers": [
            n
            for n in (
                _entity(e).get("name")
                for line in _lines(response, "otherOrAdditionalPayer")
                for e in line.get("relatedEntities") or []
            )
            if n
        ],
        "errors": [e["code"] for e in errors],
        "error_details": errors,
        "error_class": classify(errors) if coverage == "error" else None,
        "message": error_message(errors, payer_id) if coverage == "error" else None,
    }


def verify_webhook(
    secret: str, headers: dict[str, str], body: bytes, tolerance_s: int = 300
) -> bool:
    """Standard Webhooks check: ``webhook-id``, ``webhook-timestamp``, ``webhook-signature``."""
    msg_id, stamp, signatures = (
        headers.get("webhook-id"),
        headers.get("webhook-timestamp"),
        headers.get("webhook-signature"),
    )
    if not (secret and msg_id and stamp and signatures):
        return False
    try:
        if abs(time.time() - int(stamp)) > tolerance_s:
            return False
        key = base64.b64decode(secret.removeprefix("whsec_"))
    except ValueError:
        return False
    expected = base64.b64encode(
        hmac.new(key, f"{msg_id}.{stamp}.".encode() + body, hashlib.sha256).digest()
    ).decode()
    return any(hmac.compare_digest(sig.split(",", 1)[-1], expected) for sig in signatures.split())
