"""Draw labels with Pillow and turn them into printer rasters.

A label image is drawn the way you read it: x runs along the tape (the feed
direction), y runs across it. Height is always the print head's 96 dots
(12 mm); width is the label length at 8 dots per mm.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import protocol as P

# Bundled DejaVu fonts (fonts/DejaVu-LICENSE), so a label looks the same
# whether it is drawn on the Mac or on the homelab.
_FONT_DIR = Path(__file__).parent / "fonts"
FONTS = {
    "sans-bold": "DejaVuSans-Bold.ttf",
    "sans": "DejaVuSans.ttf",
    "condensed-bold": "DejaVuSansCondensed-Bold.ttf",
    "condensed": "DejaVuSansCondensed.ttf",
    "serif": "DejaVuSerif.ttf",
    "mono": "DejaVuSansMono.ttf",
}

HEIGHT = P.HEAD_DOTS
EDGE = 4  # dots kept clear at each edge, across and along the tape

# Limits on what a caller may ask for, so one request can't make the service
# draw (and hold in memory) an enormous image.
MIN_LENGTH_MM, MAX_LENGTH_MM = 8, 300
MAX_TEXT = 300
MAX_LINES = 6
MAX_QR = 500
MAX_FONT_DOTS = 2 * HEIGHT


def check_length(length_mm: float) -> float:
    try:
        length_mm = float(length_mm)
    except (TypeError, ValueError):
        raise ValueError("length_mm must be a number") from None
    if not MIN_LENGTH_MM <= length_mm <= MAX_LENGTH_MM:
        raise ValueError(f"length_mm must be between {MIN_LENGTH_MM} and {MAX_LENGTH_MM}")
    return length_mm


def check_text(text: str, what: str = "text") -> str:
    if not isinstance(text, str):
        raise ValueError(f"{what} must be a string")
    if len(text) > MAX_TEXT:
        raise ValueError(f"{what} is too long (at most {MAX_TEXT} characters)")
    if text.count("\n") >= MAX_LINES:
        raise ValueError(f"{what} has too many lines (at most {MAX_LINES})")
    return text


def check_qr(data: str | None) -> str | None:
    if not data:
        return None
    if not isinstance(data, str) or len(data) > MAX_QR:
        raise ValueError(f"qr must be a string of at most {MAX_QR} characters")
    return data


@dataclass
class LabelSpec:
    text: str = ""
    length_mm: float = 40
    font: str = "sans-bold"
    size: int | None = None  # dots; None fits the text to the label
    align: str = "center"  # left | center | right
    qr: str | None = None  # data for a QR code at the left end
    border: bool = False
    invert: bool = False


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_FONT_DIR / FONTS.get(name, FONTS["sans-bold"])), size)


def _text_ink(text: str, font, align: str) -> Image.Image:
    """Draw (multi-line) text and crop it to the ink, so centring ignores
    empty ascender/descender space."""
    size = font.size
    canvas = Image.new("L", (size * (len(text) + 2), size * 2 * (text.count("\n") + 2)), 255)
    ImageDraw.Draw(canvas).multiline_text(
        (size, size // 2), text, font=font, fill=0, align=align, spacing=max(2, size // 6)
    )
    bbox = ImageOps.invert(canvas).getbbox()
    return canvas.crop(bbox) if bbox else Image.new("L", (1, 1), 255)


def fit_ink(text: str, font: str, box_w: int, box_h: int, align: str = "center", size: int | None = None) -> Image.Image:
    """Text cropped to its ink, at `size` or the largest size that fits the box."""
    if size:
        return _text_ink(text, _font(font, size), align)
    lo, hi = 6, box_h * 2
    while lo < hi:
        mid = (lo + hi + 1) // 2
        w, h = _text_ink(text, _font(font, mid), align).size
        lo, hi = (mid, hi) if w <= box_w and h <= box_h else (lo, mid - 1)
    return _text_ink(text, _font(font, lo), align)


def finish(img: Image.Image) -> Image.Image:
    """Threshold a greyscale label to the printer's 1-bit image."""
    return img.point(lambda v: 0 if v < 128 else 255, mode="1")


def qr_image(data: str, size: int) -> Image.Image:
    import qrcode

    q = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_M)
    q.add_data(data)
    q.make(fit=True)
    matrix = q.get_matrix()
    n = len(matrix)
    img = Image.new("L", (n, n), 255)
    img.putdata([0 if on else 255 for row in matrix for on in row])
    scale = max(1, size // n)
    return img.resize((n * scale, n * scale), Image.NEAREST)


def render(spec: LabelSpec) -> Image.Image:
    """Return a 1-bit image, WIDTH x 96, black = burn."""
    check_text(spec.text)
    spec.qr = check_qr(spec.qr)
    if spec.size is not None and not (isinstance(spec.size, int) and 6 <= spec.size <= MAX_FONT_DOTS):
        raise ValueError(f"size must be a whole number between 6 and {MAX_FONT_DOTS}")
    width = round(check_length(spec.length_mm) * P.DOTS_PER_MM)
    img = Image.new("L", (width, HEIGHT), 255)
    draw = ImageDraw.Draw(img)
    x0, x1 = EDGE, width - EDGE
    inner_h = HEIGHT - 2 * EDGE

    if spec.border:
        draw.rounded_rectangle([1, 1, width - 2, HEIGHT - 2], radius=8, outline=0, width=2)
        x0, x1 = x0 + 4, x1 - 4
        inner_h -= 8

    if spec.qr:
        code = qr_image(spec.qr, inner_h)
        img.paste(code, (x0, (HEIGHT - code.height) // 2))
        x0 += code.width + 6

    if spec.text:
        ink = fit_ink(spec.text, spec.font, x1 - x0, inner_h, spec.align, spec.size)
        x = {"left": x0, "right": x1 - ink.width}.get(spec.align, x0 + (x1 - x0 - ink.width) // 2)
        img.paste(ink, (x, (HEIGHT - ink.height) // 2))

    if spec.invert:
        img = ImageOps.invert(img)
    return finish(img)


def to_raster(img: Image.Image) -> tuple[bytes, int]:
    """Column-major, LSB-first raster. Image row y maps to head dot y."""
    img = img.convert("1")
    w, h = img.size
    assert h == HEIGHT, f"label image must be {HEIGHT} dots high, got {h}"
    px = img.load()
    rows = [[px[x, y] == 0 for x in range(w)] for y in range(h)]
    return P.raster_from_rows(rows)


def preview(img: Image.Image, scale: int = 4) -> Image.Image:
    """Scaled-up picture of the label on white tape, for showing before printing."""
    pad = 6
    w, h = img.size
    out = Image.new("RGB", ((w + 2 * pad) * scale, (h + 2 * pad) * scale), (225, 225, 225))
    tape = Image.new("RGB", (w * scale, h * scale), (255, 255, 255))
    ink = img.convert("L").resize((w * scale, h * scale), Image.NEAREST)
    tape.paste((20, 20, 20), mask=ImageOps.invert(ink))
    out.paste(tape, (pad * scale, pad * scale))
    return out
