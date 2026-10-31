import pytest
from fastapi.testclient import TestClient

from careplix_api.auth import DEV_WORKER_TOKEN
from careplix_api.livekit_gateway import DialOutcome, LiveKitGateway
from careplix_api.main import create_app
from careplix_api.settings import Settings

APPROVED = "+12025550142"
MODEL = "gemini-test-live"


class FakeGateway(LiveKitGateway):
    """Records LiveKit calls instead of making them."""

    def __init__(self, settings):
        super().__init__(settings)
        self.calls: list[tuple] = []
        self.dial_outcome = DialOutcome(
            True, "connected", participant_id="PA_x", sip_call_id="SC_x"
        )

    async def create_room_and_dispatch(self, room, attempt_id):
        self.calls.append(("dispatch", room, attempt_id))
        return "AD_fake"

    def browser_token(self, room, identity, display_name):
        return f"token-for-{room}"

    async def dial(self, room, attempt_id, destination, trunk):
        self.calls.append(("dial", room, destination))
        return self.dial_outcome

    async def sip_participant_present(self, room, attempt_id):
        return False

    async def room_exists(self, room):
        return False

    async def delete_room(self, room):
        self.calls.append(("delete", room))

    # A small in-memory LiveKit project for Connections flows.
    outbound_trunks: list
    dispatch_rules: list

    async def inventory(self):
        return {
            "active_rooms": 0,
            "outbound_trunks": list(getattr(self, "outbound_trunks", [])),
            "inbound_trunks": [],
            "dispatch_rules": list(getattr(self, "dispatch_rules", [])),
        }

    async def create_inbound_rule(self, name, number, route_id):
        # Like LiveKit Cloud for purchased numbers: the number scope is not stored.
        rule = {
            "id": f"SDR_{len(getattr(self, 'dispatch_rules', [])) + 1}",
            "name": name,
            "numbers": [],
            "trunk_ids": [],
            "agents": ["careplix-agent"],
        }
        self.dispatch_rules = [*getattr(self, "dispatch_rules", []), rule]
        return rule

    async def create_outbound_trunk(self, name, address, number, username, password, transport):
        self.calls.append(("trunk", address, number, password))
        self.outbound_trunks = [
            {
                "id": "ST_new",
                "name": name,
                "address": address,
                "numbers": [number],
                "has_auth": bool(username),
            }
        ]
        return "ST_new"

    async def dispatch_probe(self, room, probe_id):
        self.calls.append(("probe", room, probe_id))


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "_env_file": None,
        "database_url": f"sqlite+aiosqlite:///{tmp_path}/test.db",
        "gemini_live_model": MODEL,
        "livekit_url": "wss://fake.livekit.cloud",
        "livekit_api_key": "key",
        "livekit_api_secret": "s" * 40,
        "livekit_outbound_trunk_id": "ST_test",
        "approved_test_destinations": APPROVED,
        "worker_claim_timeout_s": 3,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def gateway(settings):
    return FakeGateway(settings)


@pytest.fixture
def client(settings, gateway):
    with TestClient(create_app(settings, gateway)) as c:
        c.post("/api/auth/dev-login").raise_for_status()
        yield c


WORKER = {"Authorization": f"Bearer {DEV_WORKER_TOKEN}"}
