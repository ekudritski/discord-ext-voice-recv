import hashlib
import io
import random
import struct

import pytest

from discord.ext.voice_recv.ogg import OggOpusWriter, _ogg_crc, opus_packet_samples
from discord.ext.voice_recv.rtp import OPUS_SILENCE

# sha256 of the same stream written by libogg 1.3.6 (via PyOgg), serial 0x1234ABCD
LIBOGG_SHA256 = "51fb53b0f0947de15646037a9d6f1218278f6754fbc6d598d5d75e19c2720d0a"


def sample_packets():
    rng = random.Random(7)
    return [bytes([0xFC]) + rng.randbytes(rng.choice([0, 100, 254, 255, 600, 3000])) for _ in range(400)]


def read_pages(data):
    pages, pos = [], 0
    while pos < len(data):
        assert data[pos : pos + 4] == b"OggS"
        n = data[pos + 26]
        lacing = data[pos + 27 : pos + 27 + n]
        end = pos + 27 + n + sum(lacing)
        page = bytearray(data[pos:end])
        crc = struct.unpack_from("<I", page, 22)[0]
        page[22:26] = b"\0\0\0\0"
        assert _ogg_crc(bytes(page)) == crc
        flags, granule, serial, seq = struct.unpack_from("<BqII", data, pos + 5)
        pages.append(dict(flags=flags, granule=granule, serial=serial, seq=seq, lacing=lacing, body=data[pos + 27 + n : end]))
        pos = end
    return pages


def read_packets(pages):
    packets, current = [], b""
    for page in pages:
        offset = 0
        for value in page["lacing"]:
            current += page["body"][offset : offset + value]
            offset += value
            if value < 255:
                packets.append(current)
                current = b""
    return packets


def test_crc_check_value():
    # CRC-32/POSIX without the final xor: poly 0x04C11DB7, init 0, not reflected
    assert _ogg_crc(b"123456789") == 0x89A1897F


def test_matches_libogg():
    out = io.BytesIO()
    w = OggOpusWriter(out, serial=0x1234ABCD)
    for p in sample_packets():
        w.write(p)
    # the libogg reference was written with 4 + 1 silence packets at the end
    for _ in range(5):
        w.write(OPUS_SILENCE)
    w.close()
    assert hashlib.sha256(out.getvalue()).hexdigest() == LIBOGG_SHA256


def test_stream_structure():
    packets = sample_packets()
    out = io.BytesIO()
    with OggOpusWriter(out, pre_skip=312, serial=5) as w:
        for p in packets:
            w.write(p)
    pages = read_pages(out.getvalue())

    assert [p["seq"] for p in pages] == list(range(len(pages)))
    assert {p["serial"] for p in pages} == {5}
    assert pages[0]["flags"] == 0x02 and pages[-1]["flags"] & 0x04
    assert not any(p["flags"] & 0x04 for p in pages[:-1])
    assert pages[-1]["granule"] == 960 * len(packets) == w.granule

    head, tags, *audio = read_packets(pages)
    assert head == b"OpusHead" + struct.pack("<BBHIhB", 1, 2, 312, 0, 0, 0)
    assert tags.startswith(b"OpusTags")
    assert audio == packets
    # headers sit alone on their own pages
    assert len(read_packets(pages[:1])) == 1 and len(read_packets(pages[:2])) == 2


def test_close_without_packets():
    out = io.BytesIO()
    OggOpusWriter(out).close()
    pages = read_pages(out.getvalue())
    assert pages[-1]["flags"] & 0x04 and pages[-1]["granule"] == 0
    assert len(read_packets(pages)) == 2


def test_write_after_close():
    w = OggOpusWriter(io.BytesIO())
    w.close()
    with pytest.raises(ValueError):
        w.write(OPUS_SILENCE)


def test_path_is_opened_and_closed(tmp_path):
    path = tmp_path / "a.ogg"
    w = OggOpusWriter(path)
    w.write(OPUS_SILENCE)
    w.close()
    assert read_packets(read_pages(path.read_bytes()))[2] == OPUS_SILENCE


@pytest.mark.parametrize(
    "packet, samples",
    [
        (OPUS_SILENCE, 960),  # CELT 20 ms
        (bytes([0x78]), 960),  # SILK WB 20 ms
        (bytes([0x18]), 2880),  # SILK NB 60 ms
        (bytes([0x60]), 480),  # hybrid 10 ms
        (bytes([0xE1]), 240),  # CELT 2.5 ms x2
        (bytes([0xFB, 0x03]), 2880),  # CELT 20 ms, code 3, 3 frames
    ],
)
def test_opus_packet_samples(packet, samples):
    assert opus_packet_samples(packet) == samples


def test_opus_packet_samples_empty():
    with pytest.raises(ValueError):
        opus_packet_samples(b"")
