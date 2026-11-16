"""The label MCP tools, shared by the local (stdio) server and the homelab service.

A backend is anything with `async status() -> dict`, `async feed()`,
`async print_raster(raster, cols, density)` and `async release()`: the Mac
bridge client or the in-process homelab Bridge.
"""

from __future__ import annotations

import functools
import io
import logging
from typing import Protocol

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from PIL import Image as PILImage

from . import render, templates

log = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Prints labels on a SUPVAN E11 Bluetooth label maker (12 mm wide tape, die-cut labels, "
    "usually 40 mm long). Always preview first (preview_label or preview_freezer_label) and "
    "show the user the picture; print only once they are happy with it. Use \\n in text for "
    "a second line. Leave length_mm unset to use the loaded label length."
)


class Backend(Protocol):
    async def status(self) -> dict: ...
    async def feed(self) -> None: ...
    async def print_raster(self, raster: bytes, cols: int, density: int = 4) -> None: ...
    async def release(self) -> None: ...


class Labels:
    """Rendering and printing on top of a backend; also used by the HTTP API."""

    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self._length_mm: float | None = None
        # Called with the number of labels about to print; raises to refuse.
        self.quota = lambda n: None

    async def length(self, length_mm: float | None) -> float:
        if length_mm:
            return length_mm
        if self._length_mm is None:
            try:
                mat = (await self.backend.status()).get("material") or {}
                self._length_mm = float(mat.get("length_mm") or 40)
            except Exception:  # noqa: BLE001 - printer off: fall back to the usual roll
                return 40.0
        return self._length_mm

    async def status(self) -> dict:
        info = await self.backend.status()
        mat = info.get("material") or {}
        if mat.get("length_mm"):
            self._length_mm = float(mat["length_mm"])
        st = info.get("status") or {}
        # ribbon_end is set during normal E-series prints; not a problem.
        problems = {k: v for k, v in st.items() if v is True and k != "ribbon_end"}
        return {
            "model": info.get("device_name"),
            "bluetooth_name": info.get("advertised_name"),
            "tape": {
                "width_mm": mat.get("width_mm"),
                "label_length_mm": mat.get("length_mm"),
                "gap_mm": mat.get("gap_mm"),
                "labels_remaining": mat.get("remaining"),
            },
            "problems": problems or None,
        }

    async def text_image(self, **spec) -> PILImage.Image:
        spec["length_mm"] = await self.length(spec.get("length_mm"))
        spec["qr"] = spec.get("qr") or None
        return render.render(render.LabelSpec(**spec))

    async def template_image(self, template: str, fields: dict, length_mm: float | None = None) -> PILImage.Image:
        return templates.render_template(template, fields, await self.length(length_mm))

    async def print_image(self, img: PILImage.Image, copies: int = 1, density: int = 4) -> str:
        if not 1 <= copies <= 20:
            raise ValueError("copies must be between 1 and 20")
        if not 1 <= density <= 15:
            raise ValueError("density must be between 1 and 15")
        self.quota(copies)
        raster, cols = render.to_raster(img)
        for i in range(copies):
            try:
                await self.backend.print_raster(raster, cols, density)
            except Exception as e:
                raise RuntimeError(f"Printed {i} of {copies}; then: {e}") from e
        return f"Printed {copies} label{'s' if copies > 1 else ''}."


def png(img: PILImage.Image) -> bytes:
    buf = io.BytesIO()
    render.preview(img).save(buf, "PNG")
    return buf.getvalue()


def _explained(fn):
    """Pass the failure's message to the model. The SDK shows only "Error executing tool"
    for anything but a ToolError, which hides why a print failed (printer off, link
    dropped, printer busy); the HTTP API already returns the same text."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except Exception as e:
            log.warning("tool %s failed", fn.__name__, exc_info=True)
            raise ToolError(str(e) or type(e).__name__) from e

    return wrapper


def build_server(labels: Labels, **kw) -> MCPServer:
    mcp = MCPServer("supvan-e11", instructions=INSTRUCTIONS, **kw)
    tool = lambda fn: mcp.tool()(_explained(fn))  # noqa: E731

    @tool
    async def printer_status() -> dict:
        """Connect to the label printer and report model, loaded tape (width, label length,
        labels remaining) and any problem such as the cover being open or the tape running out."""
        return await labels.status()

    @tool
    async def preview_label(
        text: str,
        length_mm: float | None = None,
        font: str = "sans-bold",
        size: int | None = None,
        align: str = "center",
        qr: str | None = None,
        border: bool = False,
        invert: bool = False,
    ) -> Image:
        """Draw a text label and return a picture of it without printing.

        text: label text; use \\n for a new line (two lines fit comfortably, three is tiny).
        length_mm: label length along the tape; default is the loaded die-cut label length.
        font: sans-bold (default), sans, condensed-bold, condensed, serif or mono.
        size: font height in dots (8 dots per mm, the tape is 96 dots high); default fits the text.
        align: left, center or right.
        qr: if set, a QR code holding this text/URL goes at the left end.
        border: draw a rounded border. invert: white text on black.
        """
        img = await labels.text_image(
            text=text, length_mm=length_mm, font=font, size=size, align=align, qr=qr, border=border, invert=invert
        )
        return Image(data=png(img), format="png")

    @tool
    async def print_label(
        text: str,
        length_mm: float | None = None,
        font: str = "sans-bold",
        size: int | None = None,
        align: str = "center",
        qr: str | None = None,
        border: bool = False,
        invert: bool = False,
        copies: int = 1,
        density: int = 4,
    ) -> str:
        """Print a text label. Takes the same arguments as preview_label, plus:

        copies: how many to print (1-20).
        density: darkness, 1-15 (default 4); raise it if the print is faint.
        """
        img = await labels.text_image(
            text=text, length_mm=length_mm, font=font, size=size, align=align, qr=qr, border=border, invert=invert
        )
        return await labels.print_image(img, copies, density)

    @tool
    async def preview_freezer_label(
        dish: str,
        frozen: str | None = None,
        use_by: str | None = None,
        portions: int | None = None,
        qr: str | None = None,
    ) -> Image:
        """Picture of a freezer label: dish name large, then the date it was frozen.
        frozen defaults to today; use_by and portions are added only when given (ISO dates,
        e.g. 2026-10-01). qr: optional text/URL for a QR code at the left end."""
        fields = {"dish": dish, "frozen": frozen, "use_by": use_by, "portions": portions, "qr": qr}
        return Image(data=png(await labels.template_image("freezer", fields)), format="png")

    @tool
    async def print_freezer_label(
        dish: str,
        frozen: str | None = None,
        use_by: str | None = None,
        portions: int | None = None,
        qr: str | None = None,
        copies: int = 1,
    ) -> str:
        """Print a freezer label; same arguments as preview_freezer_label, plus copies
        (e.g. one per container)."""
        fields = {"dish": dish, "frozen": frozen, "use_by": use_by, "portions": portions, "qr": qr}
        return await labels.print_image(await labels.template_image("freezer", fields), copies)

    @tool
    async def feed_tape() -> str:
        """Feed the tape on by one label without printing."""
        await labels.backend.feed()
        return "Fed one label."

    @tool
    async def release_printer() -> str:
        """Drop the Bluetooth connection so the phone app can connect. The connection comes
        back by itself next time, and is also dropped after two idle minutes."""
        await labels.backend.release()
        return "Disconnected."

    return mcp
