import asyncio
import logging
from datetime import UTC, datetime

from app.config import ConfigBundle, Settings
from app.models import ActPayload, BoardPayload
from app.schemas import ConnectionDiagnosis, ChannelState, ConnectionStatusItem, JournalEntry, StateSnapshot
from app.services.broadcaster import WebSocketBroadcaster
from app.services.connection_status import (
    ConnectionStatusContext,
    build_connection_diagnosis,
    evaluate_connection_statuses,
)
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
        journal_broadcaster: WebSocketBroadcaster | None = None,
    ) -> None:
        self.settings = settings
        self.config_bundle = config_bundle
        self.decoder = decoder
        self.state_store = state_store
        self.journal = journal
        self.broadcaster = broadcaster
        self.journal_broadcaster = journal_broadcaster or broadcaster
        self.mqtt_client = None
        self._debug_lock = asyncio.Lock()
        self._last_board_payload: BoardPayload | None = None
        self._last_board_topic: str | None = None
        self._last_board_source: str = "mqtt"
        self._last_board_timestamp: datetime | None = None
        self._last_raw_mqtt_payload: str | None = None
        self._last_raw_mqtt_topic: str | None = None
        self._last_raw_mqtt_timestamp: datetime | None = None
        self._mqtt_connected = False
        self._last_data_at: datetime | None = None
        self._last_successful_exchange_at: datetime | None = None
        self._state_ws_clients = 0
        self._last_realtime_publish_at: datetime | None = None

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

        raw_payload = payload.to_raw_dict()

        board = decoded_channels[0].board if decoded_channels else None
        module = decoded_channels[0].module if decoded_channels else None
        snapshot, changed_channels, previous_states, changed = await self.state_store.apply_board_update(
            channels=decoded_channels,
            raw=raw_payload,
            timestamp=timestamp,
            source=source,
            board=board,
            module=module,
            topic=topic,
        )
        await self._mark_data_exchange(timestamp)
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

        journal_items = await self._append_channel_journal(
            channels=changed_channels,
            previous_states=previous_states,
            source=source,
            raw_payload=raw_payload,
        )
        await self._broadcast_state_update(snapshot)
        for item in journal_items:
            await self._broadcast_journal_entry(item)

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
        await self._broadcast_state_update(snapshot)

    async def handle_connection_event(self, event_name: str) -> None:
        if event_name == "mqtt_connected":
            await self._set_mqtt_connected(True)
        elif event_name == "mqtt_disconnected":
            await self._set_mqtt_connected(False)

        if event_name == "mqtt_connected":
            entry = await self.journal.append_system(
                title="MQTT connected",
                message="MQTT broker connection established",
            )
        elif event_name == "mqtt_disconnected":
            entry = await self.journal.append_system(
                title="MQTT disconnected",
                message="MQTT broker connection lost",
                level="warning",
            )
        else:
            entry = await self.journal.append_system(
                title="MQTT event",
                message=event_name,
            )
        await self._broadcast_journal_entry(entry)

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
        snapshot = await self.state_store.get_snapshot()
        return await self._enrich_snapshot(snapshot)

    async def get_channels(self) -> list[ChannelState]:
        return await self.state_store.get_channels()

    async def get_journal(
        self,
        *,
        limit: int,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list[JournalEntry]:
        return await self.journal.list_recent(limit=limit, date_from=date_from, date_to=date_to)

    async def export_journal_text(
        self,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> str:
        items = await self.journal.list_for_export(date_from=date_from, date_to=date_to)
        return self._render_journal_export(items=items, date_from=date_from, date_to=date_to)

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
        await self._broadcast_journal_entry(entry)
        return entry

    async def append_auth_event(self, *, username: str, action: str) -> JournalEntry:
        entry = await self.journal.append_auth(username=username, action=action)
        await self._broadcast_journal_entry(entry)
        return entry

    async def websocket_connected(self, total_clients: int) -> None:
        await self._set_state_ws_clients(total_clients)
        await self._mark_realtime_publish()
        await self.append_system_event(
            title="WebSocket client connected",
            message=f"Client connected. clients={total_clients}",
        )

    async def websocket_disconnected(self, total_clients: int) -> None:
        await self._set_state_ws_clients(total_clients)
        await self.append_system_event(
            title="WebSocket client disconnected",
            message=f"Client disconnected. clients={total_clients}",
        )

    async def heartbeat(self) -> None:
        now_value = now_utc()
        connection_payload = await self._build_connection_payload(now_value=now_value)
        payload = {
            "type": "heartbeat",
            "timestamp": datetime.now().astimezone().isoformat(),
            **connection_payload,
        }
        await self.broadcaster.broadcast(payload)
        await self._mark_realtime_publish(timestamp=now_value)

    async def _append_channel_journal(
        self,
        *,
        channels: list[ChannelState],
        previous_states: dict[str, str | None],
        source: str,
        raw_payload: dict[str, int | None],
    ) -> list[JournalEntry]:
        entries: list[JournalEntry] = []
        for channel in channels:
            previous_state = previous_states.get(channel.channelKey)
            entry = await self.journal.append_state_change(
                source=source,
                channel=channel,
                previous_state=previous_state,
                raw_payload=raw_payload,
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

    @staticmethod
    def _render_journal_export(
        *,
        items: list[JournalEntry],
        date_from: datetime | None,
        date_to: datetime | None,
    ) -> str:
        generated_at = now_utc().astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        from_label = BridgeRuntime._format_export_date(date_from)
        to_label = BridgeRuntime._format_export_date(date_to)

        lines = [
            "UNIMAT Journal Export",
            f"Generated at: {generated_at}",
            f"Filter date_from: {from_label}",
            f"Filter date_to: {to_label}",
            "",
        ]
        if not items:
            lines.append("No journal entries found for the selected period.")
            lines.append("")
            return "\n".join(lines)

        for item in items:
            timestamp = item.createdAt or item.timestamp
            ts = timestamp.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
            element_key = item.elementKey or item.channelKey or "-"
            element_name = item.elementName or item.title or "-"
            prev_state = item.previousState or "-"
            new_state = item.newState or item.status or "-"
            description = item.description or item.message or "-"
            line = (
                f"[{ts}] {item.eventType} | {item.source} | {element_key} | {element_name} | "
                f"{prev_state} -> {new_state} | {description}"
            )
            lines.append(line)
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _format_export_date(value: datetime | None) -> str:
        if value is None:
            return "-"
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    async def _broadcast_journal_entry(self, entry: JournalEntry) -> None:
        await self.journal_broadcaster.broadcast(
            {
                "type": "journal_append",
                "data": entry.model_dump(mode="json"),
            }
        )

    async def _broadcast_state_update(self, snapshot: StateSnapshot) -> None:
        enriched_snapshot = await self._enrich_snapshot(snapshot)
        await self.broadcaster.broadcast(
            {
                "type": "state_update",
                "data": enriched_snapshot.model_dump(mode="json"),
            }
        )
        await self._mark_realtime_publish()

    async def _enrich_snapshot(self, snapshot: StateSnapshot) -> StateSnapshot:
        statuses, last_exchange_at, last_data_at, data_age_sec, diagnosis = (
            await self._build_connection_status_block()
        )
        return snapshot.model_copy(
            update={
                "connectionStatuses": statuses,
                "problemTitle": diagnosis.problemTitle,
                "recommendedAction": diagnosis.recommendedAction,
                "severity": diagnosis.severity,
                "connectionDiagnosis": diagnosis,
                "lastSuccessfulExchangeAt": last_exchange_at,
                "lastDataAt": last_data_at,
                "dataAgeSec": data_age_sec,
            },
            deep=True,
        )

    async def _build_connection_payload(self, *, now_value: datetime) -> dict:
        statuses, last_exchange_at, last_data_at, data_age_sec, diagnosis = (
            await self._build_connection_status_block(now_value=now_value)
        )
        return {
            "connectionStatuses": [item.model_dump(mode="json") for item in statuses],
            "problemTitle": diagnosis.problemTitle,
            "recommendedAction": diagnosis.recommendedAction,
            "severity": diagnosis.severity,
            "connectionDiagnosis": diagnosis.model_dump(mode="json"),
            "lastSuccessfulExchangeAt": last_exchange_at.isoformat() if last_exchange_at else None,
            "lastDataAt": last_data_at.isoformat() if last_data_at else None,
            "dataAgeSec": data_age_sec,
        }

    async def _build_connection_status_block(
        self,
        *,
        now_value: datetime | None = None,
    ) -> tuple[list[ConnectionStatusItem], datetime | None, datetime | None, int | None, ConnectionDiagnosis]:
        now_ts = now_value or now_utc()
        realtime_clients = await self.broadcaster.client_count()
        async with self._debug_lock:
            self._state_ws_clients = realtime_clients
            context = ConnectionStatusContext(
                now=now_ts,
                mock_mode=self.settings.mock_mode,
                mqtt_connected=self._mqtt_connected,
                last_data_at=self._last_data_at,
                last_successful_exchange_at=self._last_successful_exchange_at,
                realtime_clients=realtime_clients,
                last_realtime_publish_at=self._last_realtime_publish_at,
            )
            last_exchange_at = self._last_successful_exchange_at
            last_data_at = self._last_data_at

        statuses, data_age_sec = evaluate_connection_statuses(context)
        diagnosis = build_connection_diagnosis(statuses)
        return statuses, last_exchange_at, last_data_at, data_age_sec, diagnosis

    async def _mark_data_exchange(self, timestamp: datetime) -> None:
        async with self._debug_lock:
            self._last_data_at = timestamp
            self._last_successful_exchange_at = timestamp

    async def _set_mqtt_connected(self, value: bool) -> None:
        async with self._debug_lock:
            self._mqtt_connected = value

    async def _set_state_ws_clients(self, total_clients: int) -> None:
        async with self._debug_lock:
            self._state_ws_clients = max(int(total_clients), 0)

    async def _mark_realtime_publish(self, timestamp: datetime | None = None) -> None:
        clients = await self.broadcaster.client_count()
        async with self._debug_lock:
            self._state_ws_clients = clients
            self._last_realtime_publish_at = timestamp or now_utc()
