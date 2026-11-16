"""Client for the Bluetooth bridge daemon; starts the bridge app if needed."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
from pathlib import Path

from .daemon import SOCKET

APP = Path(os.environ.get("SUPVAN_BRIDGE_APP", Path.home() / "Applications/Label Printer Bridge.app"))
# A print polls for up to ~30 s after sending, plus connecting and scanning.
TIMEOUT = 90


class BridgeError(Exception):
    pass


async def _open() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    try:
        return await asyncio.open_unix_connection(str(SOCKET))
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    if not APP.exists():
        raise BridgeError(f"The bridge app is missing; build it with scripts/make_app.sh ({APP}).")
    env = ["--env", "SUPVAN_DEBUG=1"] if os.environ.get("SUPVAN_DEBUG") else []
    subprocess.run(["open", "-g", *env, str(APP)], check=True)
    for _ in range(50):
        await asyncio.sleep(0.2)
        try:
            return await asyncio.open_unix_connection(str(SOCKET))
        except (FileNotFoundError, ConnectionRefusedError):
            continue
    raise BridgeError("The bridge app did not start; see ~/Library/Logs/label-printer-bridge.log.")


async def call(op: str, **kw) -> dict:
    reader, writer = await _open()
    try:
        writer.write(json.dumps({"op": op, **kw}).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), TIMEOUT)
    finally:
        writer.close()
    if not line:
        raise BridgeError("The bridge closed the connection; see ~/Library/Logs/label-printer-bridge.log.")
    resp = json.loads(line)
    if not resp.get("ok"):
        raise BridgeError(resp.get("error", "unknown error"))
    return resp


async def status() -> dict:
    return (await call("status"))["info"]


async def feed() -> None:
    await call("feed")


async def print_raster(raster: bytes, cols: int, density: int) -> None:
    await call("print", raster=base64.b64encode(raster).decode(), cols=cols, density=density)
