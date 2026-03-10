from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import JournalEvent
from app.schemas import ChannelState, JournalEntry


class EventJournalService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def append_system(
        self,
        title: str,
        message: str,
        *,
        level: str = "info",
        action: str | None = None,
    ) -> JournalEntry:
        return await self.append_event(
            event_type="system",
            source="system",
            element_key="system",
            element_name=title,
            previous_state=None,
            new_state=level,
            description=message,
            board=None,
            module=None,
            channel_key=None,
            signal_id=None,
            payload_json=None,
            level=level,
            title=title,
            message=message,
            action=action,
        )

    async def append_auth(
        self,
        *,
        username: str,
        action: str,
    ) -> JournalEntry:
        action_name = action.strip().lower()
        if action_name == "logout":
            description = f"Пользователь {username} выполнил выход из журнала"
            title = "User logout"
        else:
            description = f"Пользователь {username} выполнил вход в журнал"
            title = "User login"
            action_name = "login"

        return await self.append_event(
            event_type="auth",
            source="auth",
            element_key=f"user:{username}",
            element_name=username,
            previous_state=None,
            new_state=action_name,
            description=description,
            board=None,
            module=None,
            channel_key=None,
            signal_id=None,
            payload_json={"action": action_name},
            level="info",
            title=title,
            message=description,
            action=None,
        )

    async def append_state_change(
        self,
        *,
        source: str,
        channel: ChannelState,
        previous_state: str | None,
        raw_payload: dict[str, Any] | None = None,
    ) -> JournalEntry:
        journal_source = "mqtt" if source in {"mqtt", "mock"} else "system"
        channel_label = self._resolve_channel_label(channel)
        base_message = channel.cause if channel.isFault and channel.cause else channel.message
        old_state = previous_state or "unknown"
        description = f"{channel.title}: {old_state} -> {channel.status}. {base_message}"

        payload_json = {
            "raw": raw_payload,
            "decoded": {
                "input": channel.input,
                "output": channel.output,
                "diagnostic": channel.diagnostic,
                "isFault": channel.isFault,
            },
        }

        return await self.append_event(
            event_type="state_change",
            source=journal_source,
            element_key=channel.channelKey,
            element_name=channel_label,
            previous_state=previous_state,
            new_state=channel.status,
            description=description,
            board=channel.board,
            module=channel.module,
            channel_key=channel.channelKey,
            signal_id=channel.signalId,
            payload_json=payload_json,
            level=channel.severity,
            title=channel.title,
            message=base_message,
            action=channel.action,
        )

    @staticmethod
    def _resolve_channel_label(channel: ChannelState) -> str:
        for candidate in (channel.logicalChannel, channel.rawChannel):
            if candidate is None:
                continue
            token = str(candidate).strip().upper()
            if len(token) == 1 and token in "0123456789ABCDEF":
                return token
        return format(int(channel.channelIndex), "X")

    async def append_event(
        self,
        *,
        event_type: str,
        source: str,
        element_key: str | None,
        element_name: str | None,
        previous_state: str | None,
        new_state: str | None,
        description: str,
        board: str | None,
        module: str | None,
        channel_key: str | None,
        signal_id: str | None,
        payload_json: dict[str, Any] | None,
        level: str,
        title: str,
        message: str,
        action: str | None,
    ) -> JournalEntry:
        model = JournalEvent(
            event_type=event_type,
            source=source,
            element_key=element_key,
            element_name=element_name,
            previous_state=previous_state,
            new_state=new_state,
            description=description,
            board=board,
            module=module,
            channel_key=channel_key,
            signal_id=signal_id,
            payload_json=payload_json,
            level=level,
            title=title,
            message=message,
            action=action,
        )
        async with self._session_factory() as session:
            session.add(model)
            await session.commit()
            await session.refresh(model)
        return self._to_schema(model)

    async def list_recent(
        self,
        *,
        limit: int = 100,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list[JournalEntry]:
        query = self._build_query(limit=limit, date_from=date_from, date_to=date_to)
        models = await self._fetch(query)
        return [self._to_schema(item) for item in models]

    async def list_for_export(
        self,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list[JournalEntry]:
        query = self._build_query(limit=None, date_from=date_from, date_to=date_to)
        models = await self._fetch(query)
        return [self._to_schema(item) for item in models]

    def _build_query(
        self,
        *,
        limit: int | None,
        date_from: datetime | None,
        date_to: datetime | None,
    ) -> Select[tuple[JournalEvent]]:
        stmt: Select[tuple[JournalEvent]] = select(JournalEvent)
        if date_from is not None:
            stmt = stmt.where(JournalEvent.created_at >= _normalize_datetime(date_from))
        if date_to is not None:
            stmt = stmt.where(JournalEvent.created_at <= _normalize_datetime(date_to))
        stmt = stmt.order_by(JournalEvent.created_at.desc(), JournalEvent.id.desc())
        if limit is not None:
            stmt = stmt.limit(max(1, int(limit)))
        return stmt

    async def _fetch(self, stmt: Select[tuple[JournalEvent]]) -> list[JournalEvent]:
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return list(result.scalars().all())

    @staticmethod
    def _to_schema(model: JournalEvent) -> JournalEntry:
        created_at = _normalize_datetime(model.created_at)
        return JournalEntry(
            id=str(model.id),
            timestamp=created_at,
            signalId=model.signal_id,
            channelKey=model.channel_key,
            module=model.module,
            board=model.board,
            status=model.new_state,
            level=model.level,  # type: ignore[arg-type]
            title=model.title,
            message=model.message,
            action=model.action,
            createdAt=created_at,
            eventType=model.event_type,
            source=model.source,
            elementKey=model.element_key,
            elementName=model.element_name,
            previousState=model.previous_state,
            newState=model.new_state,
            description=model.description,
            rawPayload=model.payload_json,
        )


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
