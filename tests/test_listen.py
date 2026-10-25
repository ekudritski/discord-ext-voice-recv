import discord.opus
import pytest

from discord.ext.voice_recv import voice_client as vc_module
from discord.ext.voice_recv.sinks import BasicSink


class StubReader:
    def __init__(self, sink, voice_client, after=None):
        self.sink = sink

    def start(self):
        pass


class StubVoiceClient:
    _reader = None

    def is_connected(self):
        return True

    def is_listening(self):
        return False


@pytest.fixture
def no_libopus(monkeypatch):
    monkeypatch.setattr(discord.opus, "is_loaded", lambda: False)
    monkeypatch.setattr(discord.opus, "_load_default", lambda: False)
    monkeypatch.setattr(vc_module, "AudioReader", StubReader)


def listen(sink):
    vc = StubVoiceClient()
    vc_module.VoiceRecvClient.listen(vc, sink)
    return vc


def test_pcm_sink_without_libopus_fails_at_listen(no_libopus):
    with pytest.raises(discord.opus.OpusNotLoaded, match="libopus"):
        listen(BasicSink(lambda u, d: None, decode=True))


def test_opus_sink_without_libopus_listens(no_libopus):
    vc = listen(BasicSink(lambda u, d: None, decode=False))
    assert isinstance(vc._reader, StubReader)
