# Artifacts

Saved capabilities. Written by discovery, read by replay, reviewed by a human.

This directory is **generated** — a capability is what a discovery run learned,
serialized. Hand-written stand-ins used by the smoke scripts live in
[`backend/scripts/smoke_capabilities/`](../backend/scripts/smoke_capabilities/) and are
prefixed `fix_` so they stay distinct from the catalog.

```
<capability_id>.v<n>.json
```

Files on disk, no database. Versioning is in the filename, so old versions are retained
by construction.

## The three data directories

| | |
|---|---|
| [`policies/`](../policies/) | what a **human authors** — one YAML file per application: the allowlist, the risk disposition, declared runtime conditions, the sign-on recipe |
| `artifacts/` | what the system **records** — typed, versioned, agent-invocable capabilities |
| [`evidence/`](../evidence/) | what it **did** — one directory per run, whatever the outcome |

## Reading one

The schema is `cua.schema.artifact.Capability`; `REPORT.md` §2 argues its shape. The
fields a reviewer looks at first:

- **`status`** — `draft` until a human approves it. Only approved capabilities appear in
  `/capabilities/manifest`, which is what an AI agent can call.
- **`inputs` / `outputs`** — the typed contract, with outputs naming the step that
  produces them.
- **`business_outcomes`** — legitimate alternative answers ("no such member"), so a
  calling agent can branch rather than treat one as a failure. Synthesis rejects any
  detector the model proposed that was visible on the successful run's own frames, so a
  fresh recording often declares only what it inherited from app policy.
  `cua learn-outcome` teaches one by demonstration and emits the next version.
- **`steps[].note`** — a step the recording could not verify. `recorded without a
  checkpoint` means the step was kept and its assertion was not. Read these before
  approving.
- **`recording`** — provenance: which run, which model, when.

## Commands

```bash
cua catalog                                  # everything, with contracts
cua approve <capability_id> <version> --operator you
cua manifest                                 # approved capabilities as callable tools
cua replay <capability_id> --input k=v
```

All of it is also in the console at http://localhost:3000.
