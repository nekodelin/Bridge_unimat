from dataclasses import dataclass
from datetime import datetime

from app.schemas import ConnectionStatusItem

DATA_OK_AGE_SEC = 10
DATA_WARN_AGE_SEC = 30
REALTIME_OK_AGE_SEC = 30
REALTIME_WARN_AGE_SEC = 90

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_ERROR = "error"
STATUS_UNKNOWN = "unknown"

LABEL_BOARD_ONLINE = "\u041f\u043b\u0430\u0442\u0430 \u043e\u043d\u043b\u0430\u0439\u043d"
LABEL_INCOMING_DATA = "\u0415\u0441\u0442\u044c \u0432\u0445\u043e\u0434\u044f\u0449\u0438\u0435 \u0434\u0430\u043d\u043d\u044b\u0435"
LABEL_BACKEND_AVAILABLE = "\u0411\u0435\u043a\u0435\u043d\u0434 \u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d"
LABEL_INTERFACE_UPDATES = "\u041e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u044f \u0434\u043e\u0445\u043e\u0434\u044f\u0442 \u0434\u043e \u0438\u043d\u0442\u0435\u0440\u0444\u0435\u0439\u0441\u0430"
LABEL_DATA_FRESH = "\u0414\u0430\u043d\u043d\u044b\u0435 \u0441\u0432\u0435\u0436\u0438\u0435"


@dataclass(slots=True, frozen=True)
class ConnectionStatusContext:
    now: datetime
    mock_mode: bool
    mqtt_connected: bool
    last_data_at: datetime | None
    last_successful_exchange_at: datetime | None
    realtime_clients: int
    last_realtime_publish_at: datetime | None


def evaluate_connection_statuses(
    context: ConnectionStatusContext,
) -> tuple[list[ConnectionStatusItem], int | None]:
    data_age_sec = _age_seconds(context.now, context.last_data_at)
    publish_age_sec = _age_seconds(context.now, context.last_realtime_publish_at)

    statuses = [
        _build_board_online_status(context=context, data_age_sec=data_age_sec),
        _build_incoming_data_status(context=context, data_age_sec=data_age_sec),
        _build_backend_available_status(context=context),
        _build_interface_updates_status(context=context, publish_age_sec=publish_age_sec),
        _build_data_fresh_status(context=context, data_age_sec=data_age_sec),
    ]
    return statuses, data_age_sec


def _build_board_online_status(
    *,
    context: ConnectionStatusContext,
    data_age_sec: int | None,
) -> ConnectionStatusItem:
    if context.mock_mode:
        return ConnectionStatusItem(
            key="board_online",
            label=LABEL_BOARD_ONLINE,
            state=STATUS_UNKNOWN,
            details="MOCK_MODE=true; physical board reachability is not evaluated.",
            updatedAt=context.last_data_at,
        )

    if data_age_sec is None:
        return ConnectionStatusItem(
            key="board_online",
            label=LABEL_BOARD_ONLINE,
            state=STATUS_UNKNOWN,
            details="No board telemetry has been received yet.",
            updatedAt=None,
        )

    if not context.mqtt_connected:
        state = STATUS_WARN if data_age_sec <= DATA_WARN_AGE_SEC else STATUS_ERROR
        return ConnectionStatusItem(
            key="board_online",
            label=LABEL_BOARD_ONLINE,
            state=state,
            details=f"MQTT disconnected, last board data age {data_age_sec}s.",
            updatedAt=context.last_data_at,
        )

    return ConnectionStatusItem(
        key="board_online",
        label=LABEL_BOARD_ONLINE,
        state=_age_to_state(data_age_sec, ok_age=DATA_OK_AGE_SEC, warn_age=DATA_WARN_AGE_SEC),
        details=f"MQTT connected, last board data age {data_age_sec}s.",
        updatedAt=context.last_data_at,
    )


def _build_incoming_data_status(
    *,
    context: ConnectionStatusContext,
    data_age_sec: int | None,
) -> ConnectionStatusItem:
    if data_age_sec is None:
        state = STATUS_UNKNOWN
        details = "No incoming telemetry timestamp yet."
    else:
        state = _age_to_state(data_age_sec, ok_age=DATA_OK_AGE_SEC, warn_age=DATA_WARN_AGE_SEC)
        details = f"Last incoming telemetry packet age {data_age_sec}s."

    return ConnectionStatusItem(
        key="incoming_data",
        label=LABEL_INCOMING_DATA,
        state=state,
        details=details,
        updatedAt=context.last_data_at,
    )


def _build_backend_available_status(*, context: ConnectionStatusContext) -> ConnectionStatusItem:
    return ConnectionStatusItem(
        key="backend_available",
        label=LABEL_BACKEND_AVAILABLE,
        state=STATUS_OK,
        details="Backend process is running and produced this payload.",
        updatedAt=context.now,
    )


def _build_interface_updates_status(
    *,
    context: ConnectionStatusContext,
    publish_age_sec: int | None,
) -> ConnectionStatusItem:
    if context.realtime_clients <= 0:
        return ConnectionStatusItem(
            key="interface_updates",
            label=LABEL_INTERFACE_UPDATES,
            state=STATUS_UNKNOWN,
            details="No active state websocket clients; delivery cannot be confirmed.",
            updatedAt=context.last_realtime_publish_at,
        )

    if publish_age_sec is None:
        return ConnectionStatusItem(
            key="interface_updates",
            label=LABEL_INTERFACE_UPDATES,
            state=STATUS_WARN,
            details=f"{context.realtime_clients} client(s) connected, no realtime publish yet.",
            updatedAt=None,
        )

    state = _age_to_state(
        publish_age_sec,
        ok_age=REALTIME_OK_AGE_SEC,
        warn_age=REALTIME_WARN_AGE_SEC,
    )
    return ConnectionStatusItem(
        key="interface_updates",
        label=LABEL_INTERFACE_UPDATES,
        state=state,
        details=(
            f"Last realtime publish age {publish_age_sec}s, "
            f"clients={context.realtime_clients}."
        ),
        updatedAt=context.last_realtime_publish_at,
    )


def _build_data_fresh_status(
    *,
    context: ConnectionStatusContext,
    data_age_sec: int | None,
) -> ConnectionStatusItem:
    if data_age_sec is None:
        state = STATUS_UNKNOWN
        details = "No board data timestamp yet."
    else:
        state = _age_to_state(data_age_sec, ok_age=DATA_OK_AGE_SEC, warn_age=DATA_WARN_AGE_SEC)
        details = f"Current board data age {data_age_sec}s."

    return ConnectionStatusItem(
        key="data_fresh",
        label=LABEL_DATA_FRESH,
        state=state,
        details=details,
        updatedAt=context.last_data_at,
    )


def _age_to_state(age_sec: int | None, *, ok_age: int, warn_age: int) -> str:
    if age_sec is None:
        return STATUS_UNKNOWN
    if age_sec <= ok_age:
        return STATUS_OK
    if age_sec <= warn_age:
        return STATUS_WARN
    return STATUS_ERROR


def _age_seconds(now_value: datetime, value: datetime | None) -> int | None:
    if value is None:
        return None
    age = int((now_value - value).total_seconds())
    return age if age >= 0 else 0
