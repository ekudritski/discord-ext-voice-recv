import os
import struct

import nacl.secret

from discord.ext.voice_recv.reader import AudioReader, PacketDecryptor

MODE = "aead_xchacha20_poly1305_rtpsize"
SSRC = 1234


def encrypt(key, n, payload=b"\x78\x00\x01"):
    header = struct.pack(">BBHII", 0x80, 0x78, n, n * 960, SSRC)
    nonce = bytearray(24)
    nonce[:4] = struct.pack(">I", n)
    return header + nacl.secret.Aead(key).encrypt(payload, header, bytes(nonce)).ciphertext + nonce[:4]


class StubConnection:
    dave_session = None
    dave_protocol_version = 0


class StubVoiceClient:
    def __init__(self, key, mode=MODE):
        self.secret_key = list(key)
        self.mode = mode
        self._connection = StubConnection()
        self._ssrc_to_id = {SSRC: 42}


class StubRouter:
    def __init__(self):
        self.fed = []

    def feed_rtp(self, packet):
        self.fed.append(packet.sequence)


class StubTimer:
    def notify(self, ssrc=None):
        pass


def reader_with(reader_key, client_key):
    reader = object.__new__(AudioReader)
    reader.voice_client = StubVoiceClient(client_key)
    reader.decryptor = PacketDecryptor(MODE, reader_key)
    reader._crypto_errors = 0
    reader.error = None
    reader.packet_router = StubRouter()
    reader.speaking_timer = StubTimer()
    return reader


def crypto_logs(caplog):
    return [r for r in caplog.records if "CryptoError" in r.getMessage() and r.levelname != "DEBUG"]


def test_current_key_decrypts():
    key = os.urandom(32)
    reader = reader_with(key, key)
    reader.callback(encrypt(key, 1))
    assert reader.packet_router.fed == [1]


def test_stale_key_is_resynced_and_packet_kept(caplog):
    old, new = os.urandom(32), os.urandom(32)
    reader = reader_with(old, new)
    reader.callback(encrypt(new, 1))
    reader.callback(encrypt(new, 2))
    assert reader.packet_router.fed == [1, 2]
    assert reader.decryptor.secret_key == new
    assert not crypto_logs(caplog)


def test_garbage_with_current_key_is_dropped_without_retry_loop():
    key = os.urandom(32)
    reader = reader_with(key, key)
    reader.callback(encrypt(os.urandom(32), 1))
    assert reader.packet_router.fed == []
    assert reader._crypto_errors == 1


def test_crypto_error_storm_is_rate_limited_and_recovery_logged(caplog):
    caplog.set_level("INFO", logger="discord.ext.voice_recv.reader")
    key = os.urandom(32)
    reader = reader_with(key, key)
    for n in range(50):
        reader.callback(encrypt(os.urandom(32), n))
    assert len(crypto_logs(caplog)) == 2
    reader.callback(encrypt(key, 50))
    assert reader.packet_router.fed == [50]
    assert reader._crypto_errors == 0
    assert "after 50 CryptoErrors" in caplog.records[-1].getMessage()


def test_update_secret_key_takes_new_mode():
    reader = reader_with(os.urandom(32), os.urandom(32))
    reader.update_secret_key(os.urandom(32), "xsalsa20_poly1305_lite")
    assert reader.decryptor.mode == "xsalsa20_poly1305_lite"
    assert isinstance(reader.decryptor.box, nacl.secret.SecretBox)
