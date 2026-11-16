"""Supvan wire protocol, E-series flavour (E10/E11/E12/E16).

Ported from the reverse-engineered Rust driver at
https://github.com/heeen/supvan-cups (MIT) and its E11 fork
https://github.com/efcroasdell/supvan-cups (crates/supvan-proto). Function
names follow the Rust source so the two can be read side by side.
"""

from __future__ import annotations

import lzma
import math
import struct
from dataclasses import dataclass

MAGIC1, MAGIC2 = 0x7E, 0x5A

# Command bytes (cmd.rs).
BUF_FULL = 0x10
INQUIRY_STA = 0x11
CHECK_DEVICE = 0x12
START_PRINT = 0x13
STOP_PRINT = 0x14
RD_DEV_NAME = 0x16
PAPER_SKIP = 0x2E
RETURN_MAT = 0x30
NEXT_ZIPPEDBULK = 0x5C
READ_FWVER = 0xC5
# E-series only, captured from the vendor app; meaning unknown (profile.rs).
PRE_START = (0xC9, 110)
POST_START = (0xBA, 29)

CMD_NAMES = {v: k for k, v in globals().items() if k.isupper() and isinstance(v, int) and k not in ("MAGIC1", "MAGIC2")}

# E-series geometry (profile.rs E_SERIES).
DOTS_PER_MM = 8
HEAD_DOTS = 96  # 12 mm printable band on 15 mm tape
BYTES_PER_LINE = HEAD_DOTS // 8
BUF_SIZE = 4000
BUF_HEADER = 14
MAX_BUF_DATA = BUF_SIZE - BUF_HEADER
MARGIN_DOTS = 1
NODU = 4
MAX_DENSITY = 19
DATA_PAYLOAD = 500
RESP_HEADER = 22


def _sum16(b: bytes | bytearray, start: int, end: int) -> int:
    return sum(b[start:end]) & 0xFFFF


def make_cmd(cmd: int, p1: int = 0, p2: int = 0) -> bytes:
    """16-byte command frame (make_cmd_start_trans)."""
    f = bytearray(16)
    f[0:8] = bytes([MAGIC1, MAGIC2, 0x0C, 0x00, 0x10, 0x01, 0xAA, cmd])
    f[11] = 0x01
    struct.pack_into("<HH", f, 12, p1 & 0xFFFF, p2 & 0xFFFF)
    struct.pack_into("<H", f, 8, _sum16(f, 10, 16))
    return bytes(f)


def build_data_frames(data: bytes) -> list[bytes]:
    """Wrap compressed data in 512-byte `AA BB` transfer frames (data.rs)."""
    count = math.ceil(len(data) / DATA_PAYLOAD)
    frames = []
    for i in range(count):
        f = bytearray(512)
        f[0:8] = bytes([MAGIC1, MAGIC2, 508 & 0xFF, 508 >> 8, 0x10, 0x02, 0xAA, 0xBB])
        f[10] = i
        f[11] = count
        chunk = data[i * DATA_PAYLOAD : (i + 1) * DATA_PAYLOAD]
        f[12 : 12 + len(chunk)] = chunk
        struct.pack_into("<H", f, 8, _sum16(f, 10, 512))
        frames.append(bytes(f))
    return frames


def build_print_buffer(
    image: bytes, cols: int, *, first: bool, last: bool, density: int
) -> bytes:
    """One 4000-byte E-series print buffer (build_print_buffer_profiled)."""
    buf = bytearray(BUF_SIZE)
    b0 = (0x02 if first else 0) | ((0x04 | 0x08) if last else 0)
    b1 = (NODU & 0x0F) << 2  # mat = 0, first_cut = 0
    buf[2], buf[3] = b0, b1
    struct.pack_into("<HB", buf, 4, cols, BYTES_PER_LINE)
    struct.pack_into("<HH", buf, 8, MARGIN_DOTS, MARGIN_DOTS)
    # Density rides in the red-trim byte, first buffer only.
    buf[12] = min(density, MAX_DENSITY) if first else 0
    data = image[:MAX_BUF_DATA]
    buf[BUF_HEADER : BUF_HEADER + len(data)] = data

    data_end = cols * BYTES_PER_LINE + BUF_HEADER
    chk = sum(buf[2:14])
    for i in range(1, data_end // 256 + 1):
        idx = i * 256 - 1
        if idx < len(buf):
            chk += buf[idx]
    struct.pack_into("<H", buf, 0, chk & 0xFFFF)
    return bytes(buf)


def split_into_buffers(raster: bytes, total_cols: int, density: int) -> list[bytes]:
    """Tile a column-major raster into print buffers, skipping the margin
    columns at each end (split_into_buffers_profiled)."""
    max_cols = MAX_BUF_DATA // BYTES_PER_LINE
    remaining = total_cols - 2 * MARGIN_DOTS
    col = 0
    out = []
    while remaining > 0:
        n = min(remaining, max_cols)
        start = (MARGIN_DOTS + col) * BYTES_PER_LINE
        out.append(
            build_print_buffer(
                raster[start : start + n * BYTES_PER_LINE],
                n,
                first=col == 0,
                last=remaining <= max_cols,
                density=density,
            )
        )
        col += n
        remaining -= n
    return out


_LZMA_FILTERS = [
    {"id": lzma.FILTER_LZMA1, "dict_size": 8192, "lc": 3, "lp": 0, "pb": 2, "nice_len": 128}
]


def compress_lzma(data: bytes) -> bytes:
    """LZMA "alone" stream with the firmware's 8 KiB dictionary and the real
    uncompressed size patched into the header (compress.rs)."""
    out = bytearray(lzma.compress(data, format=lzma.FORMAT_ALONE, filters=_LZMA_FILTERS))
    out[5:13] = struct.pack("<Q", len(data))
    return bytes(out)


def calc_speed(avg_compressed: float) -> int:
    for limit, speed in ((3000, 10), (2800, 15), (2500, 20), (2000, 25), (1500, 40), (1000, 45), (500, 55)):
        if avg_compressed > limit:
            return speed
    return 60


def raster_from_rows(rows: list[list[bool]]) -> tuple[bytes, int]:
    """Pack a head-width image into column-major, LSB-first bytes.

    `rows` is HEAD_DOTS rows (across the tape) by N columns (along the feed).
    Returns (raster, columns).
    """
    assert len(rows) == HEAD_DOTS
    cols = len(rows[0])
    out = bytearray(cols * BYTES_PER_LINE)
    for r, row in enumerate(rows):
        byte, bit = divmod(r, 8)
        mask = 1 << bit
        for c, on in enumerate(row):
            if on:
                out[c * BYTES_PER_LINE + byte] |= mask
    return bytes(out), cols


# --- Responses (status.rs) -------------------------------------------------


@dataclass
class Status:
    buf_full: bool
    label_rw_error: bool
    label_end: bool
    label_mode_error: bool
    ribbon_rw_error: bool
    ribbon_end: bool
    low_battery: bool
    busy: bool
    head_too_hot: bool
    cover_open: bool
    printing: bool
    no_label: bool
    print_count: int
    regs: tuple[int, int, int, int]

    def error(self) -> str | None:
        # ribbon_end is asserted during normal E-series prints, so not fatal.
        errs = [
            (self.cover_open, "the cover is open"),
            (self.no_label, "no tape is loaded"),
            (self.label_end, "the tape has run out"),
            (self.label_rw_error, "the tape could not be read"),
            (self.label_mode_error, "the tape type does not match"),
            (self.ribbon_rw_error, "ribbon read error"),
            (self.head_too_hot, "the print head is too hot"),
        ]
        msgs = [m for on, m in errs if on]
        return ", ".join(msgs) if msgs else None

    def summary(self) -> str:
        flags = [k for k, v in self.__dict__.items() if v is True]
        return f"[{','.join(flags) or '-'}] count={self.print_count} regs={' '.join(f'{r:02x}' for r in self.regs)}"


def _has_header(d: bytes, min_len: int, cmd: int) -> bool:
    return len(d) >= min_len and d[0] == MAGIC1 and d[1] == MAGIC2 and d[7] == cmd


def parse_status(d: bytes) -> Status | None:
    if not _has_header(d, 20, INQUIRY_STA):
        return None
    b0, b1, b2, b3 = d[14:18]
    return Status(
        buf_full=bool(b0 & 0x01),
        label_rw_error=bool(b0 & 0x02),
        label_end=bool(b0 & 0x04),
        label_mode_error=bool(b0 & 0x08),
        ribbon_rw_error=bool(b0 & 0x10),
        ribbon_end=bool(b0 & 0x20),
        low_battery=bool(b0 & 0x40),
        busy=bool(b1 & 0x04),
        head_too_hot=bool(b1 & 0x08),
        cover_open=bool(b2 & 0x08),
        printing=bool(b2 & 0x40),
        no_label=bool(b3 & 0x01),
        print_count=d[18] | (d[19] << 8),
        regs=(b0, b1, b2, b3),
    )


@dataclass
class Material:
    uid: str
    label_type: int
    width_mm: int
    length_mm: int
    gap_mm: int
    remaining: int | None


def parse_material(d: bytes) -> Material | None:
    if not _has_header(d, RESP_HEADER + 21, RETURN_MAT):
        return None
    p = d[RESP_HEADER:]
    return Material(
        uid=p[0:7].hex().upper(),
        label_type=p[17],
        width_mm=p[18],
        length_mm=p[19],
        gap_mm=p[20],
        remaining=struct.unpack_from("<I", p, 21)[0] if len(p) >= 25 else None,
    )


def parse_device_name(d: bytes) -> str | None:
    if not _has_header(d, RESP_HEADER + 1, RD_DEV_NAME):
        return None
    n = (d[2] | (d[3] << 8)) - 18
    if n <= 0:
        return None
    return d[RESP_HEADER : RESP_HEADER + n].rstrip(b"\0").decode("ascii", "replace") or None


def parse_firmware(d: bytes) -> int | None:
    return d[RESP_HEADER] if _has_header(d, RESP_HEADER + 1, READ_FWVER) else None


class FrameReader:
    """Cut a byte stream of notifications into `7E 5A` frames."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def push(self, chunk: bytes) -> list[bytes]:
        self.buf += chunk
        frames = []
        while True:
            start = self.buf.find(bytes([MAGIC1, MAGIC2]))
            if start < 0:
                self.buf = self.buf[-1:] if self.buf[-1:] == bytes([MAGIC1]) else bytearray()
                break
            del self.buf[:start]
            if len(self.buf) < 4:
                break
            total = (self.buf[2] | (self.buf[3] << 8)) + 4
            if not 8 <= total <= 1024:
                del self.buf[:1]
                continue
            if len(self.buf) < total:
                break
            frames.append(bytes(self.buf[:total]))
            del self.buf[:total]
        return frames

    def flush(self) -> bytes | None:
        partial = bytes(self.buf) if len(self.buf) >= 8 and self.buf[:2] == bytes([MAGIC1, MAGIC2]) else None
        self.buf.clear()
        return partial
