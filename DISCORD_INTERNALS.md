# discord.py internals this extension relies on

Not public API, so a discord.py update can break them without notice. Check these first when
receiving stops working after an upgrade. Last checked against discord.py 2.7.0, 2.7.1 and master 65232c38.

| what | used in | for |
|---|---|---|
| `VoiceConnectionState(voice_client, hook=...)` | `voice_client.py` | voice websocket messages reach `gateway.hook` |
| `DiscordVoiceWebSocket` opcodes, `ws.secret_key`, `ws._connection.voice_client` | `gateway.py` | SSRC to user mapping, new transport key after `SESSION_DESCRIPTION` |
| `VoiceClient._connection.add_socket_listener()` / `remove_socket_listener()` | `reader.py` | raw UDP packets |
| `VoiceClient._connection.socket`, `.endpoint_ip`, `.voice_port` | `reader.py` (`UDPKeepAlive`) | keepalive packets |
| `VoiceClient._connection.dave_session`, `.dave_protocol_version` | `reader.py` (`_dave_decrypt`) | DAVE end-to-end decryption, discord.py >= 2.7.0 |
| `VoiceClient.secret_key`, `VoiceClient.mode` | `reader.py` | transport decryption |
| `VoiceClient._player` | `voice_client.py` | `stop_playing()` |
| `discord.opus.Decoder`, `discord.opus._OpusStruct.get_opus_version()` (via `Decoder`) | `opus.py`, `voice_client.py` | PCM decoding, libopus check in `listen()` |
| `discord.player.CREATE_NO_WINDOW` | `sinks.py` (`FFmpegSink`) | Windows subprocess flag |
| `discord.ext.commands._types.CoroFunc` | `voice_client.py` | type hints only |
