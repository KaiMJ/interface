"""Set-of-Marks overlay — the discovery-time view.

The model is shown the screenshot with numbered boxes drawn over every candidate,
plus a compact JSON list of those candidates. It replies with a mark id, not with
coordinates.

This is the single decision that makes discovery recordings replayable by
construction. If the model returned pixel coordinates we would have to infer, after
the fact, *what* it meant to click — and that inference is exactly the fragile
post-hoc step this design is trying to avoid. By making it choose from an
enumerated set, the chosen element's role, name, text and box are known exactly,
and the artifact's `Target` can be written from real data rather than guessed.

Replay does not use this module at all. Replay has a `Target` and needs a
coordinate; that is `resolve/`.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..schema import Element, ElementSource, Observation

# Marks are indices into `Observation.elements`, so the model's reply — a mark
# number — is looked up rather than interpreted. Controls are drawn boldly and
# text lines faintly: both are addressable, but only one class is usually worth
# clicking, and a dense back-office screen produces far more of the other.
_CONTROL_COLOR = (255, 64, 0)
_TEXT_COLOR = (0, 128, 255)


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 takes no size
        return ImageFont.load_default()


def annotate(obs: Observation, out_path: Path) -> Path:
    """Draw numbered boxes over the screenshot; return the annotated image path.

    Both images are kept in evidence. The annotated one is what the model saw and
    therefore what any argument about a bad decision has to be litigated against;
    the clean one is what the operator sees.
    """
    img = Image.open(obs.screenshot_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    w, h = img.size
    font = _font(13)

    for mark, el in enumerate(obs.elements):
        is_text = el.source is ElementSource.OCR
        color = _TEXT_COLOR if is_text else _CONTROL_COLOR
        x0, y0 = el.bbox.x * w, el.bbox.y * h
        x1, y1 = x0 + el.bbox.w * w, y0 + el.bbox.h * h
        draw.rectangle((x0, y0, x1, y1), outline=color, width=1 if is_text else 2)

        label = str(mark)
        tw, th = draw.textbbox((0, 0), label, font=font)[2:]
        # Tag above the box where there is room, inside it at the top otherwise,
        # so a control at y=0 does not lose its number off the top of the frame.
        ty = y0 - th - 2 if y0 - th - 2 >= 0 else y0
        draw.rectangle((x0, ty, x0 + tw + 4, ty + th + 2), fill=color)
        draw.text((x0 + 2, ty + 1), label, fill=(255, 255, 255), font=font)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path


def candidate_digest(elements: tuple[Element, ...], max_items: int = 80) -> list[dict[str, object]]:
    """Compact the element list for the prompt.

    Truncated on purpose. A dense enterprise screen can produce several hundred
    boxes; sending all of them costs tokens and, more importantly, degrades the
    model's ability to pick correctly. Ordering is reading order, so truncation
    drops the bottom of the page rather than an arbitrary slice — and when it
    truncates, the loop is told, so scrolling remains available to it.
    """
    digest: list[dict[str, object]] = []
    for mark, el in enumerate(elements[:max_items]):
        entry: dict[str, object] = {
            "mark": mark,
            "role": el.role or "element",
            # Rounded to the resolution a model can actually reason about, and to
            # keep several hundred of these from dominating the prompt.
            "box": [round(v, 3) for v in (el.bbox.x, el.bbox.y, el.bbox.w, el.bbox.h)],
        }
        label = (el.text or el.name or "").strip()
        if label:
            entry["text"] = label if len(label) <= 120 else label[:117] + "..."
        digest.append(entry)
    return digest


def truncated(elements: tuple[Element, ...], max_items: int = 80) -> int:
    """How many candidates the digest left out.

    Reported to the loop rather than swallowed: "there are 40 more below" is the
    difference between the model scrolling and the model concluding the control it
    needs does not exist.
    """
    return max(0, len(elements) - max_items)
