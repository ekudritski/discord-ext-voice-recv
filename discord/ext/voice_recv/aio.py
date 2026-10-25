# -*- coding: utf-8 -*-

from __future__ import annotations

import asyncio
import logging

from typing import TYPE_CHECKING

from .sinks import AudioSink

if TYPE_CHECKING:
    from typing import Any, Optional, Tuple

    from .opus import VoiceData
    from .types import MemberOrUser as User

__all__ = [
    'AsyncPacketSink',
]

log = logging.getLogger(__name__)

_END: Any = object()


class AsyncBridge:
    """Hands items from receiving threads to ``async for`` on the event loop.

    Items are queued only once iteration has started, so an un-iterated bridge does not grow.
    Iteration ends when :meth:`close` is called.
    """

    def __init__(self, *, active: bool = False):
        self._active = active
        self._queue: Optional[asyncio.Queue] = None

    def put(self, loop: Optional[asyncio.AbstractEventLoop], item: Any) -> None:
        """Thread-safe."""
        if not self._active or loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._put_in_loop, item)
        except RuntimeError:
            # the loop is closed, the client is shutting down
            pass

    def close(self, loop: Optional[asyncio.AbstractEventLoop]) -> None:
        self.put(loop, _END)

    def _get_queue(self) -> asyncio.Queue:
        # created on the loop's thread, both from _put_in_loop and __anext__
        if self._queue is None:
            self._queue = asyncio.Queue()
        return self._queue

    def _put_in_loop(self, item: Any) -> None:
        self._get_queue().put_nowait(item)

    def __aiter__(self) -> AsyncBridge:
        self._active = True
        return self

    async def __anext__(self) -> Any:
        queue = self._get_queue()
        item = await queue.get()
        if item is _END:
            queue.put_nowait(_END)
            raise StopAsyncIteration
        return item


class AsyncPacketSink(AudioSink):
    """Receive packets with ``async for user, data in sink`` on the event loop.

    Iteration ends when listening stops. Packets are queued from the moment iteration starts,
    so start iterating right after :meth:`VoiceRecvClient.listen`.

    Parameters
    ----------
    decode
        ``True`` for PCM (``data.pcm``, needs libopus), ``False`` for Opus (``data.opus``).
    """

    def __init__(self, *, decode: bool = True):
        super().__init__()
        self.decode: bool = decode
        self._bridge = AsyncBridge()
        self._voice_client = None

    def wants_opus(self) -> bool:
        return not self.decode

    def write(self, user: Optional[User], data: VoiceData) -> None:
        self._bridge.put(self.client.loop if self.client else None, (user, data))

    def cleanup(self) -> None:
        self._bridge.close(self.client.loop if self.client else None)

    def __aiter__(self) -> AsyncBridge:
        return self._bridge.__aiter__()

    async def __anext__(self) -> Tuple[Optional[User], VoiceData]:
        return await self._bridge.__anext__()
