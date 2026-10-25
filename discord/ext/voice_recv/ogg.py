# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import random
import struct

from typing import TYPE_CHECKING

from .rtp import OPUS_SILENCE

if TYPE_CHECKING:
    from typing import BinaryIO, Final, List, Optional, Union

__all__ = [
    'OggOpusWriter',
    'opus_packet_samples',
]

VENDOR: Final = b'discord-ext-voice-recv'
# libogg ends a page after this many body bytes, once it holds at least 4 whole packets
PAGE_FILL: Final = 4096


def _crc_table() -> List[int]:
    table = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) if r & 0x80000000 else (r << 1)
        table.append(r & 0xFFFFFFFF)
    return table


_CRC_TABLE: Final = _crc_table()


def _ogg_crc(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[(crc >> 24) ^ b]
    return crc


def opus_packet_samples(packet: bytes) -> int:
    """Number of 48 kHz samples in an Opus packet, from its TOC byte (RFC 6716 3.1)."""
    if not packet:
        raise ValueError('empty Opus packet')
    config = packet[0] >> 3
    if config < 12:
        frame = (480, 960, 1920, 2880)[config % 4]
    elif config < 16:
        frame = (480, 960)[config % 2]
    else:
        frame = (120, 240, 480, 960)[config % 4]
    code = packet[0] & 3
    if code == 0:
        frames = 1
    elif code < 3:
        frames = 2
    else:
        if len(packet) < 2:
            raise ValueError('truncated Opus packet')
        frames = packet[1] & 0x3F
    return frame * frames


class OggOpusWriter:
    """Writes Opus packets into an Ogg Opus file (RFC 7845) as they are, without re-encoding.

    Pure Python, no libogg. Page layout follows libogg, so the output matches a libogg based writer.
    The granule position advances by each packet's own duration. An empty packet (a lost one,
    :class:`FakePacket`) is written as 20 ms of silence. Pauses, where Discord sends nothing, are the
    caller's business, e.g. writing ``rtp.OPUS_SILENCE`` packets in their place.

    Parameters
    ----------
    file
        A path or a binary file object. A path is opened here and closed by :meth:`close`.
    channels
        Channel count in the header. Discord sends stereo.
    pre_skip
        Samples the player drops from the start (``OpusHead`` pre-skip).
    serial
        Ogg stream serial number, random if not given.
    """

    def __init__(
        self,
        file: Union[str, os.PathLike, BinaryIO],
        *,
        channels: int = 2,
        pre_skip: int = 0,
        serial: Optional[int] = None,
    ):
        if isinstance(file, (str, os.PathLike)):
            self._file = open(file, 'wb')
            self._owns_file = True
        else:
            self._file = file
            self._owns_file = False

        self._serial = random.getrandbits(32) if serial is None else serial & 0xFFFFFFFF
        self._page_no = 0
        self._granule = 0
        self._first_page = True
        self._eos = False
        self._closed = False
        # one entry per lacing value: (value, starts_packet, granule of the packet it ends or None)
        self._lacing: list = []
        self._body = bytearray()

        head = b'OpusHead' + struct.pack('<BBHIhB', 1, channels, pre_skip, 0, 0, 0)
        tags = b'OpusTags' + struct.pack('<I', len(VENDOR)) + VENDOR + struct.pack('<I', 0)
        self._packet_in(head, 0)
        self._packet_in(tags, 0)
        while self._page_out(True):
            pass

    @property
    def granule(self) -> int:
        """Samples written so far."""
        return self._granule

    def write(self, packet: Union[bytes, bytearray, memoryview]) -> None:
        if self._closed:
            raise ValueError('write to a closed OggOpusWriter')
        # a lost packet comes as FakePacket with empty data; an empty Ogg packet makes ffmpeg stop reading the file
        packet = bytes(packet) or OPUS_SILENCE
        self._granule += opus_packet_samples(packet)
        self._packet_in(packet, self._granule)
        while self._page_out(self._first_page):
            pass

    def close(self) -> None:
        """Ends the stream (last page gets the end-of-stream flag) and closes a file opened here."""
        if self._closed:
            return
        self._closed = True
        self._eos = True
        if not self._lacing:
            self._write_page(0, self._granule)
        while self._page_out(True):
            pass
        if self._owns_file:
            self._file.close()
        else:
            self._file.flush()

    def __enter__(self) -> OggOpusWriter:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _packet_in(self, packet: bytes, granule: int) -> None:
        n = len(packet) // 255 + 1
        for i in range(n):
            last = i == n - 1
            self._lacing.append((len(packet) % 255 if last else 255, i == 0, granule if last else None))
        self._body += packet


    def _page_out(self, force: bool) -> bool:
        # port of libogg 1.3's ogg_stream_pageout() / ogg_stream_flush_i()
        maxvals = min(len(self._lacing), 255)
        if maxvals == 0:
            return False

        granule = -1
        if self._first_page:
            granule = 0
            vals = maxvals
            for i in range(maxvals):
                if self._lacing[i][0] < 255:
                    vals = i + 1
                    break
        else:
            acc = 0
            packets_done = 0
            packet_just_done = 0
            vals = 0
            while vals < maxvals:
                if acc > PAGE_FILL and packet_just_done >= 4:
                    force = True
                    break
                value, _, packet_granule = self._lacing[vals]
                acc += value
                if packet_granule is not None:
                    granule = packet_granule
                    packets_done += 1
                    packet_just_done = packets_done
                else:
                    packet_just_done = 0
                vals += 1
            if vals == 255:
                force = True

        if not force:
            return False
        self._write_page(vals, granule)
        return True

    def _write_page(self, vals: int, granule: int) -> None:
        lacing = self._lacing[:vals]
        size = sum(v for v, _, _ in lacing)
        flags = 0
        if lacing and not lacing[0][1]:
            flags |= 0x01
        if self._first_page:
            flags |= 0x02
        if self._eos and vals == len(self._lacing):
            flags |= 0x04

        header = bytearray(b'OggS')
        header += struct.pack('<BBqIIIB', 0, flags, granule, self._serial, self._page_no, 0, vals)
        header += bytes(v for v, _, _ in lacing)
        body = self._body[:size]
        struct.pack_into('<I', header, 22, _ogg_crc(bytes(header) + body))
        self._file.write(header)
        self._file.write(body)

        del self._lacing[:vals]
        del self._body[:size]
        self._page_no += 1
        self._first_page = False
