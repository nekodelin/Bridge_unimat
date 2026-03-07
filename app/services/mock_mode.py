import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.models import BoardPayload

logger = logging.getLogger("unimat.mock")


class MockModeService:
    def __init__(
        self,
        on_board_payload: Callable[[BoardPayload, str], Awaitable[None]],
        interval_sec: float = 2.0,
    ) -> None:
        self.on_board_payload = on_board_payload
        self.interval_sec = interval_sec
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Mock mode started")

    async def stop(self) -> None:
        self._running = False
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        logger.info("Mock mode stopped")

    async def _run_loop(self) -> None:
        steps = [
            {"in": 0b11111111, "inversed": 0b00000000, "out": 0b00000000},  # normal off
            {"in": 0b11111111, "inversed": 0b11111111, "out": 0b11111111},  # normal on
            {"in": 0b00010000, "inversed": 0b00000000, "out": 0b00010000},  # breakage sample
            {"in": 0b00010000, "inversed": 0b00010000, "out": 0b00000000},  # short sample
        ]
        index = 0

        while self._running:
            payload = BoardPayload.model_validate(steps[index % len(steps)])
            await self.on_board_payload(payload, "puma_board")
            index += 1
            await asyncio.sleep(self.interval_sec)
