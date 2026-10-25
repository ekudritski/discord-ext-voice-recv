"""
Jitter buffer behaviour as seen through PacketDecoder, driven the same way PacketRouter does.

Packets are given as unwrapped numbers n: sequence = n % 65536, timestamp = n * 960,
so expectations stay readable across the sequence rollover.
"""

import struct

import pytest

from discord.ext.voice_recv import rtp
from discord.ext.voice_recv.opus import PacketDecoder
from discord.ext.voice_recv.utils import MultiDataEvent

SSRC = 1234
SAMPLES = 960


def make_packet(n):
    payload = b"\x78\x00\x01"
    header = struct.pack(">BBHII", 0x80, 0x78, n % 65536, (n * SAMPLES) % 2**32, SSRC)
    packet = rtp.decode_rtp(header + payload)
    packet.decrypted_data = payload
    return packet


class StubVoiceClient:
    def _get_id_from_ssrc(self, ssrc):
        return None


class StubSink:
    voice_client = StubVoiceClient()

    def wants_opus(self):
        return True


class StubRouter:
    def __init__(self):
        self.sink = StubSink()
        self.waiter = MultiDataEvent()


class Harness:
    def __init__(self):
        self.router = StubRouter()
        self.decoder = PacketDecoder(self.router, SSRC)
        self.out = []

    def push(self, numbers, drain=True):
        for n in numbers:
            self.decoder.push_packet(make_packet(n))
            if drain:
                self.drain()

    def drain(self):
        # same as PacketRouter._do_run: loop while any decoder is registered as ready
        for _ in range(10_000):
            items = self.router.waiter.items
            if not items:
                return
            for decoder in items:
                data = decoder.pop_data()
                if data is not None:
                    self.out.append(data.packet.timestamp // SAMPLES)
        raise AssertionError("router would busy-loop: decoder stays ready but yields nothing")

    def remaining(self):
        return sorted(p.timestamp // SAMPLES for p in self.decoder._buffer._buffer)

    def everything(self):
        return self.out + self.remaining()


def test_sequential():
    h = Harness()
    h.push(range(50))
    assert h.everything() == list(range(50))


def test_one_packet_lost_keeps_the_rest():
    # the original bug: one missing packet flushed the whole buffer
    h = Harness()
    sent = [n for n in range(40) if n != 10]
    h.push(sent)
    assert h.everything() == sent


def test_several_gaps():
    h = Harness()
    sent = [n for n in range(60) if n not in (5, 6, 20, 33, 34, 35)]
    h.push(sent)
    assert h.everything() == sent


def test_two_packets_swapped():
    h = Harness()
    h.push([*range(10), 11, 10, *range(12, 20)])
    assert h.everything() == list(range(20))


@pytest.mark.xfail(reason="reordering is only tolerated 1 packet deep: gap is skipped at once (fix 2: window)")
def test_reorder_three_deep():
    h = Harness()
    h.push([*range(10), 12, 13, 10, 11, *range(14, 20)])
    assert h.everything() == list(range(20))


def test_very_late_packet_dropped_rest_intact():
    h = Harness()
    h.push(range(30))
    h.push([5])
    h.push(range(30, 40))
    assert h.everything() == list(range(40))


def test_sequence_rollover():
    h = Harness()
    sent = list(range(65530, 65550))
    h.push(sent)
    assert h.everything() == sent


def test_swap_exactly_at_rollover():
    h = Harness()
    h.push([65533, 65534, 65536, 65535, 65537, 65538, 65539])
    assert h.everything() == list(range(65533, 65540))


@pytest.mark.xfail(reason="buffer drops oldest beyond maxsize=10 (200 ms) when the router stalls (fix 2: window)")
def test_router_stall_loses_nothing():
    h = Harness()
    h.push(range(30), drain=False)
    h.drain()
    assert h.everything() == list(range(30))


def test_reset_then_new_stream():
    h = Harness()
    h.push(range(20))
    h.decoder.reset()
    h.out.clear()
    h.push(range(5000, 5020))
    assert h.everything() == list(range(5000, 5020))


@pytest.mark.xfail(reason="a forward jump > 10000 without reset is taken as 'old' and dropped forever")
def test_large_forward_jump_resyncs():
    h = Harness()
    h.push(range(100))
    h.out.clear()
    h.push(range(30000, 30020))
    assert h.out[:5] == list(range(30000, 30005))


@pytest.mark.xfail(reason="last packet waits in the buffer until the next one arrives (fix 2: time-based release)")
def test_tail_is_released():
    h = Harness()
    h.push(range(10))
    assert h.out == list(range(10))


def test_waiter_does_not_accumulate_duplicates():
    h = Harness()
    h.push(range(200), drain=False)
    h.drain()
    h.push(range(200, 400))
    assert len(h.router.waiter._items) <= 1
