import asyncio
import threading

from discord.ext.voice_recv.aio import AsyncPacketSink
from discord.ext.voice_recv.opus import VoiceData
from discord.ext.voice_recv.utterance import UtteranceSink


class Packet:
    def __init__(self, ssrc, seq, ts):
        self.ssrc, self.sequence, self.timestamp, self.decrypted_data = ssrc, seq, ts, bytes([0xFC, seq])


class StubClient:
    def __init__(self, loop):
        self.loop = loop

    def dispatch(self, *args):
        pass


class StubVoiceClient:
    def __init__(self, loop):
        self.client = StubClient(loop)

    def dispatch_threadsafe(self, *args):
        pass


def attach(sink):
    sink._voice_client = StubVoiceClient(asyncio.get_running_loop())
    return sink


def in_thread(fn):
    t = threading.Thread(target=fn, name="router")
    t.start()
    return t


def test_packets_reach_async_for_and_iteration_ends():
    async def main():
        sink = attach(AsyncPacketSink(decode=False))
        it = sink.__aiter__()

        def produce():
            for i in range(5):
                sink.write("alice", VoiceData(Packet(1, i, i * 960), "alice"))
            sink.cleanup()

        in_thread(produce)
        got = [(user, data.packet.sequence) async for user, data in it]
        assert got == [("alice", i) for i in range(5)]

    asyncio.run(asyncio.wait_for(main(), 2))


def test_nothing_queued_without_iteration():
    async def main():
        sink = attach(AsyncPacketSink(decode=False))
        in_thread(lambda: sink.write("alice", VoiceData(Packet(1, 0, 0), "alice"))).join()
        await asyncio.sleep(0.01)
        assert sink._bridge._queue is None

    asyncio.run(main())


def test_utterances_reach_async_for():
    async def main():
        sink = attach(UtteranceSink(silence=0.05, decode=False))
        it = sink.__aiter__()

        def produce():
            for i in range(3):
                sink.write("alice", VoiceData(Packet(1, i, i * 960), "alice"))
            for i in range(2):
                sink.write("bob", VoiceData(Packet(2, i, i * 960), "bob"))
            sink.cleanup()

        in_thread(produce)
        got = sorted([(u.user, len(u.data)) async for u in it])
        assert got == [("alice", 3), ("bob", 2)]

    asyncio.run(asyncio.wait_for(main(), 2))
