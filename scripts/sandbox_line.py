"""Register the shared sandbox line with LiveKit for inbound calls (idempotent).

Reads LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, SANDBOX_NUMBER and LIVEKIT_AGENT_NAME
(default careplix-agent) from .env. Creates an inbound trunk that accepts calls to the number and
a dispatch rule that sends them to the CarePlix agent. Prints nothing secret.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys

from livekit import api as lk

TRUNK_NAME = "CarePlix · Sandbox line"


def load_env() -> None:
    for line in pathlib.Path(".env").read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip())


async def main() -> int:
    load_env()
    number = os.environ.get("SANDBOX_NUMBER", "").strip()
    if not number.startswith("+"):
        print("Set SANDBOX_NUMBER (E.164, e.g. +13127558589) in .env first.", file=sys.stderr)
        return 1
    agent = os.environ.get("LIVEKIT_AGENT_NAME") or "careplix-agent"
    url = os.environ["LIVEKIT_URL"].replace("wss://", "https://")
    async with lk.LiveKitAPI(
        url, os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"]
    ) as c:
        trunks = (await c.sip.list_inbound_trunk(lk.ListSIPInboundTrunkRequest())).items
        trunk = next((t for t in trunks if number in t.numbers), None)
        if trunk is None:
            trunk = await c.sip.create_inbound_trunk(
                lk.CreateSIPInboundTrunkRequest(
                    trunk=lk.SIPInboundTrunkInfo(name=TRUNK_NAME, numbers=[number])
                )
            )
            print(f"Created inbound trunk {trunk.sip_trunk_id} for {number}.")
        else:
            print(f"Inbound trunk {trunk.sip_trunk_id} already accepts {number}.")

        rules = (await c.sip.list_dispatch_rule(lk.ListSIPDispatchRuleRequest())).items
        if any(trunk.sip_trunk_id in r.trunk_ids for r in rules):
            print("A dispatch rule already sends this trunk's calls to an agent.")
        else:
            try:
                rule = await c.sip.create_dispatch_rule(
                    lk.CreateSIPDispatchRuleRequest(
                        dispatch_rule=lk.SIPDispatchRuleInfo(
                            name=TRUNK_NAME,
                            trunk_ids=[trunk.sip_trunk_id],
                            rule=lk.SIPDispatchRule(
                                dispatch_rule_callee=lk.SIPDispatchRuleCallee(
                                    room_prefix="inb", randomize=True
                                )
                            ),
                            room_config=lk.RoomConfiguration(
                                agents=[lk.RoomAgentDispatch(agent_name=agent)]
                            ),
                            krisp_enabled=True,
                        )
                    )
                )
                print(f"Created dispatch rule {rule.sip_dispatch_rule_id} → agent {agent}.")
            except lk.TwirpError as exc:
                # A project-wide rule already covers every trunk; the API routes by called number.
                print(f"Kept the existing project-wide dispatch rule ({exc.message}).")

    print(
        "\nTwilio console, one time: Elastic SIP Trunking → your trunk → Origination → add the "
        "Origination URI shown in LiveKit Cloud → Settings → SIP URI (sip:…sip.livekit.cloud), "
        f"and make sure {number} is listed under that trunk's Numbers."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
