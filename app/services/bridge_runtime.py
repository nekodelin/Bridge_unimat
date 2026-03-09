import asyncio
import logging
from datetime import datetime

from app.config import ConfigBundle, Settings
from app.models import ActPayload, BoardPayload
from app.schemas import ChannelState, JournalEntry, StateSnapshot
from app.services.broadcaster import WebSocketBroadcaster
from app.services.decoder import DecoderService
from app.services.journal import EventJournalService
from app.services.state_store import StateStore
from app.utils import now_utc

logger = logging.getLogger("unimat.runtime")


class BridgeRuntime:
    def __init__(
        self,
        settings: Settings,
        config_bundle: ConfigBundle,
        decoder: DecoderService,
        state_store: StateStore,
        journal: EventJournalService,
        broadcaster: WebSocketBroadcaster,
    ) -> None:
        self.settings = settings
        self.config_bundle = config_bundle
        self.decoder = decoder
        self.state_store = state_store
        self.journal = journal
        self.broadcaster = broadcaster
        self.mqtt_client = None
        self._debug_lock = asyncio.Lock()
        self._last_board_payload: BoardPayload | None = None
        self._last_board_topic: str | None = None
        self._last_board_source: str = "mqtt"
        self._last_board_timestamp: datetime | None = None
        self._last_raw_mqtt_payload: str | None = None
        self._last_raw_mqtt_topic: str | None = None
        self._last_raw_mqtt_timestamp: datetime | None = None

    def attach_mqtt_client(self, mqtt_client: object) -> None:
        self.mqtt_client = mqtt_client

    async def register_raw_mqtt_message(
        self,
        topic: str,
        payload: str,
        timestamp: datetime | None = None,
    ) -> None:
        async with self._debug_lock:
            self._last_raw_mqtt_topic = topic
            self._last_raw_mqtt_payload = payload
            self._last_raw_mqtt_timestamp = timestamp or now_utc()

    async def process_board_payload(self, payload: BoardPayload, topic: str, source: str = "mqtt") -> None:
        timestamp = now_utc()
        await self._remember_last_board_payload(payload=payload, topic=topic, source=source, timestamp=timestamp)

        decoded_channels = self.decoder.decode_board_payload(payload, topic=topic, updated_at=timestamp)
        if not decoded_channels:
            logger.warning("No mapped channels for topic=%s", topic)
            return

        board = decoded_channels[0].board if decoded_channels else None
        module = decoded_channels[0].module if decoded_channels else None
        snapshot, changed_channels, changed = await self.state_store.apply_board_update(
            channels=decoded_channels,
            raw=payload.to_raw_dict(),
            timestamp=timestamp,
            source=source,
            board=board,
            module=module,
            topic=topic,
        )
        logger.info(
            "Decoded payload summary topic=%s source=%s moduleStatus=%s faultCount=%s warningCount=%s normalCount=%s",
            topic,
            source,
            snapshot.summary.moduleStatus,
            snapshot.summary.faultCount,
            snapshot.summary.warningCount,
            snapshot.summary.normalCount,
        )

        if not changed:
            return

        journal_items = await self._append_channel_journal(changed_channels)
        await self.broadcaster.broadcast(
            {
                "type": "state_update",
                "data": snapshot.model_dump(mode="json"),
            }
        )
        for item in journal_items:
            await self.broadcaster.broadcast(
                {
                    "type": "journal_append",
                    "data": item.model_dump(mode="json"),
                }
            )

    async def process_act_payload(
        self,
        payload: ActPayload,
        source: str = "mqtt",
        topic: str | None = None,
    ) -> None:
        snapshot, changed = await self.state_store.apply_act_update(
            tifon_value=bool(payload.tifon),
            timestamp=now_utc(),
            topic=topic,
        )
        if not changed:
            return
        await self.broadcaster.broadcast(
            {
                "type": "state_update",
                "data": snapshot.model_dump(mode="json"),
            }
        )

    async def handle_connection_event(self, event_name: str) -> None:
        if event_name == "mqtt_connected":
            entry = await self.journal.append_system(
                title="MQTT connected",
                message="Соединение с MQTT брокером установлено",
            )
        elif event_name == "mqtt_disconnected":
            entry = await self.journal.append_system(
                title="MQTT disconnected",
                message="Соединение с MQTT брокером потеряно",
                level="warning",
            )
        else:
            entry = await self.journal.append_system(
                title="MQTT event",
                message=event_name,
            )
        await self.broadcaster.broadcast({"type": "journal_append", "data": entry.model_dump(mode="json")})

    async def publish_tifon(self, value: bool) -> tuple[bool, str | None]:
        if self.settings.mock_mode:
            await self.process_act_payload(ActPayload(tifon=value), source="mock")
            return True, None

        if self.mqtt_client is None:
            return False, "mqtt client is not initialized"

        ok, error = self.mqtt_client.publish_tifon(value)  # type: ignore[attr-defined]
        if not ok:
            return False, error

        await self.process_act_payload(
            ActPayload(tifon=value),
            source="api",
            topic=self.settings.mqtt_topic_act,
        )
        return True, None

    async def build_health(self, mqtt_connected: bool) -> dict:
        return {
            "ok": True,
            "appEnv": self.settings.app_env,
            "mqttConnected": mqtt_connected,
            "mockMode": self.settings.mock_mode,
        }

    async def get_snapshot(self) -> StateSnapshot:
        return await self.state_store.get_snapshot()

    async def get_channels(self) -> list[ChannelState]:
        return await self.state_store.get_channels()

    async def get_journal(self, limit: int) -> list[JournalEntry]:
        return await self.journal.list_recent(limit=limit)

    async def get_last_raw_mqtt_payload(self) -> dict | None:
        async with self._debug_lock:
            if self._last_raw_mqtt_payload is None:
                return None
            return {
                "timestamp": self._last_raw_mqtt_timestamp,
                "topic": self._last_raw_mqtt_topic,
                "payload": self._last_raw_mqtt_payload,
            }

    async def get_debug_bits_report(self) -> dict | None:
        async with self._debug_lock:
            if self._last_board_payload is None or self._last_board_topic is None:
                return None
            payload = self._last_board_payload.model_copy(deep=True)
            topic = self._last_board_topic
            source = self._last_board_source
            timestamp = self._last_board_timestamp

        return self.decoder.build_debug_report(
            payload=payload,
            topic=topic,
            timestamp=timestamp,
            source=source,
        )

    async def append_system_event(
        self,
        title: str,
        message: str,
        *,
        level: str = "info",
    ) -> JournalEntry:
        entry = await self.journal.append_system(title=title, message=message, level=level)
        await self.broadcaster.broadcast({"type": "journal_append", "data": entry.model_dump(mode="json")})
        return entry

    async def websocket_connected(self, total_clients: int) -> None:
        await self.append_system_event(
            title="WebSocket client connected",
            message=f"Клиент подключен. clients={total_clients}",
        )

    async def websocket_disconnected(self, total_clients: int) -> None:
        await self.append_system_event(
            title="WebSocket client disconnected",
            message=f"Клиент отключен. clients={total_clients}",
        )

    async def heartbeat(self) -> None:
        payload = {
            "type": "heartbeat",
            "timestamp": datetime.now().astimezone().isoformat(),
        }
        await self.broadcaster.broadcast(payload)

    async def _append_channel_journal(self, channels: list[ChannelState]) -> list[JournalEntry]:
        entries: list[JournalEntry] = []
        for channel in channels:
            entry = await self.journal.append_channel(
                signal_id=channel.signalId,
                channel_key=channel.channelKey,
                module=channel.module,
                board=channel.board,
                status=channel.status,
                level=channel.severity,
                title=channel.title,
                message=channel.cause if channel.isFault and channel.cause else channel.message,
                action=channel.action,
            )
            entries.append(entry)
        return entries

    async def _remember_last_board_payload(
        self,
        payload: BoardPayload,
        topic: str,
        source: str,
        timestamp: datetime,
    ) -> None:
        async with self._debug_lock:
            self._last_board_payload = payload.model_copy(deep=True)
            self._last_board_topic = topic
            self._last_board_source = source
            self._last_board_timestamp = timestamp
