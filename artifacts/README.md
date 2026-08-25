# Artifacts

Saved capabilities. Written by discovery, read by replay, reviewed by a human.

This directory is **generated**. Nothing here is hand-authored — a capability is
what a discovery run learned, serialized. Hand-written stand-ins used by the smoke
scripts live in [`backend/scripts/smoke_capabilities/`](../backend/scripts/smoke_capabilities/) and are prefixed
`fix_` so they stay distinct from anything the catalog holds.

```
<capability_id>.v<n>.json
```

Files on disk, no database. The catalog is read on every invocation and written
once per discovery run; an index would be infrastructure in search of a problem at
this size. Versioning is in the filename rather than only in the file, which means
old versions are retained by construction and a diff between v2 and v3 is
`git diff` rather than a feature.

## The three data directories

They are the system's data in the order it moves:

| | |
|---|---|
| [`policies/`](../policies/) | what a **human authors** — one YAML file per application: the allowlist, the risk disposition, declared runtime conditions, the sign-on recipe |
| `artifacts/` | what the system **records** — typed, versioned, agent-invocable capabilities |
| [`evidence/`](../evidence/) | what it **did** — one directory per run, whatever the outcome |

## Reading one

The schema is `cua.schema.artifact.Capability`, and `REPORT.md` §2 is the argument
for its shape. The fields a reviewer looks at first:

- **`status`** — `draft` until a human approves it. Only approved capabilities
  appear in `/capabilities/manifest`, which is what an AI agent can call.
- **`inputs` / `outputs`** — the contract. Typed, with the outputs naming the step
  that produces them.
- **`business_outcomes`** — legitimate alternative answers ("no such member"), so a
  calling agent can branch rather than treat one as a failure. A fresh recording
  often declares none: synthesis refuses any detector the model proposed that was
  visible on the successful run's own frames. `cua learn-outcome` teaches one by
  demonstration and emits the next version.
- **`steps[].note`** — a step the recording could not verify. `recorded without a
  checkpoint` means the run expected one thing and the screen showed another, so
  the step was kept and its assertion was not. Read these before approving.
- **`recording`** — provenance: which run, which model, when. Absent means nobody
  recorded this, which for anything in this directory would be a mistake.

## Commands

```bash
cua catalog                                  # everything, with contracts
cua approve <capability_id> <version> --operator you
cua manifest                                 # approved capabilities as callable tools
cua replay <capability_id> --input k=v
```

All of it is also in the console at http://localhost:3000.
