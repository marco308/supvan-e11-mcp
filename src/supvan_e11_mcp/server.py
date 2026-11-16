"""Local (stdio) MCP server for the Mac: renders here and prints through the
Bluetooth bridge app (daemon.py), which owns the printer connection."""

from __future__ import annotations

from . import client
from .tools import Labels, build_server


class MacBridgeBackend:
    async def status(self) -> dict:
        return await client.status()

    async def feed(self) -> None:
        await client.feed()

    async def print_raster(self, raster: bytes, cols: int, density: int = 4) -> None:
        await client.print_raster(raster, cols, density)

    async def release(self) -> None:
        await client.call("disconnect")


mcp = build_server(Labels(MacBridgeBackend()))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
