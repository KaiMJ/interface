"""Set-of-Marks overlay — the discovery-time view.

The model sees the screenshot with numbered boxes over every candidate and replies with a mark
id, not coordinates. Choosing from an enumerated set is what makes discovery recordings
replayable by construction.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..schema import Element, ElementSource, Observation

# Marks are indices into `Observation.elements`, so a reply is looked up, not interpreted.
# Controls are drawn boldly and text lines faintly: both are addressable, but a dense
# back-office screen produces far more text than controls.
_CONTROL_COLOR = (255, 64, 0)
_TEXT_COLOR = (0, 128, 255)


_Font = ImageFont.ImageFont | ImageFont.FreeTypeFont
_Rect = tuple[float, float, float, float]


def _font(size: int) -> _Font:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 takes no size
        return ImageFont.load_default()


def listed(elements: tuple[Element, ...], max_items: int) -> list[int]:
    """Indices the digest lists, in reading order.

    Controls first, then text lines with whatever slots remain: the budget bounds tokens, and
    in flat reading order a dense screen fills it with static labels while the buttons below
    them go unlisted.

    Selection is by class, ordering is not: the chosen indices are sorted back into reading
    order.
    """
    controls = [i for i, el in enumerate(elements) if el.source is not ElementSource.OCR]
    texts = [i for i, el in enumerate(elements) if el.source is ElementSource.OCR]
    chosen = controls[:max_items]
    chosen += texts[: max(0, max_items - len(chosen))]
    return sorted(chosen)


def _chip(
    draw: ImageDraw.ImageDraw, x0: float, y0: float, label: str, font: _Font
) -> _Rect:
    """Where a mark's number would sit: above the box where there is room, inside it
    otherwise, so a control at y=0 does not lose its number off the top of the frame."""
    tw, th = draw.textbbox((0, 0), label, font=font)[2:]
    ty = y0 - th - 2 if y0 - th - 2 >= 0 else y0
    return (x0, ty, x0 + tw + 4, ty + th + 2)


def _overlaps(a: _Rect, b: _Rect) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def annotate(obs: Observation, out_path: Path, max_items: int = 80) -> Path:
    """Draw numbered boxes over the screenshot; return the annotated image path.

    Both images are kept in evidence: the annotated one is what the model saw, the clean one is
    what the operator sees.

    Every element is marked, because a mark is an index into `obs.elements` and the lookup
    accepts any of them; the digest is a view over that space, not a redefinition of it. Listed
    marks are drawn at full size and claim their space first; the rest get a smaller number,
    skipped where it would overprint a listed one. Hue carries class, size carries listedness.
    """
    img = Image.open(obs.screenshot_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    w, h = img.size
    on_list = set(listed(obs.elements, max_items))
    fonts = {True: _font(13), False: _font(10)}

    corners: list[tuple[float, float]] = []
    for el in obs.elements:
        is_text = el.source is ElementSource.OCR
        x0, y0 = el.bbox.x * w, el.bbox.y * h
        x1, y1 = x0 + el.bbox.w * w, y0 + el.bbox.h * h
        draw.rectangle(
            (x0, y0, x1, y1),
            outline=_TEXT_COLOR if is_text else _CONTROL_COLOR,
            width=1 if is_text else 2,
        )
        corners.append((x0, y0))

    # Listed marks first and unconditionally: they are what the prompt spells out, so they
    # are the ones that must stay readable when the screen is dense.
    claimed: list[_Rect] = []
    for mark in sorted(on_list):
        el, (x0, y0) = obs.elements[mark], corners[mark]
        color = _TEXT_COLOR if el.source is ElementSource.OCR else _CONTROL_COLOR
        box = _chip(draw, x0, y0, str(mark), fonts[True])
        draw.rectangle(box, fill=color)
        draw.text((box[0] + 2, box[1] + 1), str(mark), fill=(255, 255, 255), font=fonts[True])
        claimed.append(box)

    for mark, el in enumerate(obs.elements):
        if mark in on_list:
            continue
        color = _TEXT_COLOR if el.source is ElementSource.OCR else _CONTROL_COLOR
        box = _chip(draw, *corners[mark], str(mark), fonts[False])
        # An unreadable number is worse than none: it costs pixels over a listed mark.
        if any(_overlaps(box, c) for c in claimed):
            continue
        draw.rectangle(box, fill=color)
        draw.text((box[0] + 2, box[1] + 1), str(mark), fill=(255, 255, 255), font=fonts[False])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path


def candidate_digest(
    elements: tuple[Element, ...], max_items: int = 80
) -> list[dict[str, object]]:
    """Compact the listed elements for the prompt.

    A view over the mark space, not the mark space itself: `mark` is the element's index in
    `obs.elements`, so listing a subset leaves gaps in the numbering and every entry carries the
    number it was drawn with. Bounded, because a dense enterprise screen produces several
    hundred boxes.
    """
    digest: list[dict[str, object]] = []
    for mark in listed(elements, max_items):
        el = elements[mark]
        entry: dict[str, object] = {
            "mark": mark,
            "role": el.role or "element",
            # Rounded to the resolution a model can reason about, and to keep several hundred
            # of these from dominating the prompt.
            "box": [round(v, 3) for v in (el.bbox.x, el.bbox.y, el.bbox.w, el.bbox.h)],
        }
        label = (el.text or el.name or "").strip()
        if label:
            entry["text"] = label if len(label) <= 120 else label[:117] + "..."
        digest.append(entry)
    return digest


def unlisted(elements: tuple[Element, ...], max_items: int = 80) -> int:
    """How many marks are drawn but not described, so the model scrolls rather than concluding
    the control it needs does not exist."""
    return max(0, len(elements) - len(listed(elements, max_items)))
