import discord.opus
import pytest

from discord.ext.voice_recv import voice_client as vc_module
from discord.ext.voice_recv.sinks import BasicSink


class StubReader:
    def __init__(self, sink, voice_client, **kw):
        self.sink = sink
        self.kw = kw

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


def test_listen_passes_buffer_options_and_speaking_timeout(no_libopus):
    vc = StubVoiceClient()
    vc_module.VoiceRecvClient.listen(
        vc, BasicSink(lambda u, d: None, decode=False), jitter_buffer=dict(window=50, max_wait=1.0), speaking_timeout=0.5
    )
    assert vc._reader.kw["jitter_buffer"] == dict(window=50, max_wait=1.0)
    assert vc._reader.kw["speaking_timeout"] == 0.5
