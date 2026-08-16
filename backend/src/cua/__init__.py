"""Computer-use automation.

The model discovers. The artifact becomes a reusable capability. Deterministic
replay is how the AI agent invokes it in production.

Layering — each depends only on what is below it:

    api / cli            control plane
    runtime              composition root, session lifecycle
    discovery | replay   the two execution paths
    resolve              semantic target -> coordinate, plus verification
    perception | action  the surface seam (swap these, not the schema)
    policy | evidence    cross-cutting guardrails and audit
    schema               typed contracts; depends on nothing
"""

__version__ = "0.1.0"
