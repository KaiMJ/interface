# Smoke capabilities

Hand-written capabilities the live smoke scripts drive. Not recordings, and not pytest
fixtures — `make test` never reads them. Hand-written on purpose: a recorded artifact
would test the machinery and discovery at once, so a failure would not say which half
broke.

| file | what it exercises | driven by |
|---|---|---|
| `read_balance.json` | `extract` plus two declared business outcomes | `smoke_replay.py` |
| `find_transaction.json` | a `find_and_act` step over a table | `smoke_scan.py` |
| `transfer_funds.json` | a step declared `risky`, so the run parks for a human | `smoke_escalate.py` |

Two things keep them out of the catalog: ids are prefixed `fix_`, and the filenames carry
no `.v<n>` segment, which is what `cua.catalog.store` globs for. `recording` is null, which
is also what lets them run at any display geometry — the viewport check has no recorded
shape to compare against.
