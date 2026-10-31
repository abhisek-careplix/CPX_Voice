"""G0 two-way audio check: create a room, explicitly dispatch the agent, mint a browser token.

    make browser-session

Join with the printed URL + token in any LiveKit client (for example the LiveKit Agents
Playground's custom connection). Synthetic conversation only. This is the precursor of
POST /browser-sessions (G1), which adds user authorization and a sandbox attempt record.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import timedelta

from livekit import api as lkapi

from careplix_api.settings import get_settings

TOKEN_TTL = timedelta(minutes=10)


async def main() -> int:
    settings = get_settings()
    if not settings.livekit_configured:
        print("blocked: set LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET in .env")
        return 1

    # Opaque identifiers only: no names, numbers or case data in room names or metadata (I4).
    room_name = f"g0-{secrets.token_hex(6)}"
    identity = f"g0-tester-{secrets.token_hex(4)}"

    async with lkapi.LiveKitAPI(
        settings.livekit_url,
        settings.livekit_api_key,
        settings.livekit_api_secret.get_secret_value(),
    ) as lk:
        await lk.room.create_room(
            lkapi.CreateRoomRequest(name=room_name, empty_timeout=300, max_participants=3)
        )
        dispatch = await lk.agent_dispatch.create_dispatch(
            lkapi.CreateAgentDispatchRequest(
                agent_name=settings.livekit_agent_name,
                room=room_name,
                metadata=json.dumps({"probe": "g0-browser"}),
            )
        )

    token = (
        lkapi.AccessToken(settings.livekit_api_key, settings.livekit_api_secret.get_secret_value())
        .with_identity(identity)
        .with_ttl(TOKEN_TTL)
        .with_grants(
            lkapi.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_publish_sources=["microphone"],
                can_subscribe=True,
            )
        )
        .to_jwt()
    )

    print(f"room:        {room_name}")
    print(f"dispatch_id: {dispatch.id}")
    print(f"url:         {settings.livekit_url}")
    print(f"token:       {token}")
    minutes = int(TOKEN_TTL.total_seconds() // 60)
    print(f"\nToken expires in {minutes} minutes and only joins this room.")
    print("Record the room and dispatch IDs in the G0-BROWSER evidence record after the test.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
