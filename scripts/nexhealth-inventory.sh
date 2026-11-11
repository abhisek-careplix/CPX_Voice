#!/usr/bin/env bash
# List every practice a NexHealth key can reach: its EHR, sync health, and which CarePlix
# automations that EHR supports (docs/solution/NEXHEALTH_EHR_MATRIX.md). Read-only.
#
#   ./scripts/nexhealth-inventory.sh        # or: make nexhealth-inventory
#
# Uses NEXHEALTH_PROD_API_KEY from .env (asks once and saves it if missing; never printed).
# Calls only practice-level endpoints (institutions, sync status, providers, appointment types):
# no patient or appointment data is read, so it is safe before the G4 PHI gate.
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

BASE = "https://nexhealth.info"
HEADERS = {
    "Nex-Api-Version": "v3.0.0",
    "User-Agent": "CarePlix-Voice/1.0",  # NexHealth's edge blocks the default urllib agent
}
ENV = pathlib.Path(".env")

# What each EHR takes through NexHealth, from NexHealth's "Supported health record systems" and
# "Testing writes, per system" pages (1 Nov 2026). B = backfill booking, R = release (cancel),
# C = confirmations, K = check-in, T = time/note change, D = documents, P = payments/adjustments.
EHRS = {
    "athenahealth": ("medical", "cloud", "BRC"),
    "eclinicalworks": ("medical", "cloud", "BRC"),
    "modernizing medicine": ("medical", "cloud", "BRCK"),
    "modmed": ("medical", "cloud", "BRCK"),
    "nextgen": ("medical", "cloud", "BRCK"),
    "cloud9": ("ortho", "cloud", "BRC"),
    "curve": ("dental", "cloud", "BRCD"),
    "denticon": ("dental", "cloud", "BRCKTD"),
    "dentrix ascend": ("dental", "cloud", "BRCD"),
    "dentrix enterprise": ("dental", "server", "BRCKTDP"),
    "dentrix": ("dental", "server", "BRCKTDP"),
    "dolphin": ("ortho", "server", "BRC"),
    "eaglesoft": ("dental", "server", "BRCKTDP"),
    "open dental": ("dental", "server", "BRCKTDP"),
    "opendental": ("dental", "server", "BRCKTDP"),
    "orthotrac": ("ortho", "server", "BC"),
    "practiceworks": ("dental", "server", "BC"),
}
FEATURES = {"B": "book", "R": "cancel", "C": "confirm", "K": "check-in", "T": "reschedule",
            "D": "documents", "P": "payments"}


def ask(prompt: str) -> str:
    with open("/dev/tty") as tty:
        sys.stdout.write(prompt)
        sys.stdout.flush()
        return tty.readline().strip()


def env_get(name: str) -> str:
    m = re.search(rf"^{name}=(.*)$", ENV.read_text(), re.M)
    return m.group(1).strip() if m else ""


def save(name: str, value: str) -> None:
    text, line = ENV.read_text(), f"{name}={value}"
    pattern = re.compile(rf"^{name}=.*$", re.M)
    ENV.write_text(pattern.sub(lambda _: line, text) if pattern.search(text) else f"{text.rstrip()}\n{line}\n")
    ENV.chmod(0o600)


def call(method: str, path: str, auth: str, params: dict | None = None) -> dict:
    url = f"{BASE}{path}" + (f"?{urllib.parse.urlencode(params, doseq=True)}" if params else "")
    req = urllib.request.Request(url, method=method, headers={**HEADERS, "Authorization": auth})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read() or b"{}")


def profile(name: str) -> tuple[str, str, str] | None:
    low = (name or "").lower()
    return next((v for k, v in EHRS.items() if k in low), None)


key = env_get("NEXHEALTH_PROD_API_KEY")
if not key:
    key = getpass.getpass("NexHealth production API key (hidden, paste and press Enter): ").strip().strip("'\"")
    key = re.sub(r"^Bearer\s+", "", key)
    if not key:
        sys.exit("No key entered.")
try:
    token = call("POST", "/authenticates", key)["data"]["token"]
except urllib.error.HTTPError as exc:
    sys.exit(f"✗ NexHealth rejected the key (HTTP {exc.code}). Nothing was saved.")
except (urllib.error.URLError, KeyError, ValueError) as exc:
    sys.exit(f"✗ Could not reach NexHealth ({type(exc).__name__}). Nothing was saved.")
save("NEXHEALTH_PROD_API_KEY", key)
bearer = f"Bearer {token}"
print("✓ Production key accepted (saved to .env as NEXHEALTH_PROD_API_KEY; the app keeps the sandbox key).\n")

institutions = call("GET", "/institutions", bearer).get("data") or []
rows = []
for inst in institutions:
    for loc in inst.get("locations") or []:
        scope = {"subdomain": inst.get("subdomain"), "location_id": loc.get("id")}
        try:
            syncs = call("GET", "/sync_status", bearer, {**scope, "location_ids[]": loc.get("id")}).get("data") or []
        except urllib.error.HTTPError as exc:
            syncs = [{"error": f"HTTP {exc.code}"}]
        counts = {}
        for label, path in (("providers", "/providers"), ("visit types", "/appointment_types")):
            try:
                body = call("GET", path, bearer, scope)
                counts[label] = body.get("count", len(body.get("data") or []))
            except urllib.error.HTTPError as exc:
                counts[label] = f"HTTP {exc.code}"
        emr = next(((s.get("emr") or {}).get("display_name") for s in syncs if s.get("emr")), None)
        emr = emr or ", ".join(inst.get("emrs") or []) or "no sync yet"
        rows.append((inst, loc, syncs, counts, emr))

if not rows:
    print("No practices yet. A production key reaches only institutions you create (or that approve")
    print("your access request) in the Developer Portal → Institutions. See NEXHEALTH_EHR_MATRIX.md §3.")
    sys.exit(0)

print(f"{len(rows)} location(s) across {len(institutions)} institution(s):\n")
for inst, loc, syncs, counts, emr in rows:
    state = "inactive" if loc.get("inactive") else "active"
    print(f"■ {inst.get('name')} · {loc.get('name')}  [{state}]")
    print(f"    subdomain {inst.get('subdomain')} · location {loc.get('id')} · EHR: {emr}")
    for s in syncs:
        if "error" in s:
            print(f"    sync status: {s['error']}")
            continue
        print(f"    sync: read {s.get('read_status') or '–'} · write {s.get('write_status') or '–'}"
              f" · last read {s.get('last_sync_time') or s.get('read_updated_at') or '–'}")
    if not syncs:
        print("    sync: none yet (practice install / cloud credentials pending)")
    print(f"    {counts['providers']} providers · {counts['visit types']} visit types")
    p = profile(emr)
    if p:
        kind, deploy, codes = p
        works = ", ".join(FEATURES[c] for c in codes)
        print(f"    {kind}, {deploy} · CarePlix can: read mirror, eligibility, {works}")
        if "R" not in codes:
            print("    ⚠ cancels don't reach this EHR: staff release a move-up's later visit by hand")
    else:
        print("    EHR not in NexHealth's published list: confirm support with NexHealth")
    print()

print("Next for each live location: Studio → Practice once the G4 PHI gate is passed (BAA in place).")
PY
