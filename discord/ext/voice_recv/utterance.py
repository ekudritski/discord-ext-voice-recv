# -*- coding: utf-8 -*-

from __future__ import annotations

import io
import logging
import os
import queue
import threading
import time
import wave

from typing import TYPE_CHECKING

from discord.opus import Decoder

from .aio import AsyncBridge
from .ogg import OggOpusWriter
from .rtp import OPUS_SILENCE
from .sinks import AudioSink

if TYPE_CHECKING:
    from typing import Any, BinaryIO, Callable, Dict, List, Optional, Union

    from .opus import VoiceData
    from .types import MemberOrUser as User

    UtteranceCB = Callable[['Utterance'], Any]

__all__ = [
    'Utterance',
    'UtteranceSink',
]

log = logging.getLogger(__name__)

_WAKE: Any = object()


class Utterance:
    """One user's speech, from the first packet until a pause longer than the sink's ``silence``.

    ``data`` holds the :class:`VoiceData` in order, as the sink received them. Pauses shorter than
    ``silence`` are inside the utterance; the methods below fill them in, so the audio keeps real time.
    """

    def __init__(self, ssrc: int, user: Optional[User]):
        self.ssrc: int = ssrc
        self.user: Optional[User] = user
        self.data: List[VoiceData] = []
        self.started: float = time.time()
        self.ended: float = self.started

    def __repr__(self) -> str:
        return f'<Utterance user={self.user} ssrc={self.ssrc} packets={len(self.data)} duration={self.duration:.2f}>'

    @property
    def duration(self) -> float:
        """Length in seconds by RTP timestamps, pauses included."""
        if not self.data:
            return 0.0
        span = (self.data[-1].packet.timestamp - self.data[0].packet.timestamp) % 2**32
        return (span + Decoder.SAMPLES_PER_FRAME) / Decoder.SAMPLING_RATE

    def _frames(self):
        last_ts = None
        for data in self.data:
            ts = data.packet.timestamp
            missing = 0 if last_ts is None else round(((ts - last_ts) % 2**32) / Decoder.SAMPLES_PER_FRAME) - 1
            last_ts = ts
            yield max(missing, 0), data

    def opus(self) -> List[bytes]:
        """Opus packets, pauses and lost packets filled with silence. Works with either ``decode`` setting."""
        packets = []
        for missing, data in self._frames():
            packets.extend([OPUS_SILENCE] * missing)
            packets.append(data.opus or OPUS_SILENCE)
        return packets

    def pcm(self) -> bytes:
        """48 kHz 16-bit stereo PCM, pauses filled with silence. Needs a sink with ``decode=True``."""
        out = bytearray()
        silence = b'\0' * Decoder.FRAME_SIZE
        for missing, data in self._frames():
            out += silence * missing
            out += data.pcm
        return bytes(out)

    def write_ogg(self, file: Union[str, os.PathLike, BinaryIO]) -> None:
        """Ogg Opus file, see :meth:`opus`."""
        with OggOpusWriter(file) as writer:
            for packet in self.opus():
                writer.write(packet)

    def write_wav(self, file: Union[str, os.PathLike, BinaryIO]) -> None:
        """wav file, see :meth:`pcm`."""
        with wave.open(os.fspath(file) if isinstance(file, os.PathLike) else file, 'wb') as w:
            w.setnchannels(Decoder.CHANNELS)
            w.setsampwidth(Decoder.SAMPLE_SIZE // Decoder.CHANNELS)
            w.setframerate(Decoder.SAMPLING_RATE)
            w.writeframes(self.pcm())

    def to_ogg(self) -> bytes:
        buf = io.BytesIO()
        self.write_ogg(buf)
        return buf.getvalue()

    def to_wav(self) -> bytes:
        buf = io.BytesIO()
        self.write_wav(buf)
        return buf.getvalue()


class UtteranceSink(AudioSink):
    """Collects each user's packets into :class:`Utterance` objects and hands over each one as soon as
    the user has been quiet for ``silence`` seconds; it does not wait for the user to speak again.

    Every utterance goes to ``on_utterance(utterance)`` (if given) and is dispatched as the event
    ``voice_member_utterance(member, utterance)`` to sink listeners, ``VoiceRecvClient.add_listener()``
    and client events, and is yielded by ``async for utterance in sink`` (from the moment iteration
    starts; it ends when listening stops). The callback runs on the sink's own thread, not the one
    receiving packets, so slow work there (speech to text, network) does not hold up the audio.
    When listening stops, the utterances in progress are handed over too.

    Parameters
    ----------
    on_utterance
        Called with each finished utterance, on the sink's thread.
    silence
        Pause in seconds that ends an utterance.
    decode
        ``True`` for PCM (needs libopus), ``False`` for the Opus packets as Discord sent them.
    """

    def __init__(self, on_utterance: Optional[UtteranceCB] = None, *, silence: float = 2.0, decode: bool = True):
        super().__init__()

        self.on_utterance: Optional[UtteranceCB] = on_utterance
        self.silence: float = silence
        self.decode: bool = decode

        self._current: Dict[int, Utterance] = {}
        self._last_arrival: Dict[int, float] = {}
        self._last_ts: Dict[int, int] = {}
        self._lock = threading.Lock()
        self._closed = False
        self._voice_client = None
        self._bridge = AsyncBridge()
        self._done: queue.SimpleQueue[Optional[Utterance]] = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f'utterance-sink-{id(self):x}')
        self._thread.start()

    def wants_opus(self) -> bool:
        return not self.decode

    def write(self, user: Optional[User], data: VoiceData) -> None:
        ssrc = data.packet.ssrc
        ts = data.packet.timestamp
        with self._lock:
            last_ts = self._last_ts.get(ssrc)
            # the timer may not have run yet when the next utterance already arrives
            if last_ts is not None and ((ts - last_ts) % 2**32) / Decoder.SAMPLING_RATE > self.silence:
                self._finish(ssrc)

            utterance = self._current.get(ssrc)
            if utterance is None:
                utterance = self._current[ssrc] = Utterance(ssrc, user)
                # the timer thread may be waiting without a deadline
                self._done.put(_WAKE)
            elif user is not None:
                utterance.user = user
            utterance.data.append(data)
            utterance.ended = time.time()
            self._last_arrival[ssrc] = time.monotonic()
            self._last_ts[ssrc] = ts

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            for ssrc in list(self._current):
                self._finish(ssrc)
        self._done.put(None)
        if threading.current_thread() is not self._thread:
            self._thread.join()
        self._bridge.close(self.client.loop if self.client else None)

    def __aiter__(self) -> AsyncBridge:
        return self._bridge.__aiter__()

    def _finish(self, ssrc: int) -> None:
        utterance = self._current.pop(ssrc, None)
        self._last_arrival.pop(ssrc, None)
        self._last_ts.pop(ssrc, None)
        if utterance is not None:
            self._done.put(utterance)

    def _run(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                for ssrc, arrival in list(self._last_arrival.items()):
                    if now - arrival >= self.silence:
                        self._finish(ssrc)
                deadline = min(self._last_arrival.values(), default=None)

            timeout = None if deadline is None else max(deadline + self.silence - time.monotonic(), 0)
            try:
                utterance = self._done.get(timeout=timeout)
            except queue.Empty:
                continue
            if utterance is None:
                self._drain()
                return
            if utterance is not _WAKE:
                self._hand_over(utterance)

    def _drain(self) -> None:
        while True:
            try:
                utterance = self._done.get_nowait()
            except queue.Empty:
                return
            if utterance is not None and utterance is not _WAKE:
                self._hand_over(utterance)

    def _hand_over(self, utterance: Utterance) -> None:
        if self.on_utterance is not None:
            try:
                self.on_utterance(utterance)
            except Exception:
                log.exception('Error in on_utterance callback for %s', utterance)
        if self.voice_client is not None:
            self.voice_client.dispatch_threadsafe('voice_member_utterance', utterance.user, utterance)
            self._bridge.put(self.client.loop if self.client else None, utterance)
