"""Owns the printer connection: connects on demand, serialises operations,
retries once after a dropped link, and lets go after a quiet spell.

Shared by the Mac bridge app (daemon.py) and the homelab service (service.py).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os

from .printer import Locator, Printer, PrinterError

IDLE_DISCONNECT = 120  # seconds; frees the printer for the phone app and saves battery

log = logging.getLogger("bridge")


class Bridge:
    def __init__(self, locate: Locator | None = None) -> None:
        self.address = os.environ.get("SUPVAN_ADDRESS") or None
        self.locate = locate
        self.printer: Printer | None = None
        self.lock = asyncio.Lock()
        self.idle_task: asyncio.Task | None = None

    async def _printer(self) -> Printer:
        if self.printer and self.printer.client.is_connected:
            return self.printer
        self.printer = await Printer.connect(self.address, self.locate)
        self.address = self.printer.address  # stick to the one we found
        return self.printer

    async def _drop(self) -> None:
        if self.printer:
            await self.printer.close()
        self.printer = None

    def _arm_idle(self) -> None:
        if self.idle_task:
            self.idle_task.cancel()
        self.idle_task = asyncio.create_task(self._idle())

    async def _idle(self) -> None:
        await asyncio.sleep(IDLE_DISCONNECT)
        async with self.lock:
            if self.printer:
                log.info("idle, disconnecting")
                await self._drop()

    async def _run(self, op: str, kw: dict):
        p = await self._printer()
        if op == "status":
            return dataclasses.asdict(await p.info())
        if op == "feed":
            return await p.feed()
        if op == "print":
            return await p.print_raster(kw["raster"], kw["cols"], kw.get("density", 4))
        raise ValueError(f"unknown op {op!r}")

    async def run(self, op: str, **kw):
        """Run one printer operation. Raises PrinterError with a readable message."""
        async with self.lock:
            if op == "disconnect":
                await self._drop()
                return None
            for attempt in (1, 2):
                try:
                    result = await self._run(op, kw)
                    break
                except (PrinterError, ValueError):
                    raise
                except Exception as e:  # noqa: BLE001 - link drops surface as many types
                    log.exception("op %s failed (attempt %d)", op, attempt)
                    await self._drop()
                    # Never retry a print: the first attempt may have printed.
                    if attempt == 2 or op == "print":
                        raise PrinterError(f"{type(e).__name__}: {e}") from e
        self._arm_idle()
        return result

    async def status(self) -> dict:
        return await self.run("status")

    async def feed(self) -> None:
        await self.run("feed")

    async def print_raster(self, raster: bytes, cols: int, density: int = 4) -> None:
        await self.run("print", raster=raster, cols=cols, density=density)

    async def close(self) -> None:
        async with self.lock:
            await self._drop()
