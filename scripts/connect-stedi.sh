#!/usr/bin/env bash
# Store the Stedi API key in .env (git-ignored, mode 600) and check it.
# The key is read without echo and is never printed or passed on a command line.
#
#   ./scripts/connect-stedi.sh          # or: make connect-stedi
#
# A test key is checked with Stedi's free mock eligibility request (Aetna mock payer, fixed test member).
# A production key is only checked against the payer directory: no payer is contacted.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || cp .env.example .env

python3 -I - <<'PY'
import getpass
import json
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://healthcare.us.stedi.com"
ENV = pathlib.Path(".env")

# Stedi's published mock request: only these exact subscriber values succeed in test mode.
MOCK_ELIGIBILITY = {
    "payerId": "60054",
    "provider": {"name": {"organization": "CarePlix Test Provider"}, "npi": "1999999984"},
    "subscriber": {
        "name": {"person": {"firstName": "Jane", "lastName": "Doe"}},
        "memberId": "AETNA12345",
        "dateOfBirth": "2004-04-04",
    },
    "encounter": {"services": [{"value": "30", "system": "STC"}]},
}


def ask(prompt: str) -> str:
    # stdin is this script, so prompts read the terminal directly.
    with open("/dev/tty") as tty:
        sys.stdout.write(prompt)
        sys.stdout.flush()
        return tty.readline().strip()


def save(values: dict[str, str]) -> None:
    text = ENV.read_text()
    for name, value in values.items():
        line = f"{name}={value}"
        pattern = re.compile(rf"^{name}=.*$", re.M)
        text = pattern.sub(lambda _: line, text) if pattern.search(text) else f"{text.rstrip()}\n{line}\n"
    ENV.write_text(text)
    ENV.chmod(0o600)


class StediError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def call(method: str, path: str, key: str, params: dict | None = None, body: dict | None = None) -> dict:
    url = f"{BASE}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    headers = {"Authorization": f"Key {key}", "User-Agent": "CarePlix-Voice/1.0", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read() or b"{}")
        except ValueError:
            detail = {}
        fields = "; ".join(str(f.get("message") or f) for f in detail.get("fieldList") or [])
        raise StediError(exc.code, " ".join(x for x in (detail.get("message"), fields) if x)) from None


key = getpass.getpass("Stedi API key (hidden, paste and press Enter): ").strip().strip("'\"")
key = re.sub(r"^Key\s+", "", key)  # tolerate a pasted "Key …" prefix
if not key:
    sys.exit("No key entered.")
MODES = {"": "test", "t": "test", "test": "test", "sandbox": "test", "y": "test",
         "p": "production", "prod": "production", "production": "production", "live": "production"}
while (mode := MODES.get(ask("Key type: press Enter for a test key, or type p for production: ").lower().strip("[] "))) is None:
    print("  Just press Enter for a test key, or type p for production.")
print(f"  Using {mode}.")
# Test and production keys live under separate names and never stand in for each other.
# STEDI_API_KEY is the older name for the test key; it's cleared so only one test key remains.
KEYS = (
    {"STEDI_TEST_API_KEY": key, "STEDI_API_KEY": "", "STEDI_ENV": "test"}
    if mode == "test"
    else {"STEDI_PROD_API_KEY": key, "STEDI_ENV": "production"}
)
if mode != "test":
    print("  Note: careplix-labs refuses a production Stedi key (./infra/gcp/deploy-labs.sh).")

# A test key is checked with Stedi's free mock eligibility request (no real payer or patient).
# A production key is checked against the payer directory only, so no payer is contacted.
print("Checking the key with Stedi…" + (" (mock eligibility: test payer, fixed test member, free)" if mode == "test" else ""))
try:
    if mode == "test":
        result = call("POST", "/2026-06-01/eligibility-check", key, body=MOCK_ELIGIBILITY)
    else:
        result = call("GET", "/2024-04-01/payers/search", key, {"query": "Aetna"})
except StediError as exc:
    if exc.code in (401, 403):
        sys.exit(f"✗ Key rejected (HTTP {exc.code}); copy it again from Stedi → API keys. Nothing was saved.")
    # Any other HTTP error means authentication passed and the check itself was refused.
    save(KEYS)
    sys.exit(f"✓ Key accepted and saved, but the check was refused (HTTP {exc.code}): {exc.message or 'no detail'}")
except (urllib.error.URLError, ValueError) as exc:
    sys.exit(f"✗ Could not reach Stedi ({type(exc).__name__}). Nothing was saved.")

save(KEYS)
print("✓ Key accepted and saved to .env (git-ignored, readable only by you).")

if mode != "test":
    print(f"  ✓ Payer directory reachable: {(result.get('stats') or {}).get('total')} matches for Aetna.")
    sys.exit(0)

errors = result.get("errors") or []
if errors:
    codes = ", ".join(str(e.get("code") or e.get("description")) for e in errors)
    sys.exit(f"  ✗ Mock eligibility returned payer errors: {codes}")
# The 2026-06-01 API groups benefits under plans[].benefits (statuses, coPayment, coInsurance, …).
groups: dict[str, int] = {}
statuses: set[str] = set()
for plan in result.get("plans") or []:
    for name, lines in (plan.get("benefits") or {}).items():
        groups[name] = groups.get(name, 0) + len(lines)
        if name == "statuses":
            statuses.update(str(s.get("status")) for s in lines if s.get("status"))
payer = (result.get("payer") or {}).get("organization") or result.get("payerId")
print(f"  ✓ {payer} answered: {', '.join(sorted(statuses)) or 'no status lines'}")
print("  ✓ Benefits returned: " + (", ".join(f"{n} {c}" for n, c in groups.items()) or "none"))
print("Next: tell Claude it's done; the Stedi connector is wired from these values.")
PY
