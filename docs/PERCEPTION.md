# Perception & Locator Strategy

Covers: how we look at the page, how we let the LLM point at things, how we
turn that into a durable descriptor, and how replay resolves it back.

## Principle

**Perception ≠ targeting.** Vision grounds the LLM cheaply. Targeting is
semantic (role + name + geometry + fallback chain). Discovery may use pixels;
replay must not depend on them.

## Discovery pipeline (Set-of-Marks)

Per `observe()` at 1440×900: (1) capture screenshot; (2) enumerate candidates from a11y tree + DOM fallback → `Candidate[]`; (3) redactor boxes secret-shaped fields; (4) overlay draws numbered boxes; (5) send image + candidate JSON to LLM; (6) LLM returns `{action, mark_id, value?, reason}` via the `act` tool.

### Candidate enumeration

Source order (first that yields ≥1 result per node wins):

1. **Playwright accessibility snapshot** (`page.accessibility.snapshot()`).
   Filter to interactable roles: `button, link, textbox, combobox, checkbox,
   radio, tab, menuitem, option, switch, searchbox`.
2. **DOM query fallback** for missing/hidden roles: `a[href], button,
   input:not([type=hidden]), select, textarea, [role], [onclick],
   [tabindex]:not([tabindex="-1"])`.
3. **Per-frame recursion** — every iframe/frameset walked; frame identity
   stored on the candidate.

Each candidate gets:
- `id` — sequential mark number (stable within the observation).
- `role` — a11y role, or inferred (`link` for `<a href>`, `button` for
  `<input type=submit>`).
- `name` — accessible name (aria-label / label / text / value).
- `bbox` — `{x, y, w, h}` in viewport pixels.
- `dom_path` — short unique selector (nth-of-type traversal, capped depth).
- `frame` — `null` or `{ kind, ref }`.
- `tags` — free-form hints: `["primary"]`, `["destructive"]`, `["disabled"]`.

**Filtering**:
- Off-screen elements dropped.
- Overlapping duplicates (same role+name, bbox IoU > 0.9) deduped.
- Cap: 60 candidates per observation. If more, cluster by proximity + role
  and drop lowest-priority (rare on real bank UIs; common on hostile pages).

### Overlay rendering

- Numbered box per candidate, color-coded by role.
- Semi-transparent label with number + role initial.
- **Secret-field redactor** runs first: any `input[type=password]` or
  name/label matching `/ssn|password|account.*number|routing|cvv|dob/i` gets
  a solid box before overlay draws.
- Legend appended below the image: `[N] role "name"` — short, since the LLM
  also gets the JSON candidate list.
- Output: PNG in `evidence/<run_id>/steps/<step>.png` (overlay) and
  `<step>.raw.png` (redacted, no overlay).

### LLM decision

Message shape (Anthropic Claude, vision):

```
system: docs/PROMPTS.md#discover_system
user  : goal, history[], current_url, candidates[], overlay_image
tools : [{ name: "act", input_schema: ActionSchema }]
```

`ActionSchema` (Pydantic-derived JSON schema):

```json
{
  "action": "click | type | select | check | navigate | scroll | key | extract | escalate | done",
  "mark_id": "integer, required for click/type/select/check/extract",
  "value":   "string, required for type/select/key",
  "reason":  "string, ≤200 chars — appears in evidence"
}
```

Output space is **bounded** — LLM cannot emit free-form coords or selectors.
Invalid → tool error surfaced back → retry (bounded).

## Descriptor derivation

When action commits, we synthesize a `TargetDescriptor` from the chosen
candidate — this is what lands in the artifact.

```python
def to_descriptor(cand: Candidate, page_context) -> TargetDescriptor:
    return TargetDescriptor(
        primary   = RoleName(role=cand.role, name=cand.name, name_match="exact"),
        fallbacks = [
            TextNear(text=cand.name, anchor_text=nearest_label(cand, page_context)),
            LabelFor(label=label_association(cand, page_context)),   # if any
            DomPath(value=cand.dom_path),
            CssSelector(value=css_from(cand)),                       # last text-based
            BboxRatio(**normalize(cand.bbox), confidence="low"),     # last resort
        ],
        frame = cand.frame,
        discovered_via = "set_of_marks",
        confidence = 0.94 if cand.name else 0.72,
        notes = uniqueness_note(cand, page_context),
    )
```

Fallbacks whose inputs are missing (no label, no unique css) are omitted, not
included as nulls.

## Replay resolution

`Resolver.resolve(descriptor, perception) -> ResolvedTarget | None`.

```
for candidate_kind in [primary, *fallbacks]:
    matches = kind.match(perception.candidates, perception.dom)
    if len(matches) == 1:
        return ResolvedTarget(bbox=matches[0].bbox, tier=kind.name)
    if len(matches) > 1:
        matches = disambiguate(matches, descriptor.notes)  # nth, near text, etc.
        if len(matches) == 1: return ...
    # otherwise: try next fallback

log_drift_event(descriptor, exhausted=[...])
return None
```

Actions:
- **Success on primary** → normal.
- **Success on fallback tier N** → drift event `{tier: N}` written to
  evidence. If tier ≥ 3 (structural) → warn. If tier == last (bbox) → warn
  loudly.
- **All tiers fail** → hard failure per step's `on_error`.

Clicks use the resolved `bbox` center → `page.mouse.click(x, y)`. Type actions
prefer `element.fill()` when we have an element handle; fall back to focus +
keyboard for pure-pixel resolves.

## Waiting

No sleeps. Waits are declarative per step:

- `network_idle(ms)` — Playwright `page.wait_for_load_state("networkidle")`
  with timeout.
- `element_visible(descriptor)` — poll `resolve()` + visibility check.
- `text_present(str)` — poll page content.
- Default per-step timeout: 10s. Configurable in artifact.

## Frame handling

Legacy bank web apps love `<frameset>` and iframes. Enumerator recurses; each
candidate carries `frame`. Descriptors persist frame identity. Resolver
switches frames before matching. Cross-frame click paths pre-focus the frame.

## Extension

Same overlay + prompt across surfaces; swap the enumerator (a11y+DOM → per-frame legacy → AX/UIA desktop → vision detector for canvas) and the action driver (Playwright → OS APIs). Descriptors are unchanged; the resolver swaps match backends. Extension table in `ARCHITECTURE.md`.
