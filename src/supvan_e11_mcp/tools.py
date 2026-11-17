"""The label MCP tools, shared by the local (stdio) server and the homelab service.

A backend is anything with `async status() -> dict`, `async feed()`,
`async print_raster(raster, cols, density)` and `async release()`: the Mac
bridge client or the in-process homelab Bridge.
"""

from __future__ import annotations

import functools
import io
import logging
import secrets
import time
from collections.abc import Callable
from typing import Protocol

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from PIL import Image as PILImage

from . import render, templates

log = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Prints labels on a SUPVAN E11 Bluetooth label maker (12 mm wide tape, die-cut labels, "
    "usually 40 mm long). Always preview first (preview_label or preview_freezer_label) and "
    "show the user the picture; print only once they are happy with it. print_label prints "
    "exactly what a preview showed: pass the preview_id it returned. Each preview prints once "
    "(use copies for more than one) and lasts 10 minutes. Use a line break in text for a "
    "second line. Leave length_mm unset to use the loaded label length."
)

PREVIEW_TTL = 600.0  # seconds a preview can be printed for


class Previews:
    """The labels an agent has been shown. The MCP print tool prints only one of these, and
    only once, so an agent can't print something nobody saw, and a retry or a second turn
    after a dropped link can't print the same label twice. The REST API doesn't use this:
    an app's print button is already a person choosing one label."""

    def __init__(self, ttl: float = PREVIEW_TTL, clock: Callable[[], float] = time.monotonic, keep: int = 200) -> None:
        self.ttl, self.clock, self.keep = ttl, clock, keep
        self._items: dict[str, dict] = {}

    def add(self, img: PILImage.Image, client: str) -> str:
        now = self.clock()
        self._items = {k: v for k, v in self._items.items() if now - v["at"] < self.ttl}
        while len(self._items) >= self.keep:
            self._items.pop(next(iter(self._items)))
        pid = secrets.token_urlsafe(9)
        self._items[pid] = {"img": img, "client": client, "at": now, "used": False}
        return pid

    def take(self, pid: str, client: str) -> PILImage.Image:
        """The previewed image, marked as printed. Raises ValueError to refuse."""
        item = self._items.get(pid or "")
        if item is None or item["client"] != client:
            raise ValueError("No preview with that id. Preview the label, show it to the user, then print with its preview_id.")
        if self.clock() - item["at"] >= self.ttl:
            raise ValueError("That preview is more than 10 minutes old. Preview the label again.")
        if item["used"]:
            raise ValueError("That preview has already been printed. Preview it again to print it again.")
        item["used"] = True  # before printing: if the link drops mid-print, it stays printed
        return item["img"]


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


def build_server(labels: Labels, who: Callable[[], str] = lambda: "-", previews: Previews | None = None, **kw) -> MCPServer:
    """who: the calling client's name (the homelab service's token name), so one client
    can't print another's preview."""
    mcp = MCPServer("supvan-e11", instructions=INSTRUCTIONS, **kw)
    tool = lambda fn: mcp.tool()(_explained(fn))  # noqa: E731
    previews = previews or Previews()

    def shown(img: PILImage.Image) -> list:
        pid = previews.add(img, who())
        return [Image(data=png(img), format="png"), f"preview_id: {pid} (print it with print_label within 10 minutes)"]

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
    ) -> list:
        """Draw a text label and return a picture of it, and a preview_id, without printing.

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
        return shown(img)

    @tool
    async def print_label(preview_id: str, copies: int = 1, density: int = 4) -> str:
        """Print a label the user has seen: exactly the picture from preview_label or
        preview_freezer_label. Show the user the preview and print only once they're happy.

        preview_id: from the preview, made in the last 10 minutes. Each preview prints once;
            preview again to print it again.
        copies: how many to print (1-20).
        density: darkness, 1-15 (default 4); raise it if the print is faint.
        """
        if not 1 <= copies <= 20:
            raise ValueError("copies must be between 1 and 20")
        if not 1 <= density <= 15:
            raise ValueError("density must be between 1 and 15")
        return await labels.print_image(previews.take(preview_id, who()), copies, density)

    @tool
    async def preview_freezer_label(
        dish: str,
        frozen: str | None = None,
        use_by: str | None = None,
        portions: int | None = None,
        qr: str | None = None,
    ) -> list:
        """Picture of a freezer label, and a preview_id to print it with print_label: dish name
        large, then the date it was frozen.
        frozen defaults to today; use_by and portions are added only when given (ISO dates,
        e.g. 2026-10-01). qr: optional text/URL for a QR code at the left end."""
        fields = {"dish": dish, "frozen": frozen, "use_by": use_by, "portions": portions, "qr": qr}
        return shown(await labels.template_image("freezer", fields))

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
