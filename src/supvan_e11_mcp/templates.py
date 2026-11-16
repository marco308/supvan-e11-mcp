"""Ready-made label layouts, so callers send data rather than draw labels."""

from __future__ import annotations

import datetime as dt

from PIL import Image

from . import protocol as P
from . import render

def _date(value: str | dt.date | None, default: dt.date) -> dt.date:
    if value is None or value == "":
        return default
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(value)


def freezer_detail(
    frozen: str | dt.date | None = None, use_by: str | dt.date | None = None, portions: int | None = None
) -> str:
    """The line under the dish name: "Frozen 1 Oct 2026", plus a use-by month
    and portions only when given."""
    frozen_d = _date(frozen, dt.date.today())
    detail = f"Frozen {frozen_d.day} {frozen_d:%b %Y}"
    if use_by:
        detail += f" · use by {_date(use_by, frozen_d):%b %y}"
    if portions:
        detail += f" · {portions} portion{'s' if portions != 1 else ''}"
    return detail


def freezer(
    dish: str,
    frozen: str | dt.date | None = None,
    use_by: str | dt.date | None = None,
    portions: int | None = None,
    qr: str | None = None,
    length_mm: float = 40,
) -> Image.Image:
    """Dish name large on top, the date it was frozen below.

    frozen defaults to today (ISO dates). A use-by month and portions are
    added to the date line only when given: the label's job is to say what
    is in the tub and since when, and anything else crowds the dish name.
    """
    render.check_text(dish, "dish")
    if not dish.strip():
        raise ValueError("dish must not be empty")
    if portions is not None and not (isinstance(portions, int) and 0 <= portions <= 99):
        raise ValueError("portions must be a whole number between 0 and 99")
    qr = render.check_qr(qr)
    detail = freezer_detail(frozen, use_by, portions)

    width = round(render.check_length(length_mm) * P.DOTS_PER_MM)
    img = Image.new("L", (width, render.HEIGHT), 255)
    x0, x1 = render.EDGE, width - render.EDGE
    if qr:
        code = render.qr_image(qr, render.HEIGHT - 2 * render.EDGE)
        img.paste(code, (x0, (render.HEIGHT - code.height) // 2))
        x0 += code.width + 6

    # 96 dots across: ~56 for the dish, a gap, ~26 for the detail line.
    top_h, gap, bottom_h = 54, 6, 24
    title = render.fit_ink(dish, "condensed-bold", x1 - x0, top_h)
    sub = render.fit_ink(detail, "condensed", x1 - x0, bottom_h)
    block_h = title.height + gap + sub.height
    y = (render.HEIGHT - block_h) // 2
    img.paste(title, (x0 + (x1 - x0 - title.width) // 2, y))
    img.paste(sub, (x0 + (x1 - x0 - sub.width) // 2, y + title.height + gap))
    return render.finish(img)


TEMPLATES = {"freezer": freezer}


def render_template(name: str, fields: dict, length_mm: float) -> Image.Image:
    try:
        fn = TEMPLATES[name]
    except KeyError:
        raise ValueError(f"unknown template {name!r}; have {sorted(TEMPLATES)}") from None
    try:
        return fn(**fields, length_mm=length_mm)
    except TypeError as e:  # unknown or missing field names
        raise ValueError(f"bad fields for template {name!r}: {e}") from None
