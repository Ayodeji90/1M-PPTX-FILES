"""Single-writer DB task for the async downloader.

SQLite allows one writer at a time, so worker coroutines never touch the
connection: they enqueue ops here. Row updates are batched into one
transaction per `batch_size` ops (or `flush_interval_s`, whichever comes
first); call-ops (claims, dedup checks) flush pending updates first so
per-row ordering is preserved, then run inline and resolve a future.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from ..db.dao import Registry

log = logging.getLogger("pptxsweeper.download.writer")


class DbWriter:
    def __init__(self, reg: Registry, batch_size: int = 100, flush_interval_s: float = 2.0):
        self.reg = reg
        self.batch_size = batch_size
        self.flush_interval_s = flush_interval_s
        self.queue: asyncio.Queue = asyncio.Queue()
        self._pending_updates: list[tuple[int, dict[str, Any]]] = []
        self._task: asyncio.Task | None = None
        self._stopping = False

    # ------------------------------------------------------------------
    # Producer API (called from worker coroutines)
    # ------------------------------------------------------------------
    def update_url(self, url_id: int, **fields: Any) -> None:
        self.queue.put_nowait(("update", (url_id, fields)))

    async def call(self, fn: Callable, *args, **kwargs) -> Any:
        """Run an arbitrary Registry method on the writer, after flushing
        pending updates. Returns its result."""
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.queue.put_nowait(("call", (fn, args, kwargs, future)))
        return await future

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="db-writer")

    async def stop(self) -> None:
        """Drain the queue, flush, and stop. Call after workers exit."""
        self._stopping = True
        self.queue.put_nowait(("stop", None))
        if self._task:
            await self._task

    # ------------------------------------------------------------------
    async def _run(self) -> None:
        while True:
            try:
                kind, payload = await asyncio.wait_for(
                    self.queue.get(), timeout=self.flush_interval_s
                )
            except asyncio.TimeoutError:
                await self._flush()
                continue

            if kind == "stop":
                # drain whatever is left without waiting
                while not self.queue.empty():
                    k2, p2 = self.queue.get_nowait()
                    if k2 == "update":
                        self._pending_updates.append(p2)
                    elif k2 == "call":
                        await self._flush()
                        await self._exec_call(p2)
                await self._flush()
                return

            if kind == "update":
                self._pending_updates.append(payload)
                if len(self._pending_updates) >= self.batch_size:
                    await self._flush()
            elif kind == "call":
                await self._flush()
                await self._exec_call(payload)

    async def _flush(self) -> None:
        if not self._pending_updates:
            return
        batch, self._pending_updates = self._pending_updates, []
        try:
            await asyncio.to_thread(self.reg.update_urls, batch)
        except Exception:
            log.exception("db writer flush failed for %d updates", len(batch))

    async def _exec_call(self, payload) -> None:
        fn, args, kwargs, future = payload
        try:
            result = await asyncio.to_thread(fn, *args, **kwargs)
            if not future.done():
                future.set_result(result)
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
