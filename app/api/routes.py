from fastapi import APIRouter, Query, Request, status
from fastapi.responses import JSONResponse

from app.schemas import (
    ActCommandRequest,
    ActCommandResponse,
    ConfigResponse,
    HealthResponse,
    JournalResponse,
    StateSnapshot,
)

router = APIRouter(prefix="/api", tags=["api"])


def _runtime(request: Request):
    return request.app.state.runtime


@router.get("/health", response_model=HealthResponse)
async def get_health(request: Request) -> HealthResponse:
    runtime = _runtime(request)
    mqtt_connected = bool(getattr(request.app.state, "mqtt_connected", False))
    payload = await runtime.build_health(mqtt_connected=mqtt_connected)
    return HealthResponse.model_validate(payload)


@router.get("/state", response_model=StateSnapshot)
async def get_state(request: Request) -> StateSnapshot:
    runtime = _runtime(request)
    return await runtime.get_snapshot()


@router.get("/debug/bits")
async def get_debug_bits(request: Request):
    runtime = _runtime(request)
    report = await runtime.get_debug_bits_report()
    if report is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": "No board payload has been received yet"},
        )
    return report


@router.get("/debug/last-payload")
async def get_last_payload(request: Request):
    runtime = _runtime(request)
    payload = await runtime.get_last_raw_mqtt_payload()
    if payload is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": "No MQTT payload has been received yet"},
        )
    return payload


@router.get("/channels")
async def get_channels(request: Request):
    runtime = _runtime(request)
    channels = await runtime.get_channels()
    return [item.model_dump(mode="json") for item in channels]


@router.get("/journal", response_model=JournalResponse)
async def get_journal(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
) -> JournalResponse:
    runtime = _runtime(request)
    items = await runtime.get_journal(limit=limit)
    return JournalResponse(items=items)


@router.get("/config", response_model=ConfigResponse)
async def get_config(request: Request) -> ConfigResponse:
    runtime = _runtime(request)
    config_bundle = runtime.config_bundle
    return ConfigResponse(
        signalMap=config_bundle.signal_map_raw,
        eventTexts=config_bundle.event_texts_raw,
        moduleMap=config_bundle.module_map_raw,
    )


@router.post("/act/tifon", response_model=ActCommandResponse)
async def post_tifon(request: Request, body: ActCommandRequest):
    runtime = _runtime(request)
    ok, error = await runtime.publish_tifon(body.value)
    if not ok:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=ActCommandResponse(ok=False, error=error or "publish failed").model_dump(),
        )
    return ActCommandResponse(ok=True, error=None)
