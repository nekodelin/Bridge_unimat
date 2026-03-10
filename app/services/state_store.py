import asyncio
from datetime import datetime

from app.models import ModuleGroupConfig
from app.schemas import AggregatesSnapshot, ChannelState, StateSnapshot, SummarySnapshot

FAULT_STATUSES = {"fault"}
NORMAL_STATUSES = {"normal"}


class StateStore:
    def __init__(
        self,
        initial_channels: list[ChannelState],
        groups: dict[str, ModuleGroupConfig],
    ) -> None:
        self._lock = asyncio.Lock()
        self._channels_by_key = {item.channelKey: item for item in initial_channels}
        self._channel_order = [item.channelKey for item in initial_channels]
        self._groups = groups

        self._timestamp: datetime | None = None
        self._source = "mqtt"
        self._board: str | None = None
        self._module: str | None = None
        self._raw: dict[str, int | None] | None = None
        self._topics: dict[str, str | None] = {}
        self._act: dict[str, bool | None] = {"tifon": None}

    async def get_snapshot(self) -> StateSnapshot:
        async with self._lock:
            return self._build_snapshot_locked()

    async def get_channels(self) -> list[ChannelState]:
        async with self._lock:
            return [self._channels_by_key[key].model_copy(deep=True) for key in self._channel_order]

    async def apply_board_update(
        self,
        channels: list[ChannelState],
        raw: dict[str, int | None],
        timestamp: datetime,
        source: str,
        board: str | None,
        module: str | None,
        topic: str | None = None,
    ) -> tuple[StateSnapshot, list[ChannelState], dict[str, str | None], bool]:
        async with self._lock:
            changed_channels: list[ChannelState] = []
            previous_states: dict[str, str | None] = {}
            for channel in channels:
                prev = self._channels_by_key.get(channel.channelKey)
                if prev is None:
                    self._channels_by_key[channel.channelKey] = channel
                    self._channel_order.append(channel.channelKey)
                    changed_channels.append(channel.model_copy(deep=True))
                    previous_states[channel.channelKey] = None
                    continue

                if (
                    prev.status != channel.status
                    or prev.input != channel.input
                    or prev.output != channel.output
                    or prev.diagnostic != channel.diagnostic
                    or prev.isFault != channel.isFault
                ):
                    changed_channels.append(channel.model_copy(deep=True))
                    previous_states[channel.channelKey] = prev.status

                self._channels_by_key[channel.channelKey] = channel

            self._timestamp = timestamp
            self._source = source
            self._board = board
            self._module = module
            self._raw = raw
            if topic:
                self._topics["state"] = topic

            snapshot = self._build_snapshot_locked()
            return snapshot, changed_channels, previous_states, bool(changed_channels)

    async def apply_act_update(
        self,
        tifon_value: bool,
        timestamp: datetime,
        topic: str | None = None,
    ) -> tuple[StateSnapshot, bool]:
        async with self._lock:
            changed = self._act.get("tifon") != tifon_value
            self._act["tifon"] = tifon_value
            self._timestamp = timestamp
            if topic:
                self._topics["act"] = topic
            snapshot = self._build_snapshot_locked()
            return snapshot, changed

    def _build_snapshot_locked(self) -> StateSnapshot:
        channels = [self._channels_by_key[key].model_copy(deep=True) for key in self._channel_order]
        module_statuses = self._calculate_module_statuses(channels)
        page_statuses = self._calculate_page_statuses(module_statuses)

        fault_count = sum(1 for item in channels if item.status in FAULT_STATUSES)
        warning_count = sum(1 for item in channels if item.status == "unknown")
        normal_count = sum(1 for item in channels if item.status in NORMAL_STATUSES)

        module_status = "inactive"
        if self._module and self._module in module_statuses:
            module_status = module_statuses[self._module]
        elif module_statuses:
            module_status = _merge_status(list(module_statuses.values()))

        summary = SummarySnapshot(
            totalChannels=len(channels),
            faultCount=fault_count,
            warningCount=warning_count,
            normalCount=normal_count,
            moduleStatus=module_status,
        )
        aggregates = AggregatesSnapshot(modules=module_statuses, pages=page_statuses)

        return StateSnapshot(
            timestamp=self._timestamp,
            updatedAt=self._timestamp,
            source=self._source,
            board=self._board,
            module=self._module,
            raw=self._raw.copy() if self._raw else None,
            topics=self._topics.copy(),
            faultCount=fault_count,
            warningCount=warning_count,
            normalCount=normal_count,
            channels=channels,
            decodedChannels=[item.model_copy(deep=True) for item in channels],
            summary=summary,
            aggregates=aggregates,
            act=self._act.copy(),
        )

    def _calculate_module_statuses(self, channels: list[ChannelState]) -> dict[str, str]:
        grouped: dict[str, list[ChannelState]] = {}
        for channel in channels:
            grouped.setdefault(channel.module, []).append(channel)

        result: dict[str, str] = {}
        for module_name, module_channels in grouped.items():
            statuses = [channel.status for channel in module_channels]
            result[module_name] = _merge_status(statuses)
        return result

    def _calculate_page_statuses(self, module_statuses: dict[str, str]) -> dict[str, str]:
        page_statuses: dict[str, str] = {}
        for group_name, group_cfg in self._groups.items():
            statuses = [module_statuses.get(module_name, "inactive") for module_name in group_cfg.modules]
            page_statuses[group_name] = _merge_status(statuses)
        return page_statuses


def _merge_status(statuses: list[str]) -> str:
    if not statuses:
        return "inactive"
    if any(status in FAULT_STATUSES.union({"error"}) for status in statuses):
        return "error"
    if all(status == "inactive" for status in statuses):
        return "inactive"
    if any(status == "unknown" for status in statuses):
        return "unknown"
    if all(status in NORMAL_STATUSES.union({"inactive"}) for status in statuses):
        return "normal"
    return "unknown"
