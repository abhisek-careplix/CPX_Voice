"""Connect CarePlix to the local OpenEMR and seed a synthetic clinic (synthetic data only).

    make openemr-up && make openemr-setup

1. Registers CarePlix as an OAuth2 API client and approves it (the same as Administration →
   System → API Clients → Enable in OpenEMR).
2. Seeds providers, insurers, patients with coverage, and two weeks of appointments, including
   cancellations and patients on an ASAP list ("call if an earlier slot opens").
3. Writes OPENEMR_* settings to .env (git-ignored, mode 600). The client secret is never printed.

Re-running keeps the existing client and only seeds an empty clinic.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import random
import re
import subprocess
import sys

import httpx

BASE = "https://localhost:9300"
API = f"{BASE}/apis/default/api"
# Local defaults; the hosted demo (infra/gcp/deploy-labs.sh openemr) sets these on its VM.
ADMIN_PASSWORD = os.environ.get("OPENEMR_ADMIN_PASSWORD", "pass")
DB_ROOT_PASSWORD = os.environ.get("OPENEMR_DB_ROOT_PASSWORD", "root")
UI_URL = os.environ.get("OPENEMR_PUBLIC_URL", "http://localhost:8300")
ENV = pathlib.Path(".env")
COMPOSE = ["docker", "compose", "-f", "infra/openemr/docker-compose.yml"] + [
    arg for extra in os.environ.get("OPENEMR_COMPOSE_EXTRA", "").split() for arg in ("-f", extra)
]
TZ = "America/New_York"
SCOPES = (
    "openid offline_access api:oemr api:fhir user/patient.read user/patient.write "
    "user/appointment.read user/appointment.write user/insurance.read user/insurance.write "
    "user/insurance_company.read user/insurance_company.write user/practitioner.read "
    "user/practitioner.write user/facility.read user/message.write user/list.read "
    "user/Patient.read user/Appointment.read user/Coverage.read user/Organization.read "
    "user/Practitioner.read"
)

PROVIDERS = [
    ("Avery", "Stone", "1902345671", "Family Medicine"),
    ("Jordan", "Reyes", "1902345689", "Internal Medicine"),
    ("Riley", "Chen", "1902345697", "Orthopedics"),
]
INSURERS = [("Aetna", "60054"), ("UnitedHealthcare", "87726"), ("Cigna", "62308")]
FIRST = [
    "Alex",
    "Blake",
    "Casey",
    "Dana",
    "Eli",
    "Frankie",
    "Gray",
    "Harper",
    "Indy",
    "Jules",
    "Kai",
    "Lane",
    "Morgan",
    "Noel",
    "Oakley",
    "Parker",
    "Quinn",
    "Reese",
    "Sage",
    "Tatum",
    "Uma",
    "Val",
    "Wren",
    "Xen",
]
LAST = ["Synth", "Testwell", "Mockley", "Sample", "Demo", "Fakeman", "Placeholder", "Trialson"]


def sql(statement: str) -> str:
    out = subprocess.run(
        [
            *COMPOSE,
            "exec",
            "-T",
            "mysql",
            "mariadb",
            "-uroot",
            f"-p{DB_ROOT_PASSWORD}",
            "-N",
            "openemr",
            "-e",
            statement,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def env_get(name: str) -> str:
    match = re.search(rf"^{name}=(.*)$", ENV.read_text(), re.M) if ENV.exists() else None
    return match.group(1).strip() if match else ""


def env_set(values: dict[str, str]) -> None:
    text = ENV.read_text() if ENV.exists() else ""
    for name, value in values.items():
        line = f"{name}={value}"
        pattern = re.compile(rf"^{name}=.*$", re.M)
        text = (
            pattern.sub(lambda _, line=line: line, text)
            if pattern.search(text)
            else f"{text.rstrip()}\n{line}\n"
        )
    ENV.write_text(text)
    ENV.chmod(0o600)


def client() -> tuple[str, str]:
    cid, secret = env_get("OPENEMR_CLIENT_ID"), env_get("OPENEMR_CLIENT_SECRET")
    if cid and secret:
        return cid, secret
    r = httpx.post(
        f"{BASE}/oauth2/default/registration",
        verify=False,
        timeout=30,
        json={
            "application_type": "private",
            "redirect_uris": [f"{BASE}/careplix/callback"],
            "client_name": "CarePlix Voice",
            "token_endpoint_auth_method": "client_secret_post",
            "contacts": ["dev@careplix.local"],
            "scope": SCOPES,
        },
    )
    r.raise_for_status()
    cid, secret = r.json()["client_id"], r.json()["client_secret"]
    sql(f"UPDATE oauth_clients SET is_enabled=1 WHERE client_id='{cid}'")
    env_set({"OPENEMR_CLIENT_ID": cid, "OPENEMR_CLIENT_SECRET": secret})
    print("✓ Registered and approved the CarePlix API client")
    return cid, secret


def brand() -> None:
    """White-label OpenEMR as "CarePlix EHR", keeping OpenEMR's attribution (GPL, trademark)."""
    for name, value in (
        ("openemr_name", "CarePlix EHR"),
        ("login_tagline_text", "Powered by OpenEMR. Front office automated by CarePlix Voice."),
        ("css_header", "style_cobalt_blue.css"),
        ("online_support_link", "https://careplix.com"),
    ):
        sql(f"UPDATE globals SET gl_value='{value}' WHERE gl_name='{name}'")
    site = "/var/www/localhost/htdocs/openemr/sites/default/images/logos/core"
    for slot in ("login/primary", "menu/primary"):
        subprocess.run(
            [*COMPOSE, "exec", "-T", "openemr", "mkdir", "-p", f"{site}/{slot}"], check=True
        )
        subprocess.run(
            [*COMPOSE, "cp", "infra/openemr/branding/logo.svg", f"openemr:{site}/{slot}/logo.svg"],
            check=True,
            capture_output=True,
        )
    # Decline OpenEMR's product registration and telemetry (no data leaves this machine).
    if sql("SELECT COUNT(*) FROM product_registration") == "0":
        sql(
            "INSERT INTO product_registration (email, opt_out, auth_by_id, telemetry_disabled, "
            "last_ask_date, "
            "last_ask_version) VALUES (NULL, 1, 1, 1, NOW(), '7.0.3')"
        )
    print("✓ Branded as CarePlix EHR (powered by OpenEMR)")


def module() -> None:
    """Register and enable the CarePlix Connector (as Administration → Modules → Install)."""
    directory = "oe-module-careplix"
    mounted = subprocess.run(
        [*COMPOSE, "exec", "-T", "openemr", "test", "-f",
         f"/var/www/localhost/htdocs/openemr/interface/modules/custom_modules/{directory}/openemr.bootstrap.php"],
    ).returncode == 0  # fmt: skip
    if not mounted:
        sys.exit("✗ The CarePlix module isn't mounted; run make openemr-up first")
    if sql(f"SELECT COUNT(*) FROM modules WHERE mod_directory='{directory}'") == "0":
        sql(
            "INSERT INTO modules SET mod_name='CarePlix Connector', mod_active=1, "
            f"mod_ui_name='CarePlix Connector', mod_directory='{directory}', type=0, sql_run=1, "
            "mod_description='Appointment status updates for CarePlix Voice', directory='', "
            "sql_version='', acl_version='', date=NOW()"
        )
        mod_id = sql(f"SELECT mod_id FROM modules WHERE mod_directory='{directory}'")
        section = sql("SELECT COALESCE(MAX(section_id), 0) + 1 FROM module_acl_sections")
        sql(
            "INSERT INTO module_acl_sections VALUES "
            f"({section}, 'CarePlix Connector', 0, '{directory}', {mod_id})"
        )
    sql(f"UPDATE modules SET mod_active=1 WHERE mod_directory='{directory}'")
    print("✓ Enabled the CarePlix Connector module (appointment status updates)")


def main() -> None:
    brand()
    module()
    cid, secret = client()
    env_set(
        {
            "OPENEMR_BASE_URL": BASE,
            "OPENEMR_USERNAME": "admin",
            "OPENEMR_PASSWORD": ADMIN_PASSWORD,
            "OPENEMR_TIMEZONE": TZ,
            "OPENEMR_VERIFY_TLS": "false",
            "OPENEMR_UI_URL": UI_URL,
        }
    )
    token = httpx.post(
        f"{BASE}/oauth2/default/token",
        verify=False,
        timeout=30,
        data={
            "grant_type": "password",
            "client_id": cid,
            "client_secret": secret,
            "user_role": "users",
            "username": "admin",
            "password": ADMIN_PASSWORD,
            "scope": SCOPES,
        },
    ).json()["access_token"]
    api = httpx.Client(
        base_url=API, verify=False, timeout=30, headers={"Authorization": f"Bearer {token}"}
    )

    facility = api.get("/facility").json()["data"][0]
    fid = facility["id"]
    if facility["name"] != "CarePlix Demo Clinic":
        sql(
            "UPDATE facility SET name='CarePlix Demo Clinic', facility_npi='1902345663' "
            f"WHERE id={fid}"
        )
    env_set({"OPENEMR_FACILITY_ID": str(fid)})

    random.seed(20261031)
    # GET /practitioner comes back empty on 7.0.3, so look providers up by NPI directly.
    existing = dict(
        row.split("\t")[::-1]
        for row in sql(
            "SELECT MIN(id), npi FROM users WHERE npi IS NOT NULL GROUP BY npi"
        ).splitlines()
    )
    providers = []
    for first, last, npi, specialty in PROVIDERS:
        if npi in existing:
            providers.append(existing[npi])
            continue
        r = api.post(
            "/practitioner",
            json={
                "fname": first,
                "lname": last,
                "npi": npi,
                "title": "MD",
                "specialty": specialty,
                "email": f"{first.lower()}@careplix.local",
            },
        )
        r.raise_for_status()
        providers.append(r.json()["data"]["id"])
    # Show them on the calendar and allow them to be scheduled (Administration → Users in the UI).
    # Practitioners are listed by the API only once they have a login username.
    sql(
        f"UPDATE users SET calendar=1, authorized=1, facility_id={fid}, "
        "facility='CarePlix Demo Clinic', "
        f"username=COALESCE(username, LOWER(CONCAT(fname, '.', lname))) "
        f"WHERE id IN ({','.join(map(str, providers))})"
    )
    print(f"✓ {len(providers)} providers")

    # OpenEMR 7.0.3's POST /insurance_company crashes (InsuranceCompanyService::validate() is
    # missing), so insurers are added the way an admin would on Practice → Insurance Companies.
    insurers = []
    for name, payer_id in INSURERS:
        found = sql(f"SELECT id FROM insurance_companies WHERE name='{name}'")
        if not found:
            next_id = int(sql("SELECT IFNULL(MAX(id), 0) + 1 FROM insurance_companies"))
            sql(
                "INSERT INTO insurance_companies "
                "(id, name, cms_id, ins_type_code, attn, x12_default_partner_id) "
                f"VALUES ({next_id}, '{name}', '{payer_id}', 1, '', 0)"
            )
            found = str(next_id)
        insurers.append(int(found))
    print(f"✓ {len(insurers)} insurers")

    patients = [(p["pid"], p["uuid"]) for p in api.get("/patient").json().get("data") or []]
    for i in range(0 if patients else 24):
        first, last = FIRST[i], LAST[i % len(LAST)]
        dob = dt.date(1950 + i * 2 % 50, 1 + i % 12, 1 + i % 27).isoformat()
        r = api.post(
            "/patient",
            json={
                "fname": first,
                "lname": last,
                "DOB": dob,
                "sex": "Female" if i % 2 else "Male",
                "title": "",
                "mname": "",
                "street": f"{100 + i} Example St",
                "city": "Springfield",
                "state": "IL",
                "postal_code": "62701",
                "country_code": "US",
                # 555-01xx is reserved for fiction; two patients have no phone (unreachable).
                "phone_cell": "" if i in (5, 17) else f"312-555-01{i:02d}",
            },
        )
        r.raise_for_status()
        data = r.json()["data"]
        patients.append((data["pid"], data["uuid"]))
    print(f"✓ {len(patients)} patients")

    covered = 0
    for i, (pid, puuid) in enumerate(sorted(patients)):
        if i in (9, 21) or api.get(f"/patient/{puuid}/insurance").json().get("data"):
            continue  # two self-pay patients keep no insurance on file
        p = api.get(f"/patient/{puuid}").json()["data"]
        r = api.post(
            f"/patient/{puuid}/insurance",
            json={
                "type": "primary",
                "provider": str(insurers[i % len(insurers)]),
                "plan_name": "PPO Gold",
                "policy_number": f"SYN{i:06d}",
                "group_number": "G100",
                "subscriber_lname": p["lname"],
                "subscriber_fname": p["fname"],
                "subscriber_relationship": "self",
                "subscriber_DOB": p["DOB"],
                "subscriber_street": p.get("street") or "1 Example St",
                "subscriber_postal_code": "62701",
                "subscriber_city": "Springfield",
                "subscriber_state": "IL",
                "subscriber_country": "USA",
                "subscriber_sex": p["sex"],
                "accept_assignment": "TRUE",
                "copay": "25",
                "date": "2026-01-01",
                "date_end": None,
            },
        )
        if r.status_code >= 300:
            print(f"  ! insurance for patient {pid}: {r.status_code} {r.text[:200]}")
            continue
        covered += 1
    print(f"✓ insurance added for {covered} patients")
    if sql("SELECT COUNT(*) FROM openemr_postcalendar_events WHERE pc_pid > 0") != "0":
        print("✓ Schedule already seeded; nothing to add")
        return

    cats = dict(
        row.split("\t")
        for row in sql(
            "SELECT pc_catid, pc_catname FROM openemr_postcalendar_categories WHERE pc_cattype=0"
        ).splitlines()
    )
    visit = next(k for k, v in cats.items() if v == "Office Visit")
    new_patient = next(k for k, v in cats.items() if v == "New Patient")
    booked = cancelled = asap = 0
    today = dt.date.today()
    days = [
        today + dt.timedelta(days=d)
        for d in range(1, 21)
        if (today + dt.timedelta(days=d)).weekday() < 5
    ][:10]
    for day in days:
        for p_index, provider in enumerate(providers):
            for slot in range(9 * 2, 16 * 2):  # 09:00–16:00, 30-minute slots
                if random.random() > 0.45:
                    continue
                pid, _ = random.choice(patients)
                start = f"{slot // 2:02d}:{30 * (slot % 2):02d}"
                status, note = "-", "Routine visit"
                roll = random.random()
                if roll < 0.10:
                    status, note = "x", "Patient called to cancel"
                    cancelled += 1
                elif roll < 0.17 and day > days[2]:
                    note = "ASAP list: call if an earlier slot opens"
                    asap += 1
                cat = new_patient if p_index == 2 and roll > 0.85 else visit
                r = api.post(
                    f"/patient/{pid}/appointment",
                    json={
                        "pc_catid": cat,
                        "pc_title": cats[cat],
                        "pc_duration": "1800",
                        "pc_hometext": note,
                        "pc_apptstatus": status,
                        "pc_eventDate": day.isoformat(),
                        "pc_startTime": start,
                        "pc_facility": str(fid),
                        "pc_billing_location": str(fid),
                        "pc_aid": str(provider),
                    },
                )
                if r.status_code >= 300:
                    print(f"  ! appointment: {r.status_code} {r.text[:160]}")
                    continue
                booked += 1
    print(
        f"✓ {booked} appointments over {len(days)} days "
        f"({cancelled} cancelled, {asap} on the ASAP list)"
    )
    print(f"Open {UI_URL} → Calendar to see the schedule.")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as exc:
        sys.exit(f"✗ OpenEMR request failed: {exc}")
