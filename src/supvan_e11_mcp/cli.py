"""Command line for testing the printer without an MCP client."""

from __future__ import annotations

import argparse
import asyncio
import json

from . import client, render


async def _status(args) -> None:
    print(json.dumps(await client.status(), indent=2))


async def _feed(args) -> None:
    await client.feed()


async def _stop(args) -> None:
    await client.call("disconnect")


async def _print(args) -> None:
    spec = render.LabelSpec(
        text=args.text.replace("\\n", "\n"), length_mm=args.length, font=args.font, size=args.size,
        align=args.align, qr=args.qr, border=args.border, invert=args.invert,
    )
    img = render.render(spec)
    if args.preview:
        render.preview(img).save(args.preview)
        print(f"preview saved to {args.preview}")
    if args.dry_run:
        return
    raster, cols = render.to_raster(img)
    await client.print_raster(raster, cols, args.density)
    print("printed")


def main() -> None:
    ap = argparse.ArgumentParser(prog="supvan-e11")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="connect and report model, tape and status")
    sub.add_parser("feed", help="feed a blank length of tape")
    sub.add_parser("disconnect", help="drop the Bluetooth link so the phone app can connect")
    pr = sub.add_parser("print", help="print a text label (use \\n for a new line)")
    pr.add_argument("text")
    pr.add_argument("--length", type=float, default=40, help="label length in mm")
    pr.add_argument("--font", default="sans-bold", choices=sorted(render.FONTS))
    pr.add_argument("--size", type=int, help="font size in dots (default: fit)")
    pr.add_argument("--align", default="center", choices=["left", "center", "right"])
    pr.add_argument("--qr", help="put a QR code with this data at the left end")
    pr.add_argument("--border", action="store_true")
    pr.add_argument("--invert", action="store_true")
    pr.add_argument("--density", type=int, default=4)
    pr.add_argument("--preview", help="save a PNG preview here")
    pr.add_argument("--dry-run", action="store_true", help="render only, do not print")
    args = ap.parse_args()
    cmds = {"status": _status, "feed": _feed, "disconnect": _stop, "print": _print}
    asyncio.run(cmds[args.cmd](args))


if __name__ == "__main__":
    main()
