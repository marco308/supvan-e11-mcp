"""Mac Bluetooth bridge daemon.

Runs inside "Label Printer Bridge.app" (scripts/make_app.sh) so macOS lets it
use Bluetooth. Answers newline-delimited JSON requests on a Unix socket; the
local MCP server and CLI are clients (client.py).

Request:  {"op": "status" | "feed" | "print" | "disconnect", ...}
          print takes {"raster": base64, "cols": int, "density": int}
Response: {"ok": true, ...} or {"ok": false, "error": "..."}
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from pathlib import Path

from .bridge import Bridge
from .printer import PrinterError

SOCKET = Path.home() / "Library/Application Support/supvan-e11/bridge.sock"

log = logging.getLogger("bridge")


async def handle(bridge: Bridge, req: dict) -> dict:
    op = req.get("op", "")
    try:
        if op == "print":
            await bridge.print_raster(base64.b64decode(req["raster"]), int(req["cols"]), int(req.get("density", 4)))
            return {"ok": True}
        result = await bridge.run(op)
        return {"ok": True, "info": result} if op == "status" else {"ok": True}
    except (PrinterError, ValueError, KeyError) as e:
        return {"ok": False, "error": str(e)}


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if os.environ.get("SUPVAN_DEBUG"):
        logging.getLogger("supvan_e11_mcp").setLevel(logging.DEBUG)
    bridge = Bridge()

    async def serve_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                try:
                    resp = await handle(bridge, json.loads(line))
                except json.JSONDecodeError as e:
                    resp = {"ok": False, "error": f"bad request: {e}"}
                writer.write(json.dumps(resp).encode() + b"\n")
                await writer.drain()
        finally:
            writer.close()

    SOCKET.parent.mkdir(parents=True, exist_ok=True)
    SOCKET.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(serve_client, path=str(SOCKET))
    os.chmod(SOCKET, 0o600)
    log.info("listening on %s", SOCKET)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
