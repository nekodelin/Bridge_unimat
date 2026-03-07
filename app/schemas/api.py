from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ChannelStatus = Literal["normal", "active", "breakage", "short_circuit", "inactive", "unknown"]
SeverityLevel = Literal["info", "warning", "error"]


class ChannelState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channelKey: str
    channelIndex: int
    signalId: str
    title: str
    purpose: str
    photoIndex: int | None = None
    board: str
    module: str
    input: int
    output: int
    diagnostic: int
    status: ChannelStatus
    stateLabel: str
    message: str
    cause: str | None = None
    action: str | None = None
    severity: SeverityLevel
    isFault: bool


class SummarySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    totalChannels: int
    faultCount: int
    warningCount: int
    normalCount: int
    moduleStatus: str


class AggregatesSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    modules: dict[str, str]
    pages: dict[str, str]


class StateSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: datetime | None = None
    source: str = "mqtt"
    board: str | None = None
    module: str | None = None
    raw: dict[str, int] | None = None
    channels: list[ChannelState] = Field(default_factory=list)
    summary: SummarySnapshot
    aggregates: AggregatesSnapshot
    act: dict[str, bool | None] = Field(default_factory=lambda: {"tifon": None})


class JournalEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    timestamp: datetime
    signalId: str | None = None
    channelKey: str | None = None
    module: str | None = None
    board: str | None = None
    status: str | None = None
    level: SeverityLevel | Literal["info"]
    title: str
    message: str
    action: str | None = None


class JournalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[JournalEntry]


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    appEnv: str
    mqttConnected: bool
    mockMode: bool


class ActCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: bool


class ActCommandResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    error: str | None = None


class ConfigResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signalMap: list[dict[str, Any]]
    eventTexts: dict[str, dict[str, Any]]
    moduleMap: dict[str, Any]
