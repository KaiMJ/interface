# Perception

Turns a screenshot into a list of things on screen. Nothing above this folder knows
what kind of application it is looking at.

```
  screen.py     capture the display        ─┐
  detect.py     where the controls are      ├──► merge.py ──► base.py
  ocr.py        what the words are         ─┘   one Element list   Perceiver
                                                                   Observation
  index.py      spatial queries over an Observation   (how replay reads it back)
  table.py      rows and columns, without a DOM
  som.py        numbered boxes drawn on the frame     (what discovery shows the model)
```

---

## 1) The seam — three protocols, and the one class that composes them (base.py)

- Screen (capture — a picture, and a hash of it)
- Detector (where are the controls? pixels in, boxes out, no text)
- TextReader (what does it say, and where?)
- Perceiver (composes the three into one Observation)
    - observe (one frame)
    - settle (observe until the screen stops changing — see below)
    - peek (the frame hash alone, ~1000x cheaper than observing)
- Unsettled (never stopped changing — terminal for the step, rather than carrying
  on with a page that is still laying out)

A new surface — a legacy frameset, a desktop app — is a new Screen/Detector pair.
`Perceiver` is concrete rather than a protocol because capture → detect → read →
merge is the same everywhere; only the collaborators change.

## 2) Sources — the swappable collaborators

- XDisplayScreen (the whole X display, not the browser viewport — the same pixels
  the operator sees over VNC, in the same coordinate space the input layer clicks in)
- ImageFileScreen (a recorded PNG sequence: replay with no browser and no display)
- OmniParserDetector (icon / control detection)
- NullDetector (detects nothing — OCR lines become the only candidates; the honest
  floor for a surface where control detection fails)
- OnnxTextReader (PP-OCR: one Element per text line, with a box and a confidence)

## 3) Fusion — detector boxes and OCR lines become one list (merge.py)

- merge (each text line joins the smallest control that contains it; ids are
  reassigned e0..eN in reading order, so a numbered overlay reads the way a person
  scans)
- infer_role (button? textbox? row? — a hint that narrows candidates, never the
  only matching key)

The two sources disagree about padding: the detector boxes the glyphs, OCR boxes the
padded line — 29x16 against 41x23 on the same button. So containment is tested both
ways, with a size guard so an icon inside a table row's text cannot claim the whole
row as its name.

## 4) Reading it back — the replay-time view (index.py, table.py)

- ElementIndex (within / right_of / left_of / below / overlapping)
    - built lazily — most observations, like settle polls, are never queried
    - overlapping is overlay detection: a modal moves nothing, it lands on top
- rows (cluster elements into visual lines)
- find_header / column_span / cell_in_column (a column is the band between one
  header and the next — not "the cells whose text overlaps the header", which never
  matches when a header is left-aligned and its values are right-aligned)

## 5) Showing it — the discovery-time view (som.py)

- annotate (draw numbered boxes on the screenshot; both the clean and the annotated
  frame are kept, because the annotated one is what the model actually saw)
- candidate_digest (the same list as text, truncated)
- truncated (how many were left out — so the model scrolls instead of concluding the
  control does not exist)

---

## One observation

```
   Screen.capture()          a PNG, and a hash of the raw pixels
        │
        ├──► Detector.detect()    where the controls are   (no text)
        └──► TextReader.read()    what the words are       (no idea what is clickable)
                    │
                    ▼
                 merge()          deduped, text joined to controls, renumbered
                    │
                    ▼
              Observation         the frame, and every Element in it
```

Neither source is enough alone: a bare box cannot be targeted semantically, and a
bare line of text is not necessarily clickable.

## Settling — how waiting works without sleep()

```
   capture ──► same hash as the last frame? ──yes──► settled (PIXELS)
        │ no
        ▼
   observe ──► same text and boxes as the last observation? ──yes──► settled (TEXT)
        │ no, until the timeout
        ▼
     Unsettled
```

The text test exists because a caret, a spinner or a clock means no two frames are
ever byte-identical. Lines the app's policy declares `volatile_text` — a countdown, a
"last refreshed" stamp — are dropped from that comparison and from nothing else.

Which test fired is recorded on the Observation: a run settling by TEXT on every step
is telling you the surface animates.
