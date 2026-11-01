#!/usr/bin/env bash
# Store the NexHealth API key in .env (git-ignored, mode 600), check it, and record the practice.
# The key is read without echo and is never printed or passed on a command line.
#
#   ./scripts/connect-nexhealth.sh          # or: make connect-nexhealth
#
# Re-run any time to replace the key or pick a different location.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || cp .env.example .env

python3 -I - <<'PY'
import datetime
import getpass
import json
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://nexhealth.info"
# NexHealth's edge blocks the default Python-urllib user agent (Cloudflare 1010) before auth.
HEADERS = {
    "Nex-Api-Version": "v3.0.0",
    "Accept": "application/vnd.Nexhealth+json;version=2",
    "User-Agent": "CarePlix-Voice/1.0",
}
ENV = pathlib.Path(".env")


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


def call(method: str, path: str, auth: str, params: dict | None = None) -> dict:
    url = f"{BASE}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    request = urllib.request.Request(url, method=method, headers={**HEADERS, "Authorization": auth})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read() or b"{}")


key = getpass.getpass("NexHealth API key (hidden, paste and press Enter): ").strip().strip("'\"")
key = re.sub(r"^Bearer\s+", "", key)  # tolerate a pasted "Bearer …" prefix
if not key:
    sys.exit("No key entered.")
MODES = {"": "sandbox", "s": "sandbox", "sandbox": "sandbox", "test": "sandbox", "y": "sandbox",
         "p": "production", "prod": "production", "production": "production", "live": "production"}
while (mode := MODES.get(ask("Key type: press Enter for sandbox, or type p for production: ").lower().strip("[] "))) is None:
    print("  Just press Enter for a sandbox key, or type p for production.")
print(f"  Using {mode}.")

print("Checking the key with NexHealth…")
try:
    token = call("POST", "/authenticates", key)["data"]["token"]
except urllib.error.HTTPError as exc:
    reason = {
        401: "key rejected; copy it again from the Developer Portal",
        403: "request blocked by NexHealth's firewall",
    }.get(exc.code, f"HTTP {exc.code}")
    sys.exit(f"✗ Could not authenticate ({reason}). Nothing was saved.")
except (urllib.error.URLError, KeyError, ValueError) as exc:
    sys.exit(f"✗ Could not reach NexHealth ({type(exc).__name__}). Nothing was saved.")
bearer = f"Bearer {token}"

save({"NEXHEALTH_API_KEY": key, "NEXHEALTH_ENV": mode})
print("✓ Key accepted and saved to .env (git-ignored, readable only by you).")

institutions = call("GET", "/institutions", bearer).get("data") or []
choices = [
    (inst, loc)
    for inst in institutions
    for loc in inst.get("locations") or []
    if not loc.get("inactive")
]
if not choices:
    sys.exit("✗ The key works but has no active locations yet. Add a practice in the Developer Portal, then re-run.")

print("\nLocations this key can access:")
for i, (inst, loc) in enumerate(choices, 1):
    emrs = ", ".join(inst.get("emrs") or []) or "unknown EHR"
    print(f"  {i}. {inst.get('name')} · {loc.get('name')}  (subdomain {inst.get('subdomain')}, location {loc.get('id')}, {emrs})")
pick = ask(f"Use which location? [1-{len(choices)}, default 1]: ") or "1"
if not pick.isdigit() or not 1 <= int(pick) <= len(choices):
    sys.exit("Invalid choice. Re-run the script.")
inst, loc = choices[int(pick) - 1]
subdomain, location_id = inst["subdomain"], str(loc["id"])
save({
    "NEXHEALTH_SUBDOMAIN": subdomain,
    "NEXHEALTH_INSTITUTION_ID": str(inst["id"]),
    "NEXHEALTH_LOCATION_ID": location_id,
})
print(f"✓ Saved subdomain {subdomain}, location {location_id}.")

# Smoke test: counts only, never patient details.
scope = {"subdomain": subdomain, "location_id": location_id}
now = datetime.datetime.now(datetime.timezone.utc)
window = {"start": now.isoformat(), "end": (now + datetime.timedelta(days=30)).isoformat()}
print("\nReading the practice (counts only):")
for label, path, extra in (
    ("Providers", "/providers", {}),
    ("Appointment types", "/appointment_types", {}),
    ("Patients", "/patients", {"per_page": 1}),
    ("Appointments (next 30 days)", "/appointments", {"per_page": 1, **window}),
    ("Sync status entries", "/sync_status", {}),
):
    try:
        body = call("GET", path, bearer, {**scope, **extra})
        count = body.get("count")
        if count is None:
            data = body.get("data")
            count = len(data) if isinstance(data, list) else "ok"
        print(f"  ✓ {label}: {count}")
    except urllib.error.HTTPError as exc:
        print(f"  ✗ {label}: HTTP {exc.code}")

if mode == "sandbox":
    print("\nSandbox note: the demo location has no sync, so writes are accepted but never confirmed,"
          " and sync status is empty. That is expected.")
print("Next: tell Claude it's done; the NexHealth connector is wired from these values.")
PY
