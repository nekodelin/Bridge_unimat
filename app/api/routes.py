from datetime import UTC, date, datetime, time

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response

from app.api.deps import get_current_user
from app.schemas import (
    ActCommandRequest,
    ActCommandResponse,
    ConfigResponse,
    HealthResponse,
    JournalResponse,
    StateSnapshot,
)
from app.services import AuthenticatedUser
from app.utils import now_utc

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
    _current_user: AuthenticatedUser = Depends(get_current_user),
    limit: int = Query(default=100, ge=1, le=1000),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
) -> JournalResponse:
    runtime = _runtime(request)
    parsed_from = _parse_date(date_from, end_of_day=False, parameter_name="date_from")
    parsed_to = _parse_date(date_to, end_of_day=True, parameter_name="date_to")
    if parsed_from and parsed_to and parsed_from > parsed_to:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="date_from must be less than or equal to date_to",
        )
    items = await runtime.get_journal(limit=limit, date_from=parsed_from, date_to=parsed_to)
    return JournalResponse(items=items)


@router.get("/journal/export")
async def export_journal(
    request: Request,
    _current_user: AuthenticatedUser = Depends(get_current_user),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
):
    runtime = _runtime(request)
    parsed_from = _parse_date(date_from, end_of_day=False, parameter_name="date_from")
    parsed_to = _parse_date(date_to, end_of_day=True, parameter_name="date_to")
    if parsed_from and parsed_to and parsed_from > parsed_to:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="date_from must be less than or equal to date_to",
        )

    try:
        text_data = await runtime.export_journal_text(date_from=parsed_from, date_to=parsed_to)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Export failed: {exc}",
        ) from exc

    filename = f"journal_export_{now_utc().strftime('%Y%m%d_%H%M%S')}.txt"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=text_data.encode("utf-8"), media_type="text/plain; charset=utf-8", headers=headers)


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


def _parse_date(value: str | None, *, end_of_day: bool, parameter_name: str) -> datetime | None:
    if value is None or not value.strip():
        return None

    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        parsed_date = _parse_date_only(raw)
        if parsed_date is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid {parameter_name}. Use ISO datetime or YYYY-MM-DD.",
            )
        parsed_time = time.max if end_of_day else time.min
        parsed = datetime.combine(parsed_date, parsed_time)

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_date_only(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
