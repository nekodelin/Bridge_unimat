import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware

from app.api import router as api_router
from app.config import get_settings, load_config_bundle
from app.db import close_database, create_tables, get_session_factory, init_database
from app.models import ActPayload, BoardPayload
from app.mqtt import MQTTBridgeClient
from app.services import (
    AuthService,
    BridgeRuntime,
    DecoderService,
    EventJournalService,
    MockModeService,
    StateStore,
    WebSocketBroadcaster,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("unimat.app")

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database(settings.database_url)
    await create_tables()
    session_factory = get_session_factory()

    config_bundle = load_config_bundle()
    decoder = DecoderService(
        signal_map=config_bundle.signal_map,
        event_texts=config_bundle.event_texts,
    )
    initial_channels = decoder.default_inactive_channels()
    state_store = StateStore(initial_channels=initial_channels, groups=config_bundle.groups)
    journal = EventJournalService(session_factory=session_factory)
    broadcaster = WebSocketBroadcaster()
    journal_broadcaster = WebSocketBroadcaster()
    auth_service = AuthService(settings=settings, session_factory=session_factory)
    await auth_service.ensure_first_admin()

    runtime = BridgeRuntime(
        settings=settings,
        config_bundle=config_bundle,
        decoder=decoder,
        state_store=state_store,
        journal=journal,
        broadcaster=broadcaster,
        journal_broadcaster=journal_broadcaster,
    )

    app.state.runtime = runtime
    app.state.auth_service = auth_service
    app.state.mqtt_connected = False
    app.state.mqtt_client = None
    app.state.mock_service = None
    app.state.heartbeat_task = None

    await runtime.append_system_event(title="backend started", message="UNIMAT backend started")

    async def on_board_message(payload: BoardPayload, topic: str) -> None:
        source = "mock" if settings.mock_mode else "mqtt"
        await runtime.process_board_payload(payload=payload, topic=topic, source=source)

    async def on_act_message(payload: ActPayload, topic: str) -> None:
        await runtime.process_act_payload(payload=payload, source="mqtt", topic=topic)

    async def on_connection_event(event_name: str) -> None:
        if event_name == "mqtt_connected":
            app.state.mqtt_connected = True
        elif event_name == "mqtt_disconnected":
            app.state.mqtt_connected = False
        await runtime.handle_connection_event(event_name)

    async def on_raw_message(topic: str, payload: str, timestamp) -> None:
        await runtime.register_raw_mqtt_message(topic=topic, payload=payload, timestamp=timestamp)

    if settings.mock_mode:
        mock_service = MockModeService(on_board_payload=on_board_message, interval_sec=2.0)
        app.state.mock_service = mock_service
        await mock_service.start()
        await runtime.append_system_event(
            title="mock mode enabled",
            message="MOCK_MODE=true. MQTT disabled, synthetic telemetry started.",
        )
    else:
        mqtt_client = MQTTBridgeClient(
            settings=settings,
            on_board_message=on_board_message,
            on_act_message=on_act_message,
            on_connection_event=on_connection_event,
            on_raw_message=on_raw_message,
        )
        runtime.attach_mqtt_client(mqtt_client)
        app.state.mqtt_client = mqtt_client
        mqtt_client.start(asyncio.get_running_loop())

    app.state.heartbeat_task = asyncio.create_task(_heartbeat_loop(app))

    try:
        yield
    finally:
        heartbeat_task: asyncio.Task[None] | None = app.state.heartbeat_task
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

        mock_service: MockModeService | None = app.state.mock_service
        if mock_service is not None:
            await mock_service.stop()

        mqtt_client: MQTTBridgeClient | None = app.state.mqtt_client
        if mqtt_client is not None:
            mqtt_client.stop()

        await close_database()


async def _heartbeat_loop(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(settings.ws_heartbeat_sec)
        runtime: BridgeRuntime = app.state.runtime
        await runtime.heartbeat()


app = FastAPI(
    title="UNIMAT Monitoring Backend",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.websocket("/ws/state")
@app.websocket("/ws")
async def websocket_state(websocket: WebSocket) -> None:
    runtime: BridgeRuntime = websocket.app.state.runtime
    broadcaster: WebSocketBroadcaster = runtime.broadcaster

    total = await broadcaster.connect(websocket)
    snapshot = await runtime.get_snapshot()

    await websocket.send_json(
        {
            "type": "snapshot",
            "data": snapshot.model_dump(mode="json"),
        }
    )
    await runtime.websocket_connected(total_clients=total)

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message["type"] == "websocket.receive":
                await websocket.send_json({"type": "info", "message": "read-only"})
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    finally:
        total = await broadcaster.disconnect(websocket)
        await runtime.websocket_disconnected(total_clients=total)


@app.websocket("/ws/journal")
async def websocket_journal(websocket: WebSocket) -> None:
    runtime: BridgeRuntime = websocket.app.state.runtime
    auth_service: AuthService = websocket.app.state.auth_service
    broadcaster: WebSocketBroadcaster = runtime.journal_broadcaster

    token = _extract_ws_token(websocket)
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Not authenticated")
        return

    user = await auth_service.authenticate_token(token)
    if user is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid or expired token")
        return

    total = await broadcaster.connect(websocket)
    logger.info("Journal WebSocket connected user=%s clients=%s", user.username, total)

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message["type"] == "websocket.receive":
                await websocket.send_json({"type": "info", "message": "read-only"})
    except WebSocketDisconnect:
        logger.info("Journal WebSocket disconnected user=%s", user.username)
    finally:
        total = await broadcaster.disconnect(websocket)
        logger.info("Journal WebSocket clients=%s", total)


def _extract_ws_token(websocket: WebSocket) -> str | None:
    token = websocket.query_params.get("token") or websocket.query_params.get("access_token")
    if token:
        return token.strip() or None

    auth_header = websocket.headers.get("authorization")
    if not auth_header:
        return None

    scheme, _, value = auth_header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    clean_value = value.strip()
    return clean_value or None


@app.get("/health")
async def health_alias() -> dict[str, Any]:
    return {"ok": True}
