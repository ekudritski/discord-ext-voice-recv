# -*- coding: utf-8 -*-

from __future__ import annotations

import time
import asyncio
import logging
import functools
import threading

import discord
from discord.voice_state import VoiceConnectionState
from discord.utils import MISSING

from typing import TYPE_CHECKING

from .gateway import hook
from .reader import AudioReader
from .sinks import AudioSink

if TYPE_CHECKING:
    from typing import Optional, Dict, Any, Union
    from discord.ext.commands._types import CoroFunc
    from .reader import AfterCB

from pprint import pformat

__all__ = [
    'VoiceRecvClient',
]

log = logging.getLogger(__name__)


class VoiceRecvClient(discord.VoiceClient):
    endpoint_ip: str
    voice_port: int

    def __init__(self, client: discord.Client, channel: discord.abc.Connectable):
        super().__init__(client, channel)

        self._reader: AudioReader = MISSING
        self._ssrc_to_id: Dict[int, int] = {}
        self._id_to_ssrc: Dict[int, int] = {}
        self._event_listeners: Dict[str, list] = {}

    def create_connection_state(self) -> VoiceConnectionState:
        return VoiceConnectionState(self, hook=hook)

    async def on_voice_state_update(self, data) -> None:
        old_channel_id = self.channel.id if self.channel else None

        await super().on_voice_state_update(data)

        log.debug("Got voice_client VSU: \n%s", pformat(data, compact=True))

        # this can be None
        try:
            channel_id = int(data['channel_id'])
        except TypeError:
            return

        # if we joined, left, or switched channels, reset the decoders
        if self._reader and channel_id != old_channel_id:
            log.debug("Destroying all decoders in guild %s", self.guild.id)
            self._reader.packet_router.destroy_all_decoders()

    def add_listener(self, func: CoroFunc, *, name: str = MISSING) -> None:
        name = func.__name__ if name is MISSING else name

        if not asyncio.iscoroutinefunction(func):
            raise TypeError('Listeners must be coroutines')

        if name in self._event_listeners:
            self._event_listeners[name].append(func)
        else:
            self._event_listeners[name] = [func]

    def remove_listener(self, func: CoroFunc, *, name: str = MISSING) -> None:
        name = func.__name__ if name is MISSING else name

        if name in self._event_listeners:
            try:
                self._event_listeners[name].remove(func)
            except ValueError:
                pass

    async def _run_event(self, coro: CoroFunc, event_name: str, *args: Any, **kwargs: Any) -> None:
        try:
            await coro(*args, **kwargs)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Error calling %s", event_name)

    def _schedule_event(self, coro: CoroFunc, event_name: str, *args: Any, **kwargs: Any) -> asyncio.Task:
        wrapped = self._run_event(coro, event_name, *args, **kwargs)
        return self.client.loop.create_task(wrapped, name=f"ext.voice_recv: {event_name}")

    def dispatch(self, event: str, /, *args: Any, **kwargs: Any) -> None:
        log.debug("Dispatching voice_client event %s", event)

        self.dispatch_sink(event, *args, **kwargs)
        self._dispatch_listeners(event, *args, **kwargs)

    def dispatch_threadsafe(self, event: str, /, *args: Any, **kwargs: Any) -> None:
        """:meth:`dispatch` for threads other than the event loop's: listeners are scheduled through the loop."""
        log.debug("Dispatching voice_client event %s from %s", event, threading.current_thread().name)

        self.dispatch_sink(event, *args, **kwargs)
        try:
            self.client.loop.call_soon_threadsafe(functools.partial(self._dispatch_listeners, event, *args, **kwargs))
        except RuntimeError:
            # the loop is closed, the client is shutting down
            log.debug("Event loop closed, not dispatching %s", event)

    def _dispatch_listeners(self, event: str, /, *args: Any, **kwargs: Any) -> None:
        event_name = f"on_{event}"
        for coro in self._event_listeners.get(event_name, []):
            self._schedule_event(coro, event_name, *args, **kwargs)

        self.client.dispatch(event, *args, **kwargs)

    def dispatch_sink(self, event: str, /, *args: Any, **kwargs: Any) -> None:
        if self._reader:
            self._reader.event_router.dispatch(event, *args, **kwargs)

    def cleanup(self) -> None:
        # TODO: Does the order here matter?
        super().cleanup()
        self._event_listeners.clear()
        self.stop()

    def _add_ssrc(self, user_id: int, ssrc: int) -> None:
        self._ssrc_to_id[ssrc] = user_id
        self._id_to_ssrc[user_id] = ssrc

        if self._reader:
            self._reader.packet_router.set_user_id(ssrc, user_id)

    def _remove_ssrc(self, *, user_id: int) -> None:
        ssrc = self._id_to_ssrc.pop(user_id, None)
        if ssrc:
            self._reader.speaking_timer.drop_ssrc(ssrc)
            self._ssrc_to_id.pop(ssrc, None)

    def _get_ssrc_from_id(self, user_id: int) -> Optional[int]:
        return self._id_to_ssrc.get(user_id)

    def _get_id_from_ssrc(self, ssrc: int) -> Optional[int]:
        return self._ssrc_to_id.get(ssrc)

    def listen(
        self,
        sink: AudioSink,
        *,
        after: Optional[AfterCB] = None,
        jitter_buffer: Optional[Dict[str, Any]] = None,
        speaking_timeout: float = 0.2,
    ) -> None:
        """Receives audio into a :class:`AudioSink`.

        Parameters
        ----------
        after
            Called with the error (or ``None``) when listening stops.
        jitter_buffer
            Keyword arguments for each user's :class:`HeapJitterBuffer`: ``window`` (packets a missing
            one is waited for while newer ones arrive, default 25), ``max_wait`` (seconds it is waited for
            when nothing newer arrives, default 0.5), ``maxsize`` (default 500), ``prefsize``, ``prefill``.
        speaking_timeout
            Seconds without packets after which ``voice_member_speaking_stop`` is dispatched.
        """

        if not self.is_connected():
            raise discord.ClientException('Not connected to voice.')

        if not isinstance(sink, AudioSink):
            raise TypeError('sink must be an AudioSink not {0.__class__.__name__}'.format(sink))

        if self.is_listening():
            raise discord.ClientException('Already receiving audio.')

        if not sink.wants_opus():
            # otherwise the first packet fails inside the reader thread and listening stops silently
            try:
                discord.opus.Decoder.get_opus_version()
            except discord.opus.OpusNotLoaded:
                raise discord.opus.OpusNotLoaded(
                    'libopus is needed to decode audio for a sink whose wants_opus() is False. '
                    'Install libopus (or call discord.opus.load_opus()), or use a sink that wants opus.'
                ) from None

        self._reader = AudioReader(sink, self, after=after, jitter_buffer=jitter_buffer, speaking_timeout=speaking_timeout)
        self._reader.start()

    def is_listening(self) -> bool:
        """Indicates if we're currently receiving audio."""
        return self._reader and self._reader.is_listening()

    def stop_listening(self) -> None:
        """Stops receiving audio."""
        if self._reader:
            self._reader.stop()
            self._reader = MISSING

    def stop_playing(self) -> None:
        """Stops playing audio."""
        if self._player:
            self._player.stop()
            self._player = None

    def stop(self) -> None:
        """Stops playing and receiving audio."""
        self.stop_playing()
        self.stop_listening()

    @property
    def sink(self) -> Optional[AudioSink]:
        return self._reader.sink if self._reader else None

    @sink.setter
    def sink(self, sink: AudioSink) -> None:
        if not isinstance(sink, AudioSink):
            raise TypeError('expected AudioSink not {0.__class__.__name__}.'.format(sink))

        if not self._reader:
            raise ValueError('Not receiving anything.')

        self._reader.set_sink(sink)

    def get_speaking(self, member: Union[discord.Member, discord.User]) -> Optional[bool]:
        """Returns if a member is speaking (approximately), or None if not found."""

        ssrc = self._get_ssrc_from_id(member.id)
        if ssrc is None:
            return

        if self._reader:
            return self._reader.speaking_timer.get_speaking(ssrc)
