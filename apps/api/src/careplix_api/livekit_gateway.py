"""The API's only path to LiveKit (ADR08: the backend owns rooms, dispatch and dialing).

Room names and participant identities are deterministic from the attempt ID, so a timed-out
request can be reconciled by looking the participant up instead of dialing again (TR04).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta

from livekit import api as lkapi

from careplix_api.errors import ApiError
from careplix_api.settings import Settings

BROWSER_TOKEN_TTL = timedelta(minutes=15)


def room_name_for(attempt_id: str) -> str:
    return f"att_{attempt_id.replace('-', '')}"


def sip_identity_for(attempt_id: str) -> str:
    return f"pstn_{attempt_id.replace('-', '')}"


@dataclass
class DialOutcome:
    connected: bool
    disposition: str  # connected|busy|no_answer|rejected|failed
    sip_status_code: int | None = None
    participant_id: str | None = None
    sip_call_id: str | None = None


class LiveKitGateway:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def configured(self) -> bool:
        return self.settings.livekit_configured

    def _client(self) -> lkapi.LiveKitAPI:
        if not self.configured:
            raise ApiError(503, "voice_not_configured", "Voice connection is not configured.")
        return lkapi.LiveKitAPI(
            self.settings.livekit_url,
            self.settings.livekit_api_key,
            self.settings.livekit_api_secret.get_secret_value(),
        )

    async def create_room_and_dispatch(self, room: str, attempt_id: str) -> str:
        async with self._client() as lk:
            await lk.room.create_room(
                lkapi.CreateRoomRequest(
                    name=room,
                    empty_timeout=60,
                    departure_timeout=10,
                    max_participants=4,
                )
            )
            dispatch = await lk.agent_dispatch.create_dispatch(
                lkapi.CreateAgentDispatchRequest(
                    agent_name=self.settings.livekit_agent_name,
                    room=room,
                    # Opaque reference only (I4); the worker fetches everything else after claim.
                    metadata=json.dumps({"attempt_id": attempt_id}),
                )
            )
            return dispatch.id

    async def dispatch_counterpart(self, room: str, attempt_id: str) -> str:
        """VS2: dispatch the simulated other side into an existing room (same worker image)."""
        async with self._client() as lk:
            dispatch = await lk.agent_dispatch.create_dispatch(
                lkapi.CreateAgentDispatchRequest(
                    agent_name=self.settings.livekit_agent_name,
                    room=room,
                    metadata=json.dumps({"counterpart_for": attempt_id}),
                )
            )
            return dispatch.id

    def browser_token(self, room: str, identity: str, display_name: str) -> str:
        return (
            lkapi.AccessToken(
                self.settings.livekit_api_key, self.settings.livekit_api_secret.get_secret_value()
            )
            .with_identity(identity)
            .with_name(display_name)
            .with_ttl(BROWSER_TOKEN_TTL)
            .with_grants(
                lkapi.VideoGrants(
                    room_join=True,
                    room=room,
                    can_publish=True,
                    can_publish_sources=["microphone"],
                    can_subscribe=True,
                    can_publish_data=False,
                )
            )
            .to_jwt()
        )

    async def dial(self, room: str, attempt_id: str, destination: str, trunk: str) -> DialOutcome:
        if not trunk:
            raise ApiError(503, "outbound_not_configured", "Phone connection is not configured.")
        async with self._client() as lk:
            try:
                info = await lk.sip.create_sip_participant(
                    lkapi.CreateSIPParticipantRequest(
                        sip_trunk_id=trunk,
                        sip_call_to=destination,
                        room_name=room,
                        participant_identity=sip_identity_for(attempt_id),
                        participant_name="Phone",
                        wait_until_answered=True,
                        play_dialtone=False,
                    )
                )
            except lkapi.SipCallError as err:
                code = err.sip_status_code
                disposition = {486: "busy", 600: "busy", 480: "no_answer", 408: "no_answer"}.get(
                    code or 0, "rejected" if code and 400 <= code < 700 else "failed"
                )
                return DialOutcome(False, disposition, code)
            return DialOutcome(
                True, "connected", participant_id=info.participant_id, sip_call_id=info.sip_call_id
            )

    async def sip_participant_present(self, room: str, attempt_id: str) -> bool:
        async with self._client() as lk:
            participants = await lk.room.list_participants(lkapi.ListParticipantsRequest(room=room))
        return any(p.identity == sip_identity_for(attempt_id) for p in participants.participants)

    async def room_exists(self, room: str) -> bool:
        async with self._client() as lk:
            rooms = await lk.room.list_rooms(lkapi.ListRoomsRequest(names=[room]))
        return bool(rooms.rooms)

    async def delete_room(self, room: str) -> None:
        async with self._client() as lk:
            try:
                await lk.room.delete_room(lkapi.DeleteRoomRequest(room=room))
            except lkapi.ServerError as err:
                if "not_found" not in str(err.code).lower():
                    raise

    # --- Project setup (Connections) ------------------------------------------------------------

    async def inventory(self) -> dict:
        """Trunks and dispatch rules in the project. Credentials are never returned."""
        async with self._client() as lk:
            rooms = await lk.room.list_rooms(lkapi.ListRoomsRequest())
            outbound = await lk.sip.list_outbound_trunk(lkapi.ListSIPOutboundTrunkRequest())
            inbound = await lk.sip.list_inbound_trunk(lkapi.ListSIPInboundTrunkRequest())
            rules = await lk.sip.list_dispatch_rule(lkapi.ListSIPDispatchRuleRequest())
        return {
            "active_rooms": len(rooms.rooms),
            "outbound_trunks": [
                {
                    "id": t.sip_trunk_id,
                    "name": t.name,
                    "address": t.address,
                    "numbers": list(t.numbers),
                    "has_auth": bool(t.auth_username),
                }
                for t in outbound.items
            ],
            "inbound_trunks": [
                {"id": t.sip_trunk_id, "name": t.name, "numbers": list(t.numbers)}
                for t in inbound.items
            ],
            "dispatch_rules": [
                {
                    "id": r.sip_dispatch_rule_id,
                    "name": r.name,
                    "numbers": list(r.numbers),
                    "trunk_ids": list(r.trunk_ids),
                    "agents": [a.agent_name for a in r.room_config.agents],
                }
                for r in rules.items
            ],
        }

    async def create_inbound_rule(self, name: str, number: str, route_id: str) -> dict:
        """Send inbound calls into a fresh room with an explicit CarePlix agent dispatch.

        Asks LiveKit to scope the rule to ``number``. LiveKit may store it project-wide instead
        (observed for purchased numbers: ``numbers`` comes back empty), so the stored scope is
        returned rather than assumed. Either way the worker claims by the called number and the
        API answers only numbers that have a CarePlix route. Metadata carries only the opaque
        route ID (I4).
        """
        async with self._client() as lk:
            rule = await lk.sip.create_dispatch_rule(
                lkapi.CreateSIPDispatchRuleRequest(
                    dispatch_rule=lkapi.SIPDispatchRuleInfo(
                        name=name,
                        numbers=[number],
                        # Callee rule: rooms are named after our number, never the caller's (I4).
                        rule=lkapi.SIPDispatchRule(
                            dispatch_rule_callee=lkapi.SIPDispatchRuleCallee(
                                room_prefix="inb", randomize=True
                            )
                        ),
                        room_config=lkapi.RoomConfiguration(
                            agents=[
                                lkapi.RoomAgentDispatch(
                                    agent_name=self.settings.livekit_agent_name,
                                    metadata=json.dumps({"route_id": route_id}),
                                )
                            ]
                        ),
                        krisp_enabled=True,
                    )
                )
            )
        return {"id": rule.sip_dispatch_rule_id, "numbers": list(rule.numbers)}

    async def delete_dispatch_rule(self, rule_id: str) -> None:
        async with self._client() as lk:
            await lk.sip.delete_dispatch_rule(
                lkapi.DeleteSIPDispatchRuleRequest(sip_dispatch_rule_id=rule_id)
            )

    async def create_outbound_trunk(
        self, name: str, address: str, number: str, username: str, password: str, transport: str
    ) -> str:
        """Create a carrier trunk. The SIP password goes to LiveKit; CarePlix never stores it."""
        transports = {
            "auto": lkapi.SIP_TRANSPORT_AUTO,
            "udp": lkapi.SIP_TRANSPORT_UDP,
            "tcp": lkapi.SIP_TRANSPORT_TCP,
            "tls": lkapi.SIP_TRANSPORT_TLS,
        }
        async with self._client() as lk:
            trunk = await lk.sip.create_outbound_trunk(
                lkapi.CreateSIPOutboundTrunkRequest(
                    trunk=lkapi.SIPOutboundTrunkInfo(
                        name=name,
                        address=address,
                        numbers=[number],
                        auth_username=username,
                        auth_password=password,
                        transport=transports[transport],
                    )
                )
            )
        return trunk.sip_trunk_id

    async def dispatch_probe(self, room: str, probe_id: str) -> None:
        async with self._client() as lk:
            await lk.room.create_room(
                lkapi.CreateRoomRequest(name=room, empty_timeout=30, max_participants=1)
            )
            await lk.agent_dispatch.create_dispatch(
                lkapi.CreateAgentDispatchRequest(
                    agent_name=self.settings.livekit_agent_name,
                    room=room,
                    metadata=json.dumps({"probe_id": probe_id}),
                )
            )
