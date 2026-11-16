"""Bluetooth Low Energy link and print flow for the E11 (printer.rs, ble.rs)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

# Looked up as bleak.X at call time, not imported by name: on the homelab,
# habluetooth swaps these for wrappers that route through the best adapter or
# ESPHome proxy (hub.py).
import bleak
from bleak.backends.device import BLEDevice

from . import protocol as P

log = logging.getLogger(__name__)


def _sig(short: int) -> str:
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


# The three GATT layouts the vendor app probes for; the E11 uses the first.
GATT_LAYOUTS = [
    (_sig(0xFEE7), _sig(0xFEC1), _sig(0xFEC1)),
    ("0000e0ff-3c17-d293-8e48-14fe2e4da212", _sig(0xFFE1), _sig(0xFFE9)),
    (_sig(0xFF00), _sig(0xFF01), _sig(0xFF02)),
]
NAME_PREFIXES = ("T0", "E1", "SUPVAN", "KATASYMBOL")
CHUNK = 128
CHUNK_DELAY = 0.01
RESPONSE_TIMEOUT = 4.0
SETTLE = 0.1


class PrinterError(Exception):
    pass


def looks_like_printer(name: str | None, service_uuids: list[str]) -> bool:
    if any(s.lower() in (l[0] for l in GATT_LAYOUTS) for s in service_uuids):
        return True
    return bool(name) and name.upper().startswith(NAME_PREFIXES)


Locator = Callable[[str | None], Awaitable[BLEDevice | None]]


async def scan(timeout: float = 6.0) -> list[tuple[BLEDevice, int]]:
    """Return (device, rssi) for every advertiser that looks like a Supvan printer."""
    found = await bleak.BleakScanner.discover(timeout=timeout, return_adv=True)
    out = []
    for dev, adv in found.values():
        if looks_like_printer(adv.local_name or dev.name, adv.service_uuids):
            out.append((dev, adv.rssi))
    return sorted(out, key=lambda x: -x[1])


async def _establish(device: BLEDevice):
    """Connect with retries. bleak-retry-connector (homelab) handles flaky
    links and proxy slots properly; plain bleak retries are the Mac fallback."""
    try:
        from bleak_retry_connector import establish_connection
    except ImportError:
        last: Exception | None = None
        for attempt in range(3):
            client = bleak.BleakClient(device)
            try:
                await client.connect(timeout=15)
                return client
            except Exception as e:  # noqa: BLE001
                last = e
                log.warning("connect attempt %d failed: %s", attempt + 1, e)
                await asyncio.sleep(0.5)
        raise last  # type: ignore[misc]
    return await establish_connection(bleak.BleakClient, device, device.name or device.address, max_attempts=4)


async def scan_locate(address: str | None, timeout: float = 6.0) -> BLEDevice | None:
    """Find the printer with a plain bleak scan (the Mac bridge)."""
    if address:
        return await bleak.BleakScanner.find_device_by_address(address, timeout=timeout)
    found = await scan(timeout)
    return found[0][0] if found else None


@dataclass
class Info:
    address: str
    advertised_name: str | None
    device_name: str | None
    firmware: int | None
    material: P.Material | None
    status: P.Status | None


class Printer:
    def __init__(self, client: bleak.BleakClient, write_char, notify_char, advertised_name: str | None):
        self.client = client
        self.write_char = write_char
        self.notify_char = notify_char
        self.advertised_name = advertised_name
        self.address = ""
        self.reader = P.FrameReader()
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.with_response = "write-without-response" not in write_char.properties
        self.lock = asyncio.Lock()

    # --- connection ---------------------------------------------------------

    @classmethod
    async def connect(cls, address: str | None = None, locate: Locator | None = None) -> "Printer":
        device = await (locate or scan_locate)(address)
        if device is None:
            raise PrinterError(
                "Could not find the label printer over Bluetooth. Is it switched on, and is "
                "the phone app disconnected from it?"
            )
        try:
            client = await _establish(device)
        except Exception as e:  # noqa: BLE001 - bleak raises a variety of types
            raise PrinterError(f"Could not connect to the printer: {e or type(e).__name__}") from e

        for service_uuid, notify_uuid, write_uuid in GATT_LAYOUTS:
            service = client.services.get_service(service_uuid)
            if not service:
                continue
            notify = service.get_characteristic(notify_uuid)
            write = service.get_characteristic(write_uuid)
            if notify and write:
                p = cls(client, write, notify, device.name)
                p.address = device.address
                await client.start_notify(notify, p._on_notify)
                log.info(
                    "connected to %s (%s), service %s, write %s response",
                    device.name, device.address, service_uuid, "with" if p.with_response else "without",
                )
                return p
        await client.disconnect()
        raise PrinterError("The device does not offer a known Supvan Bluetooth service.")

    async def close(self) -> None:
        try:
            await self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    async def __aenter__(self) -> "Printer":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # --- framing ------------------------------------------------------------

    def _on_notify(self, _char, data: bytearray) -> None:
        for frame in self.reader.push(bytes(data)):
            self.queue.put_nowait(frame)

    async def _write(self, frame: bytes) -> None:
        for off in range(0, len(frame), CHUNK):
            if off:
                await asyncio.sleep(CHUNK_DELAY)
            await self.client.write_gatt_char(
                self.write_char, frame[off : off + CHUNK], response=self.with_response
            )

    async def command(self, cmd: int, p1: int = 0, p2: int = 0, quiet: bool = False) -> bytes | None:
        """Send a command and wait for the reply echoing its command byte."""
        while not self.queue.empty():
            stale = self.queue.get_nowait()
            log.debug("dropping stale frame %s", stale[:24].hex(" "))
        if not quiet:
            log.debug("TX %s(%d, %d)", P.CMD_NAMES.get(cmd, hex(cmd)), p1, p2)
        await self._write(P.make_cmd(cmd, p1, p2))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RESPONSE_TIMEOUT
        while (left := deadline - loop.time()) > 0:
            try:
                frame = await asyncio.wait_for(self.queue.get(), left)
            except TimeoutError:
                break
            if len(frame) > 7 and frame[7] == cmd:
                if not quiet:
                    log.debug("RX %s: %s", P.CMD_NAMES.get(cmd, hex(cmd)), frame[:48].hex(" "))
                return frame
            log.debug("RX (other): %s", frame[:48].hex(" "))
        partial = self.reader.flush()
        if partial and len(partial) > 7 and partial[7] == cmd:
            return partial
        log.debug("RX %s: no response", P.CMD_NAMES.get(cmd, hex(cmd)))
        return None

    async def status(self) -> P.Status | None:
        r = await self.command(P.INQUIRY_STA, quiet=True)
        return P.parse_status(r) if r else None

    # --- operations ---------------------------------------------------------

    async def info(self) -> Info:
        async with self.lock:
            if not await self.command(P.CHECK_DEVICE):
                raise PrinterError("The printer did not answer. Is it switched on?")
            name = await self.command(P.RD_DEV_NAME)
            fw = await self.command(P.READ_FWVER)
            mat = await self.command(P.RETURN_MAT)
            st = await self.status()
            if mat:
                log.debug("material raw: %s", mat.hex(" "))
            return Info(
                address=self.address,
                advertised_name=self.advertised_name,
                device_name=P.parse_device_name(name) if name else None,
                firmware=P.parse_firmware(fw) if fw else None,
                material=P.parse_material(mat) if mat else None,
                status=st,
            )

    async def feed(self) -> None:
        async with self.lock:
            if not await self.command(P.PAPER_SKIP):
                raise PrinterError("The printer did not answer.")

    async def _poll(self, attempts: int, interval: float, until) -> P.Status | None:
        last = ""
        for _ in range(attempts):
            s = await self.status()
            if s:
                if (d := s.summary()) != last:
                    log.debug("status %s", d)
                    last = d
                if until(s):
                    return s
            await asyncio.sleep(interval)
        return None

    @staticmethod
    def _raise_on_error(s: P.Status) -> None:
        if err := s.error():
            raise PrinterError(f"The printer reports: {err}.")

    async def print_raster(self, raster: bytes, cols: int, density: int = 4) -> None:
        """Print one label from a column-major raster (see protocol.raster_from_rows)."""
        buffers = P.split_into_buffers(raster, cols, density)
        blocks = [P.compress_lzma(b) for b in buffers]
        speed = 20 if len(blocks) > 1 else P.calc_speed(sum(map(len, blocks)) / len(blocks))
        log.info("print: %d columns, %d buffers, blocks %s, speed %d", cols, len(buffers), [len(b) for b in blocks], speed)

        async with self.lock:
            if not await self.command(P.CHECK_DEVICE):
                raise PrinterError("The printer did not answer. Is it switched on?")
            ready = await self._poll(60, 0.1, lambda s: not s.busy and not s.printing)
            if not ready:
                raise PrinterError("The printer stayed busy.")
            self._raise_on_error(ready)

            await self.command(*P.PRE_START)
            await self.command(P.START_PRINT)
            started = await self._poll(60, 0.1, lambda s: s.printing or s.error() is not None)
            if not started:
                raise PrinterError("The printer never started printing.")
            self._raise_on_error(started)
            await self.command(*P.POST_START)

            for i, block in enumerate(blocks):
                room = await self._poll(200, 0.02, lambda s: not s.buf_full or s.error() is not None)
                if not room:
                    raise PrinterError(f"Timed out waiting for buffer space (block {i + 1}).")
                if room.error():
                    await self.command(P.STOP_PRINT)
                    self._raise_on_error(room)
                frames = P.build_data_frames(block)
                if not await self.command(P.NEXT_ZIPPEDBULK, 512, len(frames)):
                    raise PrinterError("The printer did not accept the image data.")
                for f in frames:
                    await self._write(f)
                await asyncio.sleep(SETTLE)
                await self.command(P.BUF_FULL, 0, 0)  # E-series sends (0, 0)
                await asyncio.sleep(SETTLE)

            done = await self._poll(
                300, 0.1, lambda s: (not s.printing and not s.busy and not s.buf_full) or s.error() is not None
            )
            if not done:
                raise PrinterError("The printer took the label but did not finish printing it.")
            self._raise_on_error(done)
            log.info("print complete: %s", done.summary())
