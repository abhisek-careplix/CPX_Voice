"""Healthcare integration layer: connectors that let agents see, validate and act on payer data.

Only the control plane calls connectors. Models never hold a raw client; validation runs as a
deterministic workflow step, and any future agent-facing capability goes through the server-side
tool authorisation (SEC06). See docs/solution/HEALTHCARE_INTEGRATION_LAYER.md.
"""
