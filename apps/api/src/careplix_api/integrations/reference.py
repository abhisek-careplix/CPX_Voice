"""Public reference-data connectors: NPPES NPI Registry, NLM Clinical Tables, CMS Coverage API.

All three are free, public and need no credentials. They answer "is this fact about the case
real?" before anyone dials: provider identity, diagnosis/supply codes, and which Medicare coverage
policies are likely to drive documentation requests. Only identifiers and outcomes are audited.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

NPPES_URL = "https://npiregistry.cms.hhs.gov/api/"
NLM_URL = "https://clinicaltables.nlm.nih.gov/api"
CMS_COVERAGE_URL = "https://api.coverage.cms.gov/v1"

# Built-in definitions used to seed workspace connections (kind → name, base URL).
REFERENCE_SYSTEMS = {
    "npi": ("NPPES NPI Registry (CMS)", NPPES_URL),
    "icd10": ("NLM Clinical Tables: ICD-10-CM", f"{NLM_URL}/icd10cm/v3"),
    "hcpcs": ("NLM Clinical Tables: HCPCS Level II", f"{NLM_URL}/hcpcs/v3"),
    "cms_coverage": ("CMS Coverage API (NCD/LCD)", CMS_COVERAGE_URL),
}


@dataclass
class CheckResult:
    capability: str
    status: str  # confirmed | invalid | unverifiable | skipped | error
    subject: str
    detail: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ReferenceClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, timeout_s: float = 12.0):
        self._http = httpx.AsyncClient(
            timeout=timeout_s, transport=transport, follow_redirects=True
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> tuple[Any, int]:
        started = time.perf_counter()
        response = await self._http.get(url, params=params)
        response.raise_for_status()
        return response.json(), round((time.perf_counter() - started) * 1000)

    # --- NPPES -----------------------------------------------------------------------------------

    async def npi(self, npi: str) -> CheckResult:
        if not re.fullmatch(r"\d{10}", npi):
            return CheckResult(
                "provider.npi.validate",
                "invalid",
                npi,
                {"reason": "NPIs are 10 digits"},
                source="nppes",
            )
        if npi.startswith("9"):
            # Synthetic fixtures use a reserved range that never collides with real providers.
            return CheckResult(
                "provider.npi.validate",
                "skipped",
                npi,
                {"reason": "synthetic NPI (reserved range)"},
                source="nppes",
            )
        body, ms = await self._get(NPPES_URL, {"version": "2.1", "number": npi})
        results = body.get("results") or []
        if not results:
            return CheckResult(
                "provider.npi.validate",
                "invalid",
                npi,
                {"reason": "not found in NPPES"},
                ms,
                "nppes",
            )
        r = results[0]
        basic = r.get("basic", {})
        name = basic.get("organization_name") or " ".join(
            p for p in (basic.get("first_name"), basic.get("last_name")) if p
        )
        location = next(
            (a for a in r.get("addresses", []) if a.get("address_purpose") == "LOCATION"), {}
        )
        taxonomy = next((t for t in r.get("taxonomies", []) if t.get("primary")), {})
        return CheckResult(
            "provider.npi.validate",
            "confirmed" if basic.get("status", "A") == "A" else "invalid",
            npi,
            {
                "name": name,
                "type": r.get("enumeration_type"),
                "taxonomy": taxonomy.get("desc"),
                "city": location.get("city"),
                "state": location.get("state"),
                "fax": location.get("fax_number"),
                "status": basic.get("status"),
            },
            ms,
            "nppes",
        )

    # --- NLM Clinical Tables ---------------------------------------------------------------------

    async def _nlm_exact(self, table: str, code: str, capability: str) -> CheckResult:
        body, ms = await self._get(
            f"{NLM_URL}/{table}/v3/search", {"sf": "code,name", "terms": code, "maxList": 20}
        )
        rows = body[3] if isinstance(body, list) and len(body) > 3 and body[3] else []
        match = next((row for row in rows if row and row[0].upper() == code.upper()), None)
        if match:
            return CheckResult(
                capability, "confirmed", code, {"display": match[1]}, ms, f"nlm-{table}"
            )
        suggestions = [row[0] for row in rows[:5] if row]
        reason = "not a billable code; use a more specific code" if suggestions else "not found"
        return CheckResult(
            capability,
            "invalid",
            code,
            {"reason": reason, "suggestions": suggestions},
            ms,
            f"nlm-{table}",
        )

    async def icd10(self, code: str) -> CheckResult:
        return await self._nlm_exact("icd10cm", code, "code.icd10.validate")

    async def hcpcs(self, code: str) -> CheckResult:
        if re.fullmatch(r"\d{4}[0-9A-Z]", code):
            return CheckResult(
                "code.hcpcs.validate",
                "skipped",
                code,
                {"reason": "CPT (Level I) is AMA-licensed; not validated"},
                source="nlm-hcpcs",
            )
        return await self._nlm_exact("hcpcs", code, "code.hcpcs.validate")

    # --- CMS Coverage ----------------------------------------------------------------------------

    _cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    async def _report(self, name: str) -> tuple[list[dict[str, Any]], int]:
        cached = self._cache.get(name)
        if cached and time.time() - cached[0] < 86_400:
            return cached[1], 0
        body, ms = await self._get(f"{CMS_COVERAGE_URL}/reports/{name}/")
        rows = body.get("data", [])
        ReferenceClient._cache[name] = (time.time(), rows)
        return rows, ms

    async def coverage(self, terms: list[str]) -> CheckResult:
        if not terms:
            return CheckResult(
                "coverage.policy.lookup",
                "skipped",
                "-",
                {"reason": "no coverage terms on the case"},
                source="cms-coverage",
            )
        ncds, ms1 = await self._report("national-coverage-ncd")
        lcds, ms2 = await self._report("local-coverage-final-lcds")
        lowered = [t.lower() for t in terms]

        def hit(title: str) -> bool:
            return any(t in title.lower() for t in lowered)

        found = [
            {
                "id": r.get("document_display_id"),
                "type": "NCD",
                "title": r.get("title"),
                "url": "https://www.cms.gov/medicare-coverage-database/view/ncd.aspx?ncdid="
                + str(r.get("document_id")),
            }
            for r in ncds
            if hit(r.get("title", ""))
        ][:5] + [
            {
                "id": r.get("document_display_id"),
                "type": "LCD",
                "title": r.get("title"),
                "contractor": (r.get("contractor_name_type") or "").splitlines()[0],
                "url": "https://www.cms.gov/medicare-coverage-database/view/lcd.aspx?lcdid="
                + str(r.get("document_id")),
            }
            for r in lcds
            if hit(r.get("title", "")) and "retired" not in (r.get("note") or "").lower()
        ][:5]
        return CheckResult(
            "coverage.policy.lookup",
            "confirmed" if found else "unverifiable",
            ", ".join(terms),
            {"policies": found, "reason": None if found else "no matching NCD/LCD titles"},
            ms1 + ms2,
            "cms-coverage",
        )

    async def ping(self, kind: str) -> CheckResult:
        """Live health probe used by connection verification."""
        if kind == "npi":
            return await self.npi("1003000126")
        if kind == "icd10":
            return await self.icd10("M54.50")
        if kind == "hcpcs":
            return await self.hcpcs("E0601")
        if kind == "cms_coverage":
            body, ms = await self._get(f"{CMS_COVERAGE_URL}/metadata/license-agreement/")
            ok = body.get("meta", {}).get("status", {}).get("id") == 200
            return CheckResult(
                "coverage.metadata",
                "confirmed" if ok else "error",
                "license-agreement",
                {},
                ms,
                "cms-coverage",
            )
        raise ValueError(kind)
