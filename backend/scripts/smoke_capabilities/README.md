# Smoke capabilities

Hand-written capabilities the live smoke scripts drive. Not recordings, and not
pytest fixtures — `make test` never reads them.

| file | what it exercises | driven by |
|---|---|---|
| `read_balance.json` | `extract` plus two declared business outcomes | `smoke_replay.py` |
| `find_transaction.json` | a `find_and_act` step over a table | `smoke_scan.py` |
| `transfer_funds.json` | a step declared `risky`, so the run parks for a human | `smoke_escalate.py` |

They are hand-written on purpose. A *recorded* artifact would answer the machinery
question and the discovery question at once, so a failure would not say which half
broke. These say only: given a correct artifact, does deterministic replay do the
right thing?

Two things keep them out of the catalog. Ids are prefixed `fix_`, and the filenames
carry no `.v<n>` segment, which is what `cua.catalog.store` globs for. `recording` is
null, because nobody recorded them — which is also what lets them run at any display
geometry, since the viewport check has no recorded shape to compare against.
