import asyncio
from collections import deque
from uuid import uuid4

from app.schemas import JournalEntry
from app.utils import now_utc


class EventJournalService:
    def __init__(self, max_size: int = 500) -> None:
        self._lock = asyncio.Lock()
        self._items: deque[JournalEntry] = deque(maxlen=max_size)

    async def append(self, entry: JournalEntry) -> JournalEntry:
        async with self._lock:
            self._items.append(entry)
        return entry

    async def append_system(
        self,
        title: str,
        message: str,
        *,
        level: str = "info",
        action: str | None = None,
    ) -> JournalEntry:
        entry = JournalEntry(
            id=str(uuid4()),
            timestamp=now_utc(),
            level=level,  # type: ignore[arg-type]
            title=title,
            message=message,
            action=action,
        )
        return await self.append(entry)

    async def append_channel(
        self,
        *,
        signal_id: str,
        channel_key: str,
        module: str,
        board: str,
        status: str,
        level: str,
        title: str,
        message: str,
        action: str | None,
    ) -> JournalEntry:
        entry = JournalEntry(
            id=str(uuid4()),
            timestamp=now_utc(),
            signalId=signal_id,
            channelKey=channel_key,
            module=module,
            board=board,
            status=status,
            level=level,  # type: ignore[arg-type]
            title=title,
            message=message,
            action=action,
        )
        return await self.append(entry)

    async def list_recent(self, limit: int = 100) -> list[JournalEntry]:
        limit_value = max(1, int(limit))
        async with self._lock:
            items = list(self._items)
        return items[-limit_value:]
