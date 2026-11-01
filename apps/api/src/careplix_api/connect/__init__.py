"""CarePlix Connect: the EHR integration hub (docs/solution/INTEGRATION_HUB_DESIGN.md).

- ``catalog``: every EHR profile CarePlix can reach and which operations each supports.
- ``base``: the connector contract every EHR path implements.
- ``registry``: the connector for a practice connection (including OpenEMR emulating a profile).

Engines (sync, write intents, backfill) talk to connectors only; they never branch on the EHR.
"""
