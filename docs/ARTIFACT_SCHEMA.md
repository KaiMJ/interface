# Artifact Schema

The artifact is the contract between the *discovery* run that wrote it and the
*agents* that will invoke it. It is designed to be:

- **Typed** — inputs/outputs are declared and validated (Pydantic v2).
- **Reviewable** — a human should read it and understand what happens.
- **Versioned + content-addressed** — semantic edits produce a new hash.
- **Surface-agnostic** — descriptors are semantic, not Playwright-specific.
- **Multi-tenant-ready** — a base artifact can be overridden per tenant.

Storage: `artifacts/<app>/<capability_id>.v<n>.json`.
Content-addressed id: `sha256(canonical(artifact))[:12]`.

## Top-level

```jsonc
{
  "id": "cap_transfer_funds",
  "content_hash": "a3f19c7b2e04",
  "version": 3,
  "created_at": "2026-08-14T10:22:00Z",
  "created_by": { "mode": "discovery", "run_id": "run_..." },
  "status": "draft",                      // draft | approved | deprecated

  "app": {
    "name": "parabank",
    "vendor": "parasoft",
    "base_url_pattern": "https://{host}/parabank",
    "url_params": { "host": { "type": "string", "example": "parabank.parasoft.com" } }
  },

  "goal": "Transfer funds between two of the logged-in member's accounts.",
  "description": "Assumes the user is already authenticated. Navigates to the transfer form, submits, and captures the confirmation id.",

  "inputs":  [ /* see below */ ],
  "outputs": [ /* see below */ ],

  "preconditions": [
    { "kind": "url_matches", "value": ".*/parabank/(overview|index).*" },
    { "kind": "text_present", "value": "Accounts Overview" }
  ],

  "steps":    [ /* see below */ ],

  "success":  { "kind": "text_present", "value": "Transfer Complete" },

  "business_outcomes": [
    {
      "name": "insufficient_funds",
      "detector": { "kind": "text_present", "value": "insufficient funds" },
      "result_shape": { "reason": "string" },
      "severity": "info"
    }
  ],

  "policy_ref": "policies/parabank.yaml",
  "stability": {
    "runs": 12, "successes": 11, "outcomes": 1, "failures": 0,
    "last_replayed_at": "2026-08-14T09:41:11Z"
  }
}
```

## Inputs

```jsonc
"inputs": [
  {
    "name": "from_account",
    "type": "string",
    "required": true,
    "constraints": { "pattern": "^\\d{5}$" },
    "description": "Source account id (5 digits)."
  },
  {
    "name": "to_account",
    "type": "string",
    "required": true,
    "constraints": { "pattern": "^\\d{5}$", "not_equal_to_input": "from_account" }
  },
  {
    "name": "amount",
    "type": "number",
    "required": true,
    "constraints": { "min": 0.01, "max": 10000 }
  },
  {
    "name": "member_password",
    "type": "secret_ref",           // resolves from vault at replay; never persisted
    "required": true
  }
]
```

Types: `string | number | integer | boolean | enum | date | secret_ref`.
`secret_ref` values are placeholders in the artifact (`<<SECRET:member_password>>`);
the action layer substitutes at execution.

## Outputs

```jsonc
"outputs": [
  {
    "name": "confirmation_id",
    "type": "string",
    "extract": {
      "step": 6,
      "descriptor": {
        "primary":  { "kind": "role_name", "role": "text", "name": "Confirmation ID" },
        "fallbacks":[{ "kind": "text_near", "text": "Confirmation", "anchor_text": "ID" }]
      },
      "transform": "trim"
    }
  },
  {
    "name": "new_from_balance",
    "type": "number",
    "extract": { "step": 6, "descriptor": { ... }, "transform": "currency_to_number" }
  }
]
```

Outputs are declared, not "whatever the LLM extracted." At replay time the
resolver locates the element, applies the transform, validates the type.

## Steps

```jsonc
"steps": [
  {
    "id": 1,
    "action": "click",
    "target": { "primary": { "kind": "role_name", "role": "link", "name": "Transfer Funds" }, ... },
    "input_ref": null,
    "checkpoint": { "kind": "url_matches", "value": ".*/transfer.*" },
    "wait": { "kind": "network_idle", "timeout_ms": 5000 },
    "on_error": "hard_fail"
  },
  {
    "id": 2,
    "action": "type",
    "target": { "primary": { "kind": "role_name", "role": "textbox", "name": "Amount" }, ... },
    "input_ref": "amount",
    "checkpoint": { "kind": "field_value_matches", "target_ref": "self", "value_from_input": "amount" },
    "on_error": "hard_fail"
  },
  {
    "id": 3,
    "action": "select",
    "target": { "primary": { "kind": "role_name", "role": "combobox", "name": "From Account" }, ... },
    "input_ref": "from_account",
    "checkpoint": { "kind": "select_value_matches", "value_from_input": "from_account" },
    "on_error": "hard_fail"
  },
  // ... to_account select, submit, confirmation page ...
  {
    "id": 6,
    "action": "extract",             // no-op action; extract runs
    "target": null,
    "checkpoint": { "kind": "text_present", "value": "Transfer Complete" },
    "on_error": "hard_fail"
  }
]
```

Actions: `click | type | select | check | uncheck | navigate | scroll | key |
extract | wait | assert`.

`on_error`: `hard_fail | escalate | ignore | retry(n)`.

## Target descriptor

The load-bearing type. Never a bare selector.

```jsonc
{
  "primary":   { "kind": "role_name", "role": "button", "name": "Transfer",
                 "name_match": "exact" /* exact | contains | regex */ },
  "fallbacks": [
    { "kind": "text_near",    "text": "Transfer", "anchor_text": "Amount",
      "max_px": 200 },
    { "kind": "label_for",    "label": "Transfer Amount" },
    { "kind": "dom_path",     "value": "form#transferForm input[type=submit]" },
    { "kind": "css",          "value": "input[value='Transfer']" },
    { "kind": "bbox_ratio",   "x": 0.42, "y": 0.71, "w": 0.08, "h": 0.04,
      "confidence": "low" }
  ],
  "frame":     null,                   // or { "kind": "name"|"index", ... }
  "discovered_via": "set_of_marks",
  "confidence": 0.94,
  "notes": "Only submit-type input in the transfer form."
}
```

Fallback kinds (in preferred order):
1. `role_name` — a11y role + accessible name. Most stable.
2. `text_near` — visible text with a spatial anchor (label near input).
3. `label_for` — form label association.
4. `dom_path` — structural selector (last resort before pixels).
5. `css` / `xpath` — same tier, escape hatch.
6. `bbox_ratio` — normalized viewport coords. Always marked
   `confidence: "low"`; use of this fallback logs a drift event.

## Checkpoints

Declarative, no `sleep()`:

- `url_matches(regex)`
- `text_present(str)` / `text_absent(str)`
- `element_visible(descriptor)`
- `field_value_matches(target_ref, value_from_input | literal)`
- `select_value_matches(value_from_input | literal)`
- `network_idle(timeout_ms)`
- `custom_js(script)` — escape hatch, allowlist-checked

Each step has a `checkpoint`; the artifact has a top-level `success` (final
checkpoint).

## Business outcomes

First-class alternative results — not failures. Declared per artifact so the
caller knows what shapes to expect.

```jsonc
{
  "name": "account_not_found",
  "detector": { "kind": "text_present", "value": "no such account" },
  "result_shape": { "account_id": "string", "reason": "string" },
  "extract": [
    { "field": "account_id", "from_input": "from_account" },
    { "field": "reason",     "from_element": { "descriptor": {...} } }
  ],
  "severity": "info"      // info | warn — never error
}
```

Detectors are evaluated **before** the success check at each step. First
match wins; run stops cleanly with the typed outcome.

## Recoverable conditions

Declared per **app policy** (see `policies/<app>.yaml`), not per artifact — they apply across all capabilities on the same app. Each entry: detector + ordered recovery actions + `max_per_run` cap.

## Versioning & diffs

- `version` is an integer, monotonic per `id`.
- `content_hash` is `sha256(canonical(artifact))[:12]` — canonicalization
  strips volatile fields (`created_at`, `stability`).
- Any change that alters `content_hash` produces a new `version` and a new
  file. Old versions retained.
- UI renders a semantic diff (step-level, descriptor-level) to make review
  cheap.

## Multi-tenant overrides

Sibling file, not a fork:

```jsonc
// tenants/first_national/parabank/transfer_funds.overrides.json
{
  "artifact_ref": "cap_transfer_funds@v3",
  "app_overrides": {
    "base_url_pattern": "https://parabank.firstnational.example/parabank"
  },
  "input_defaults": {
    "member_password": { "kind": "secret_ref", "ref": "vault://fn/parabank/pw" }
  },
  "descriptor_overrides": {
    "step_id=2": {
      "primary": { "kind": "role_name", "role": "textbox", "name": "Amount ($)" }
    }
  },
  "extra_business_outcomes": [
    { "name": "regional_hold", "detector": {...}, "result_shape": {...} }
  ]
}
```

Layering rule: overrides are merged at load time; base artifact is never
mutated. Missing overrides = base wins.

