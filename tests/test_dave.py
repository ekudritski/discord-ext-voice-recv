import pytest

from discord.ext.voice_recv.reader import AudioReader
from discord.ext.voice_recv.rtp import OPUS_SILENCE

from test_buffer import make_packet

USER_ID = 42


class StubSession:
    def __init__(self, result=b"plain", error=None):
        self.result = result
        self.error = error
        self.calls = []

    def decrypt(self, user_id, media_type, data):
        self.calls.append((user_id, data))
        if self.error:
            raise self.error
        return self.result


class StubConnection:
    def __init__(self, session, version=1):
        self.dave_session = session
        self.dave_protocol_version = version


class StubVoiceClient:
    def __init__(self, session, version=1, user_id=USER_ID):
        self._connection = StubConnection(session, version)
        self.user_id = user_id

    def _get_id_from_ssrc(self, ssrc):
        return self.user_id


def reader_with(voice_client):
    reader = object.__new__(AudioReader)
    reader.voice_client = voice_client
    return reader


def test_decrypts_with_sender_id():
    session = StubSession()
    packet = make_packet(1)
    assert reader_with(StubVoiceClient(session))._dave_decrypt(packet)
    assert packet.decrypted_data == b"plain"
    assert session.calls[0][0] == USER_ID


@pytest.mark.parametrize("session,version", [(None, 1), (StubSession(), 0)])
def test_passthrough_without_dave(session, version):
    packet = make_packet(1)
    before = packet.decrypted_data
    assert reader_with(StubVoiceClient(session, version))._dave_decrypt(packet)
    assert packet.decrypted_data == before


def test_silence_frame_passes_unencrypted():
    session = StubSession()
    packet = make_packet(1)
    packet.decrypted_data = OPUS_SILENCE
    assert reader_with(StubVoiceClient(session))._dave_decrypt(packet)
    assert not session.calls


def test_unknown_sender_is_dropped():
    packet = make_packet(1)
    assert not reader_with(StubVoiceClient(StubSession(), user_id=None))._dave_decrypt(packet)


def test_decrypt_error_drops_packet_without_raising():
    packet = make_packet(1)
    before = packet.decrypted_data
    reader = reader_with(StubVoiceClient(StubSession(error=RuntimeError("NoDecryptorForUser"))))
    assert not reader._dave_decrypt(packet)
    assert packet.decrypted_data == before
