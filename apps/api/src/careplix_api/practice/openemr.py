"""OpenEMR adapter: a real, ONC-certified EHR connected directly (no hub) for end-to-end testing.

What OpenEMR 7.0.3's APIs offer, verified against the running container on 31 Oct 2026:
- OAuth2 password grant for the integration user; tokens last 1 h with a refresh token.
- Standard API: patients, patient insurance, practitioners, facility, appointments (create, read,
  delete), patient messages (create). There is no appointment update; the CarePlix Connector
  module (infra/openemr/modules/oe-module-careplix) adds ``PUT /patient/{pid}/appointment/{eid}``
  for status changes, so "cancel" sets ``x`` and the visit stays on the calendar. Without the
  module, cancel falls back to deleting the event. Staff cancellations in the OpenEMR calendar
  arrive as status ``x``/``%``.
- ``GET/POST /insurance_company`` is broken in 7.0.3, so insurers (name, payer ID) come from FHIR
  ``Organization`` (type ``ins``) and patient insurance links to them by ``insureruuid``.
- Appointment times are clinic-local; this adapter converts them with the practice time zone.
- OpenEMR has no "sooner if possible" flag: the front desk notes "ASAP" on the appointment.

Writes are made directly in the EHR, so a successful write is ``written`` immediately.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.db import as_utc, utcnow
from careplix_api.integrations.nexhealth import NexHealthError
from careplix_api.models import EhrRecord, PayerConnection
from careplix_api.phi import PhiBox
from careplix_api.practice import records
from careplix_api.practice.connections import audit, openemr_client
from careplix_api.practice.records import Mirrored
from careplix_api.settings import Settings

SCOPES = (
    "openid offline_access api:oemr api:fhir user/patient.read user/patient.write "
    "user/appointment.read user/appointment.write user/insurance.read user/insurance.write "
    "user/insurance_company.read user/insurance_company.write user/practitioner.read "
    "user/practitioner.write user/facility.read user/message.write user/list.read "
    "user/Patient.read user/Appointment.read user/Coverage.read user/Organization.read "
    "user/Practitioner.read"
)
CANCELLED, NO_SHOW, DONE = {"x", "%"}, {"?"}, {">", "$"}
CLINIC_HOURS = (8, 17)  # local; OpenEMR provider availability isn't exposed by the API
WINDOW_BACK, WINDOW_AHEAD = timedelta(days=1), timedelta(days=60)


class OpenEmrError(NexHealthError):
    """Same contract as NexHealthError (status, message, http_status) so callers handle both."""


class OpenEmrClient:
    is_openemr = True

    def __init__(
        self,
        base_url: str,
        *,
        client_id: str,
        client_secret: str,
        username: str,
        password: str,
        timezone: str,
        verify_tls: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.tz = ZoneInfo(timezone)
        self.username = username
        self._creds = (client_id, client_secret, username, password)
        self._token: tuple[str, float, str | None] | None = None
        self._connector: str | None = None
        self.calls = 0
        self._http = httpx.AsyncClient(timeout=30, verify=verify_tls, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _bearer(self, refresh: bool = False) -> str:
        if self._token and not refresh and time.monotonic() < self._token[1]:
            return self._token[0]
        client_id, client_secret, username, password = self._creds
        data: dict[str, str] = {
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": SCOPES,
        }
        if self._token and self._token[2]:
            data.update(grant_type="refresh_token", refresh_token=self._token[2])
        else:
            data.update(
                grant_type="password", user_role="users", username=username, password=password
            )
        self.calls += 1
        response = await self._http.post(f"{self.base_url}/oauth2/default/token", data=data)
        if response.status_code >= 400 and data["grant_type"] == "refresh_token":
            self._token = None
            return await self._bearer()
        if response.status_code >= 400:
            raise OpenEmrError("auth", "OpenEMR rejected the API credentials", response.status_code)
        body = response.json()
        self._token = (
            body["access_token"],
            time.monotonic() + body.get("expires_in", 3600) - 120,
            body.get("refresh_token"),
        )
        return self._token[0]

    async def request(
        self,
        method: str,
        path: str,
        *,
        fhir: bool = False,
        params: dict | None = None,
        json: Any = None,
    ) -> Any:
        url = f"{self.base_url}/apis/default/{'fhir' if fhir else 'api'}{path}"
        for attempt in range(2):
            self.calls += 1
            try:
                response = await self._http.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers={"Authorization": f"Bearer {await self._bearer(refresh=attempt > 0)}"},
                )
            except httpx.TimeoutException:
                raise OpenEmrError("timeout", f"{method} {path} timed out") from None
            except httpx.HTTPError as exc:
                raise OpenEmrError("network", type(exc).__name__) from None
            if response.status_code != 401:
                break
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {}
        if response.status_code >= 400:
            detail = (
                body.get("validationErrors")
                or body.get("error_description")
                or body.get("message")
                or body.get("error")
                if isinstance(body, dict)
                else None
            )
            status = "auth" if response.status_code in (401, 403) else "http"
            raise OpenEmrError(
                status, f"{method} {path}: {detail or response.reason_phrase}", response.status_code
            )
        return body

    @staticmethod
    def rows(body: Any) -> list[dict[str, Any]]:
        data = body.get("data", body) if isinstance(body, dict) else body
        if isinstance(data, dict):
            data = [data] if data else []
        return [r for r in data or [] if isinstance(r, dict)]

    async def fhir_all(self, resource: str, params: dict | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        body = await self.request(
            "GET", f"/{resource}", fhir=True, params={"_count": 200, **(params or {})}
        )
        while True:
            out += [e["resource"] for e in body.get("entry") or [] if "resource" in e]
            nxt = next(
                (link["url"] for link in body.get("link") or [] if link.get("relation") == "next"),
                None,
            )
            if not nxt or len(out) > 5000:
                return out
            self.calls += 1
            response = await self._http.get(
                nxt, headers={"Authorization": f"Bearer {await self._bearer()}"}
            )
            body = response.json()

    # --- time ------------------------------------------------------------------------------------

    def to_utc(self, day: str | None, clock: str | None) -> datetime | None:
        if not day or not clock:
            return None
        local = datetime.fromisoformat(f"{day}T{clock[:8] if len(clock) >= 8 else clock + ':00'}")
        return local.replace(tzinfo=self.tz).astimezone(UTC)

    def to_local(self, moment: datetime) -> tuple[str, str]:
        local = as_utc(moment).astimezone(self.tz)
        return local.date().isoformat(), local.strftime("%H:%M")

    # --- scheduling ----------------------------------------------------------------------------

    async def appointments(self) -> list[dict[str, Any]]:
        return self.rows(await self.request("GET", "/appointment"))

    async def appointment(self, eid: str) -> dict[str, Any] | None:
        try:
            rows = self.rows(await self.request("GET", f"/appointment/{eid}"))
        except OpenEmrError as err:
            if err.http_status == 404:
                return None
            raise
        return rows[0] if rows else None

    async def slot_free(self, opening: Any) -> str | None:
        """``""`` when the provider has nothing overlapping inside clinic hours, else ``None``."""
        start = as_utc(opening.starts_at)
        end = start + timedelta(minutes=opening.minutes)
        local = start.astimezone(self.tz)
        if not (
            CLINIC_HOURS[0] <= local.hour
            and (end.astimezone(self.tz).hour, end.astimezone(self.tz).minute)
            <= (CLINIC_HOURS[1], 0)
        ):
            return None
        for appt in await self.appointments():
            if (
                str(appt.get("pc_aid")) != str(opening.provider_ref)
                or appt.get("pc_apptstatus") in CANCELLED
            ):
                continue
            if str(appt.get("pc_eid")) == str(opening.source_appointment_ref):
                continue
            other_start = self.to_utc(appt.get("pc_eventDate"), appt.get("pc_startTime"))
            other_end = self.to_utc(appt.get("pc_eventDate"), appt.get("pc_endTime"))
            if other_start and other_end and other_start < end and start < other_end:
                return None
        return ""

    async def write(self, kind: str, intent: Any, payload: dict[str, Any]) -> dict[str, Any]:
        if kind == "patient_alert":
            body = await self.request(
                "POST",
                f"/patient/{intent.target_source_id}/message",
                json={
                    "title": "CarePlix",
                    "body": payload["note"],
                    "groupname": "Default",
                    "from": self.username,
                    "to": self.username,
                    "message_status": "New",
                },
            )
            mid = str(body.get("mid") or (body.get("data") or {}).get("mid") or "")
            return {"id": mid, "foreign_id": mid or None}
        if kind == "appointment_book":
            start = datetime.fromisoformat(payload["start_time"])
            end = datetime.fromisoformat(payload["end_time"])
            day, clock = self.to_local(start)
            category = str(payload.get("appointment_type_id") or "5")
            titles = {str(a.get("pc_catid")): a.get("pc_title") for a in await self.appointments()}
            body = await self.request(
                "POST",
                f"/patient/{payload['patient_id']}/appointment",
                json={
                    "pc_catid": category,
                    "pc_title": titles.get(category) or "Office Visit",
                    "pc_duration": str(int((end - start).total_seconds())),
                    "pc_hometext": payload.get("note") or "Booked by CarePlix",
                    "pc_apptstatus": "-",
                    "pc_eventDate": day,
                    "pc_startTime": clock,
                    "pc_facility": str(payload.get("location_id") or ""),
                    "pc_billing_location": str(payload.get("location_id") or ""),
                    "pc_aid": str(payload.get("provider_id") or ""),
                },
            )
            data = body.get("data", body) if isinstance(body, dict) else body
            # OpenEMR answers {"id": <pc_eid>} (sometimes wrapped in "data").
            eid = (data.get("id") or data.get("pc_eid")) if isinstance(data, dict) else data
            eid = str((eid[0] if isinstance(eid, list) else eid) or "")
            return {"id": eid, "foreign_id": eid or None}
        if kind == "appointment_cancel":
            path = f"/patient/{payload['patient_id']}/appointment/{intent.target_source_id}"
            if await self.connector():
                # The visit stays on the calendar as "x Canceled", with the reason in its note.
                await self.request(
                    "PUT",
                    path,
                    json={
                        "pc_apptstatus": "x",
                        "comment": f"Cancelled by CarePlix: {intent.reason}",
                    },
                )
            else:
                # Without the connector module, deleting is the only cancel the API offers.
                await self.request("DELETE", path)
            return {
                "id": intent.target_source_id,
                "foreign_id": intent.target_source_id,
                "cancelled": True,
            }
        raise OpenEmrError("http", f"OpenEMR adapter does not support {kind}")

    async def connector(self) -> str | None:
        """The CarePlix Connector module's version, or ``None`` when it isn't installed.

        Asks for a status change on appointment 0, which never exists: the module answers 404
        with its version header, while core OpenEMR has no such route (404, no header).
        """
        if self._connector is None:
            self.calls += 1
            try:
                response = await self._http.put(
                    f"{self.base_url}/apis/default/api/patient/0/appointment/0",
                    json={"pc_apptstatus": "-"},
                    headers={"Authorization": f"Bearer {await self._bearer()}"},
                )
            except httpx.HTTPError as exc:
                raise OpenEmrError("network", type(exc).__name__) from None
            self._connector = response.headers.get("x-careplix-connector") or ""
        return self._connector or None

    async def read_back(self, kind: str, intent: Any) -> dict[str, Any] | None:
        if kind == "appointment_book":
            appt = await self.appointment(intent.remote_id)
            return {"foreign_id": intent.remote_id} if appt else None
        if kind == "appointment_cancel":
            appt = await self.appointment(intent.target_source_id)
            done = appt is None or appt.get("pc_apptstatus") in CANCELLED
            return {"foreign_id": intent.target_source_id, "cancelled": True} if done else None
        return {"foreign_id": intent.remote_id}


# --- mapping -----------------------------------------------------------------------------------


def appointment_status(code: str | None) -> str:
    if code in CANCELLED:
        return "cancelled"
    if code in NO_SHOW:
        return "missed"
    if code in DONE:
        return "completed"
    return "booked"


def map_appointment(
    client: OpenEmrClient,
    a: dict[str, Any],
    note: str | None,
    providers: dict[str, str] | None = None,
) -> Mirrored:
    start = client.to_utc(a.get("pc_eventDate"), a.get("pc_startTime"))
    end = client.to_utc(a.get("pc_eventDate"), a.get("pc_endTime"))
    return Mirrored(
        str(a["pc_eid"]),
        {
            "provider_id": str(a.get("pc_aid") or "") or None,
            "provider_name": (providers or {}).get(str(a.get("pc_aid"))),
            "appointment_type_id": str(a.get("pc_catid") or "") or None,
            "operatory_id": None,
            "location_id": str(a.get("pc_facility") or "") or None,
            "end_time": end.isoformat() if end else None,
            "timezone": str(client.tz),
            # The ASAP marker is the only part of the note we keep (notes are never mirrored).
            "sooner_if_possible": bool(note and "asap" in note.lower()),
            "foreign_id": str(a["pc_eid"]),
            "status_code": a.get("pc_apptstatus"),
        },
        patient_ref=str(a.get("pc_pid") or "") or None,
        starts_at=start,
        status=appointment_status(a.get("pc_apptstatus")),
    )


async def _fetch(client: OpenEmrClient) -> dict[str, list[Mirrored]]:
    out: dict[str, list[Mirrored]] = {}
    facilities = client.rows(await client.request("GET", "/facility"))
    out["location"] = [
        Mirrored(
            str(f["id"]),
            {
                "name": f.get("name"),
                "inactive": False,
                "institution_name": "OpenEMR",
                "emrs": ["openemr"],
                "npi": f.get("facility_npi"),
            },
            status="active",
        )
        for f in facilities
    ]
    out["provider"] = [
        Mirrored(
            str(p["id"]),
            {
                "name": f"Dr. {p.get('fname', '')} {p.get('lname', '')}".strip(),
                "npi": p.get("npi"),
                "specialty_code": p.get("specialty"),
                "inactive": False,
            },
            status="active",
        )
        for p in client.rows(await client.request("GET", "/practitioner"))
    ]
    out["insurance_plan"] = [
        Mirrored(
            o["id"],
            {
                "name": o.get("name"),
                "payer_id": next((i.get("value") for i in o.get("identifier") or []), None),
            },
            status="active",
        )
        for o in await client.fhir_all("Organization")
        if any(c.get("code") == "ins" for t in o.get("type") or [] for c in t.get("coding") or [])
    ]
    patients = client.rows(await client.request("GET", "/patient"))
    out["patient"] = [
        Mirrored(
            str(p["pid"]),
            {
                "uuid": p.get("uuid"),
                "foreign_id": str(p["pid"]),
                "has_phone": bool(p.get("phone_cell") or p.get("phone_home")),
                "inactive": False,
            },
            pii={
                "first_name": p.get("fname"),
                "last_name": p.get("lname"),
                "date_of_birth": p.get("DOB"),
                "gender": p.get("sex"),
                "phone": p.get("phone_cell") or p.get("phone_home"),
                "email": p.get("email"),
            },
            patient_ref=str(p["pid"]),
            status="active",
        )
        for p in patients
    ]
    today = date.today().isoformat()
    coverages: list[Mirrored] = []
    priority = {"primary": 0, "secondary": 1, "tertiary": 2}
    gate = asyncio.Semaphore(8)

    async def insurance(p: dict[str, Any]) -> list[dict[str, Any]]:
        async with gate:
            return client.rows(await client.request("GET", f"/patient/{p['uuid']}/insurance"))

    for p, rows in zip(
        patients, await asyncio.gather(*(insurance(p) for p in patients)), strict=True
    ):
        for c in rows:
            if not c.get("provider") or str(c.get("provider")) == "0":
                continue
            ended = c.get("date_end") and c["date_end"] < today
            coverages.append(
                Mirrored(
                    str(c.get("uuid") or c["id"]),
                    {
                        "plan_id": c.get("insureruuid"),
                        "priority": priority.get(c.get("type"), 9),
                        "insurance_type": "medical",
                        "effective_date": c.get("date"),
                        "expiration_date": c.get("date_end"),
                        "subscription_relation": c.get("subscriber_relationship"),
                    },
                    pii={"member_id": c.get("policy_number")},
                    patient_ref=str(p["pid"]),
                    status="inactive" if ended else "active",
                )
            )
    out["coverage"] = coverages

    now = utcnow()
    appts = [
        a
        for a in await client.appointments()
        if (s := client.to_utc(a.get("pc_eventDate"), a.get("pc_startTime")))
        and now - WINDOW_BACK <= s <= now + WINDOW_AHEAD
    ]
    types: dict[str, Mirrored] = {}
    for a in appts:
        start, end = (
            client.to_utc(a["pc_eventDate"], a["pc_startTime"]),
            client.to_utc(a["pc_eventDate"], a.get("pc_endTime")),
        )
        minutes = int((end - start).total_seconds() // 60) if start and end else 30
        types.setdefault(
            str(a.get("pc_catid")),
            Mirrored(str(a.get("pc_catid")), {"name": a.get("pc_title"), "minutes": minutes}),
        )
    out["appointment_type"] = list(types.values())

    async def note(a: dict[str, Any]) -> str | None:
        # Only upcoming scheduled visits can be "ASAP"; the list omits notes, so read those few.
        start = client.to_utc(a["pc_eventDate"], a["pc_startTime"])
        if a.get("pc_apptstatus") in CANCELLED or not start or start < now:
            return None
        async with gate:
            detail = await client.appointment(str(a["pc_eid"]))
        return (detail or {}).get("pc_hometext")

    notes = await asyncio.gather(*(note(a) for a in appts))
    names = {p.source_id: p.attrs["name"] for p in out["provider"]}
    out["appointment"] = [
        map_appointment(client, a, n, names) for a, n in zip(appts, notes, strict=True)
    ]
    return out


ORDER = (
    "location",
    "provider",
    "appointment_type",
    "insurance_plan",
    "patient",
    "coverage",
    "appointment",
)


async def sync_practice(
    session: AsyncSession,
    conn: PayerConnection,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    full: bool = False,
) -> dict[str, Any]:
    """OpenEMR has no ``updated_since``, so every pass is a full, cheap local read."""
    from careplix_api.practice.sync import _cursor

    box, client = PhiBox(settings), openemr_client(conn, settings, transport)
    report: dict[str, Any] = {"full": True, "resources": {}, "transitions": []}
    started = time.perf_counter()
    try:
        fetched = await _fetch(client)
    except OpenEmrError as err:
        await audit(
            session,
            conn,
            "openemr.sync",
            "sync",
            ok=False,
            status=err.status,
            http_status=err.http_status,
            error=err.message,
        )
        report["error"] = err.message
        return report
    finally:
        calls = client.calls
        await client.aclose()
    ms = round((time.perf_counter() - started) * 1000)
    for resource in ORDER:
        items = fetched.get(resource, [])
        transitions = await records.upsert(session, conn, resource, items, box)
        if resource == "appointment":
            transitions += await _mark_missing(session, conn, {i.source_id for i in items})
            report["transitions"] = transitions
        cursor = await _cursor(session, conn, resource)
        cursor.rows, cursor.duration_ms, cursor.last_error = len(items), ms, None
        cursor.last_run_at = cursor.last_full_at = utcnow()
        report["resources"][resource] = {"rows": len(items)}
    (await _cursor(session, conn, "appointment")).api_calls += calls
    await audit(
        session,
        conn,
        "openemr.sync",
        "sync",
        ok=True,
        status="success",
        duration_ms=ms,
        summary={"rows": sum(len(v) for v in fetched.values()), "api_calls": calls},
    )
    report["api_calls"] = calls
    conn.capability = {
        **(conn.capability or {}),
        "last_sync": {"at": utcnow().isoformat(), "full": True, "api_calls": calls},
    }
    return report


async def _mark_missing(
    session: AsyncSession, conn: PayerConnection, seen: set[str]
) -> list[dict[str, Any]]:
    """Appointments deleted in OpenEMR simply disappear from the API: record them as deleted."""
    now = utcnow()
    changed = []
    for rec in await session.scalars(
        select(EhrRecord).where(
            EhrRecord.connection_id == conn.id,
            EhrRecord.resource == "appointment",
            EhrRecord.status.in_(("booked", "confirmed")),
            EhrRecord.starts_at >= now - WINDOW_BACK,
        )
    ):
        if rec.source_id not in seen:
            changed.append({"id": rec.source_id, "from": rec.status, "to": "deleted"})
            rec.status = "deleted"
    return changed


async def probe(
    session: AsyncSession,
    conn: PayerConnection,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    client = openemr_client(conn, settings, transport)
    reads: dict[str, Any] = {}
    location = None
    started = time.perf_counter()
    try:
        for name, call in (
            ("facility", lambda: client.request("GET", "/facility")),
            ("practitioners", lambda: client.request("GET", "/practitioner")),
            ("patients", lambda: client.request("GET", "/patient")),
            ("appointments", lambda: client.request("GET", "/appointment")),
            ("appointment_statuses", lambda: client.request("GET", "/list/apptstat")),
            (
                "insurers (FHIR Organization)",
                lambda: client.request("GET", "/Organization", fhir=True),
            ),
            (
                "coverage (FHIR Coverage)",
                lambda: client.request("GET", "/Coverage", fhir=True, params={"_count": 1}),
            ),
            (
                "insurance_company (standard API)",
                lambda: client.request("GET", "/insurance_company"),
            ),
        ):
            try:
                body = await call()
                reads[name] = {
                    "ok": True,
                    "has_data": bool(
                        client.rows(body) or (isinstance(body, dict) and body.get("entry"))
                    ),
                }
                if name == "facility":
                    facility = next(iter(client.rows(body)), {})
                    location = (
                        {"id": str(facility.get("id")), "name": facility.get("name")}
                        if facility
                        else None
                    )
            except OpenEmrError as err:
                reads[name] = {
                    "ok": False,
                    "http_status": err.http_status,
                    "error": err.message[:120],
                }
        try:
            connector = await client.connector()
        except OpenEmrError:
            connector = None
    finally:
        calls = client.calls
        await client.aclose()
    capability = {
        **(conn.capability or {}),
        "probed_at": utcnow().isoformat(),
        "location": location,
        "emrs": ["OpenEMR 7"],
        "reads": reads,
        # A direct connection: the API writes straight into the EHR.
        "sync": [
            {
                "emr": "OpenEMR",
                "type": "direct",
                "read_status": "green" if location else "red",
                "write_status": "green" if location else "red",
            }
        ],
        "writes_confirmable": True,
        "connector_module": connector,
        "writes": {
            "appointments": "POST /patient/{pid}/appointment (written immediately)",
            "appointment_cancel": (
                f"PUT /patient/{{pid}}/appointment/{{eid}} → x (CarePlix Connector {connector})"
                if connector
                else "DELETE /patient/{pid}/appointment/{eid} (install the CarePlix Connector "
                "module to cancel by status instead)"
            ),
            "patient_messages": "POST /patient/{pid}/message (chart message)",
            "patients": "POST /patient",
            "insurance": "POST /patient/{puuid}/insurance",
        },
        "probe_api_calls": calls,
    }
    conn.capability = capability
    await audit(
        session,
        conn,
        "openemr.probe",
        "verify",
        ok=location is not None,
        status="success" if location else "error",
        duration_ms=round((time.perf_counter() - started) * 1000),
        summary={"reads_ok": sum(1 for r in reads.values() if r.get("ok")), "reads": len(reads)},
    )
    return capability


def deep_links(base_url: str) -> dict[str, str]:
    """Where staff see the same records inside OpenEMR."""
    root = base_url.rstrip("/")
    return {
        "calendar": f"{root}/interface/main/calendar/index.php"
        "?module=PostCalendar&viewtype=day&Date={date}",
        "patient": f"{root}/interface/patient_file/summary/demographics.php?set_pid={{pid}}",
        "home": f"{root}/interface/main/tabs/main.php",
    }
