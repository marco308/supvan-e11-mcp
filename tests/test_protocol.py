import lzma

from supvan_e11_mcp import protocol as P
from supvan_e11_mcp import render


def test_command_frame_matches_rust_driver():
    f = P.make_cmd(P.NEXT_ZIPPEDBULK, 512, 3)
    assert f[:8] == bytes([0x7E, 0x5A, 0x0C, 0x00, 0x10, 0x01, 0xAA, 0x5C])
    assert f[12:16] == bytes([0x00, 0x02, 0x03, 0x00])
    assert f[8] | (f[9] << 8) == sum(f[10:16])
    assert P.make_cmd(P.CHECK_DEVICE)[8:10] == bytes([1, 0])


def test_lzma_header_and_roundtrip():
    data = bytes(range(256)) * 15
    z = P.compress_lzma(data)
    assert z[0] == 0x5D and z[1:5] == (8192).to_bytes(4, "little")
    assert z[5:13] == len(data).to_bytes(8, "little")
    unknown = z[:5] + b"\xff" * 8 + z[13:]
    assert lzma.decompress(unknown, format=lzma.FORMAT_ALONE) == data


def test_buffers_skip_margins_and_flag_page():
    img = render.render(render.LabelSpec(text="Hi", length_mm=40))
    raster, cols = render.to_raster(img)
    assert cols == 320 and len(raster) == 320 * 12
    bufs = P.split_into_buffers(raster, cols, density=4)
    assert len(bufs) == 1 and len(bufs[0]) == 4000
    b = bufs[0]
    assert b[2] == 0x0E and b[3] == 4 << 2  # page start+end+print end, nodu 4
    assert int.from_bytes(b[4:6], "little") == 318 and b[6] == 12
    assert b[12] == 4
    assert b[14 : 14 + 318 * 12] == raster[12 : 12 + 318 * 12]


def test_long_label_splits_and_density_only_on_first():
    raster = bytes(12 * 800)
    bufs = P.split_into_buffers(raster, 800, density=6)
    assert len(bufs) == 3
    assert [b[12] for b in bufs] == [6, 0, 0]
    assert [b[2] for b in bufs] == [0x02, 0x00, 0x0C]


def test_frame_reader_reassembles_split_notifications():
    frame = P.make_cmd(P.INQUIRY_STA)
    r = P.FrameReader()
    assert r.push(b"junk" + frame[:5]) == []
    assert r.push(frame[5:]) == [frame]
