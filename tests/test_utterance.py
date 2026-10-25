import io
import queue
import time
import wave

from discord.ext.voice_recv.opus import VoiceData
from discord.ext.voice_recv.rtp import OPUS_SILENCE, FakePacket
from discord.ext.voice_recv.utterance import UtteranceSink

SILENCE = 0.1  # short for the tests; the default is 2 s


class Packet:
    def __init__(self, ssrc, seq, ts, data):
        self.ssrc, self.sequence, self.timestamp, self.decrypted_data = ssrc, seq, ts, data


class StubVoiceClient:
    def __init__(self):
        self.events = []

    def dispatch_threadsafe(self, event, *args):
        self.events.append((event, args))


def make_sink(**kw):
    got = queue.SimpleQueue()
    sink = UtteranceSink(got.put, silence=SILENCE, **kw)
    return sink, got


def feed(sink, ssrc, start_seq, start_ts, n, user="alice", pcm=None):
    for i in range(n):
        p = Packet(ssrc, start_seq + i, start_ts + i * 960, bytes([0xFC, i]))
        sink.write(user, VoiceData(p, user, pcm=pcm))


def test_handed_over_after_silence_without_more_packets():
    sink, got = make_sink(decode=False)
    feed(sink, 1, 0, 0, 10)
    t = time.monotonic()
    u = got.get(timeout=2)
    assert SILENCE * 0.9 <= time.monotonic() - t < SILENCE + 0.5
    assert u.user == "alice" and len(u.data) == 10
    assert abs(u.duration - 0.2) < 1e-9
    sink.cleanup()


def test_timestamp_gap_ends_utterance_before_timer():
    sink, got = make_sink(decode=False)
    feed(sink, 1, 0, 0, 5)
    feed(sink, 1, 5, 4 * 960 + int(48000 * (SILENCE + 1)), 3)  # next speech already here
    first = got.get(timeout=0.05)
    assert len(first.data) == 5
    second = got.get(timeout=2)
    assert len(second.data) == 3
    sink.cleanup()


def test_users_are_separate():
    sink, got = make_sink(decode=False)
    feed(sink, 1, 0, 0, 3, user="alice")
    feed(sink, 2, 100, 5000, 4, user="bob")
    users = sorted((got.get(timeout=2).user, got.get(timeout=2).user))
    assert users == ["alice", "bob"]
    sink.cleanup()


def test_opus_fills_pauses_and_lost_packets():
    sink, got = make_sink(decode=False)
    a = Packet(1, 0, 0, bytes([0xFC, 1]))
    b = FakePacket(1, 1, 960)  # lost
    c = Packet(1, 4, 4 * 960, bytes([0xFC, 2]))  # 2 frames of pause before it
    for p in (a, b, c):
        sink.write("alice", VoiceData(p, "alice"))
    u = got.get(timeout=2)
    assert u.opus() == [bytes([0xFC, 1]), OPUS_SILENCE, OPUS_SILENCE, OPUS_SILENCE, bytes([0xFC, 2])]
    assert len(u.to_ogg()) > 0
    sink.cleanup()


def test_pcm_fills_pauses_and_wav():
    sink, got = make_sink(decode=True)
    frame = b"\x01\x00" * 1920
    sink.write("alice", VoiceData(Packet(1, 0, 0, b"x"), "alice", pcm=frame))
    sink.write("alice", VoiceData(Packet(1, 1, 3 * 960, b"x"), "alice", pcm=frame))
    u = got.get(timeout=2)
    assert u.pcm() == frame + b"\0" * 3840 * 2 + frame
    with wave.open(io.BytesIO(u.to_wav())) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (2, 2, 48000, 4 * 960)
    sink.cleanup()


def test_cleanup_hands_over_utterance_in_progress():
    sink, got = make_sink(decode=False)
    sink.silence = 60
    feed(sink, 1, 0, 0, 3)
    sink.cleanup()
    assert len(got.get_nowait().data) == 3
    sink.cleanup()  # second call is harmless


def test_event_dispatched_and_callback_error_survived():
    calls = []

    def bad(u):
        calls.append(u)
        raise RuntimeError("boom")

    sink = UtteranceSink(bad, silence=SILENCE, decode=False)
    vc = StubVoiceClient()
    sink._voice_client = vc
    feed(sink, 1, 0, 0, 2)
    time.sleep(SILENCE + 0.3)
    feed(sink, 1, 2, 10 * 48000, 2)
    sink.cleanup()
    assert len(calls) == 2
    assert [e for e, _ in vc.events] == ["voice_member_utterance"] * 2
    assert vc.events[0][1][0] == "alice"
