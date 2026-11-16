"""Bluetooth on the homelab: local adapters plus ESPHome Bluetooth proxies.

habluetooth (the library behind Home Assistant's Bluetooth) pools every radio
we register and, once installed, makes bleak.BleakClient connect through
whichever one hears the printer best with a free connection slot. So the
printer can sit anywhere a proxy reaches, and adding a room is config only.

Config, from the environment:
  SUPVAN_LOCAL_ADAPTERS  "1" (default) to use the host's own adapters, "0" not to
  SUPVAN_PROXIES         comma-separated ESPHome proxies, each "host" or
                         "host|noise_psk" (the device's api encryption key)
"""

from __future__ import annotations

import asyncio
import logging
import os

import habluetooth
from bleak.backends.device import BLEDevice
from bluetooth_data_tools import monotonic_time_coarse
from habluetooth import BluetoothScanningMode

from .printer import looks_like_printer

log = logging.getLogger(__name__)

LOCATE_TIMEOUT = 15.0
# Only connect after a recent advert: an old sighting usually means the
# printer has switched itself off or is out of range.
FRESH_SECONDS = 20.0


class _Manager(habluetooth.BluetoothManager):
    def _discover_service_info(self, service_info) -> None:
        """Advert callback; we read the manager's history on demand instead."""


class Hub:
    def __init__(self) -> None:
        self.manager: habluetooth.BluetoothManager | None = None
        self.proxies: list = []
        self.local_scanners: list = []
        self._cancel: list = []

    async def start(self) -> None:
        self.manager = _Manager()
        await self.manager.async_setup()  # also swaps bleak's classes for habluetooth's wrappers

        if os.environ.get("SUPVAN_LOCAL_ADAPTERS", "1") != "0":
            adapters = await self.manager.async_get_bluetooth_adapters()
            for name, details in adapters.items():
                try:
                    scanner = habluetooth.create_local_scanner(
                        BluetoothScanningMode.ACTIVE, name, details["address"]
                    )
                    self._cancel.append(scanner.async_setup())
                    self._cancel.append(self.manager.async_register_scanner(scanner))
                    await scanner.async_start()
                    self.local_scanners.append(scanner)
                    log.info("local adapter %s (%s) scanning", name, details["address"])
                except Exception:  # noqa: BLE001 - keep going with whatever works
                    log.exception("could not start local adapter %s", name)

        proxies = [p.strip() for p in os.environ.get("SUPVAN_PROXIES", "").split(",") if p.strip()]
        if proxies:
            from bleak_esphome import APIConnectionManager

            for entry in proxies:
                host, _, psk = entry.partition("|")
                conn = APIConnectionManager({"address": host, "noise_psk": psk or None})
                self.proxies.append(conn)
                # start() returns once connected; the manager keeps reconnecting after.
                asyncio.create_task(self._start_proxy(conn, host))

        if not self.local_scanners and not proxies:
            log.warning("no Bluetooth radios configured; nothing can reach the printer")

    async def _start_proxy(self, conn, host: str) -> None:
        try:
            await conn.start()
            log.info("ESPHome proxy %s connected", host)
        except Exception:  # noqa: BLE001
            log.exception("ESPHome proxy %s failed to start", host)

    async def stop(self) -> None:
        for conn in self.proxies:
            await conn.stop()
        for scanner in self.local_scanners:
            await scanner.async_stop()
        for cancel in reversed(self._cancel):
            cancel()

    def radios(self) -> list[dict]:
        """What can currently hear what, for the status endpoint."""
        if not self.manager:
            return []
        out = []
        for info in self.manager.async_discovered_service_info(connectable=True):
            if looks_like_printer(info.name, info.service_uuids):
                out.append({
                    "address": info.address,
                    "name": info.name,
                    "rssi": info.rssi,
                    "via": info.source,
                    "seconds_ago": round(monotonic_time_coarse() - info.time, 1),
                })
        return out

    async def locate(self, address: str | None) -> BLEDevice | None:
        """Wait for the printer to show up on any radio; return the best sighting."""
        assert self.manager
        loop = asyncio.get_running_loop()
        deadline = loop.time() + LOCATE_TIMEOUT
        while True:
            now = monotonic_time_coarse()
            seen = [
                i
                for i in self.manager.async_discovered_service_info(connectable=True)
                if now - i.time < FRESH_SECONDS
                and (i.address == address if address else looks_like_printer(i.name, i.service_uuids))
            ]
            if seen:
                best = max(seen, key=lambda i: i.rssi)
                log.info("printer %s %s seen via %s rssi %d", best.name, best.address, best.source, best.rssi)
                return best.device
            if loop.time() > deadline:
                return None
            await asyncio.sleep(0.5)
