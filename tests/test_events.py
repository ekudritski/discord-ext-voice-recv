import asyncio
import threading

from discord.utils import MISSING

from discord.ext.voice_recv.reader import SpeakingTimer
from discord.ext.voice_recv.voice_client import VoiceRecvClient


class StubClient:
    def __init__(self, loop):
        self.loop = loop
        self.events = []

    def dispatch(self, event, *args):
        asyncio.get_running_loop()  # raises outside the event loop thread
        self.events.append((event, threading.current_thread().name))


def make_voice_client(loop):
    vc = VoiceRecvClient.__new__(VoiceRecvClient)
    vc.client = StubClient(loop)
    vc._reader = MISSING
    vc._event_listeners = {}
    return vc


def test_speaking_events_reach_listeners_from_other_threads():
    # #63: speaking start/stop used to reach only sink listeners
    async def main():
        loop = asyncio.get_running_loop()
        vc = make_voice_client(loop)
        heard = []
        done = asyncio.Event()

        async def on_voice_member_speaking_start(member):
            heard.append(member)
            done.set()

        vc.add_listener(on_voice_member_speaking_start)

        timer = SpeakingTimer.__new__(SpeakingTimer)
        timer.voice_client = vc
        timer._lookup_member = lambda ssrc: f"member-{ssrc}"
        thread = threading.Thread(target=timer.dispatch, args=("voice_member_speaking_start", 7), name="udp")
        thread.start()
        thread.join()

        await asyncio.wait_for(done.wait(), 1)
        assert heard == ["member-7"]
        assert vc.client.events == [("voice_member_speaking_start", threading.current_thread().name)]

    asyncio.run(main())


def test_dispatch_threadsafe_after_loop_closed():
    loop = asyncio.new_event_loop()
    loop.close()
    make_voice_client(loop).dispatch_threadsafe("voice_member_speaking_stop", "m")
