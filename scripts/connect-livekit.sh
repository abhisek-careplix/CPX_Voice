#!/usr/bin/env bash
# Store LiveKit credentials in .env (git-ignored, mode 600) and check the connection.
# The API secret is read without echo and is never printed or passed on a command line.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || cp .env.example .env

read -rp "LiveKit URL (wss://<project>.livekit.cloud): " url
read -rp "LiveKit API key: " key
read -rsp "LiveKit API secret (hidden): " secret
echo

if [[ "$url" != wss://* ]]; then
  echo "The URL must start with wss:// (LiveKit Cloud → Settings → Project URL)." >&2
  exit 1
fi
if [[ -z "$key" || -z "$secret" ]]; then
  echo "Key and secret are both required." >&2
  exit 1
fi

LK_URL="$url" LK_KEY="$key" LK_SECRET="$secret" python3 -I - <<'PY'
import os
import pathlib
import re

path = pathlib.Path(".env")
text = path.read_text()
for name, var in (("LIVEKIT_URL", "LK_URL"), ("LIVEKIT_API_KEY", "LK_KEY"), ("LIVEKIT_API_SECRET", "LK_SECRET")):
    line = f"{name}={os.environ[var].strip()}"
    pattern = re.compile(rf"^{name}=.*$", re.M)
    text = pattern.sub(lambda _: line, text) if pattern.search(text) else f"{text.rstrip()}\n{line}\n"
path.write_text(text)
PY
chmod 600 .env
echo "Saved to .env (git-ignored, readable only by you)."

echo "Checking LiveKit…"
uv run python - <<'PY'
import asyncio

from careplix_api.livekit_gateway import LiveKitGateway
from careplix_api.settings import Settings


async def main() -> None:
    try:
        inv = await LiveKitGateway(Settings()).inventory()
    except Exception as exc:  # noqa: BLE001
        print(f"✗ Could not connect ({type(exc).__name__}). Check the URL, key and secret.")
        raise SystemExit(1) from None
    print(
        f"✓ Connected. {inv['active_rooms']} active rooms, {len(inv['outbound_trunks'])} carrier "
        f"trunks, {len(inv['dispatch_rules'])} dispatch rules."
    )


asyncio.run(main())
PY
echo "Next: restart the API (make api) and open Studio → Connections."
