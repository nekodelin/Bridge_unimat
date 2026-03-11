from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ChannelStatus = Literal[
    "normal",
    "fault",
    "active",
    "normal_on",
    "normal_off",
    "open_circuit",
    "breakage",
    "short_circuit",
    "fault_break",
    "fault_short",
    "inactive",
    "unknown",
]
SeverityLevel = Literal["info", "warning", "error"]
FaultType = Literal["break", "short", "unknown"]
ComputedStatus = Literal["normal", "break", "short", "unknown"]
ConnectionState = Literal["ok", "warn", "error", "unknown"]
DiagnosisSeverity = Literal["ok", "warn", "error"]


class ChannelState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channelKey: str
    channelIndex: int
    signalId: str
    title: str
    purpose: str
    photoIndex: int | None = None
    board: str
    unit: str | None = None
    module: str
    logicalChannel: str | None = None
    rawChannel: str | None = None
    topic: str | None = None
    input: int
    output: int
    diagnostic: int
    status: ChannelStatus
    statusCode: ComputedStatus | None = None
    statusLabel: str | None = None
    stateLabel: str
    stateText: str | None = None
    label: str | None = None
    faultType: FaultType | None = None
    faultText: str | None = None
    inBit: int | None = None
    outBit: int | None = None
    diagBit: int | None = None
    stateTuple: list[int] | None = None
    yellow_led: bool | None = None
    red_led: bool | None = None
    message: str
    reason: str | None = None
    cause: str | None = None
    action: str | None = None
    severity: SeverityLevel
    fault: bool | None = None
    isFault: bool
    raw: dict[str, int] | None = None
    rawBits: dict[str, int] | None = None
    updatedAt: datetime | None = None


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


class ConnectionStatusItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    state: ConnectionState
    details: str | None = None
    updatedAt: datetime | None = None


class ConnectionDiagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    problemTitle: str
    recommendedAction: str
    severity: DiagnosisSeverity


class StateSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: datetime | None = None
    updatedAt: datetime | None = None
    source: str = "mqtt"
    board: str | None = None
    module: str | None = None
    raw: dict[str, int | None] | None = None
    topics: dict[str, str | None] = Field(default_factory=dict)
    faultCount: int = 0
    warningCount: int = 0
    normalCount: int = 0
    channels: list[ChannelState] = Field(default_factory=list)
    decodedChannels: list[ChannelState] = Field(default_factory=list)
    connectionStatuses: list[ConnectionStatusItem] = Field(default_factory=list)
    problemTitle: str = "Недостаточно данных для диагностики"
    recommendedAction: str = "Проверить поступление телеметрии и состояние соединения"
    severity: DiagnosisSeverity = "warn"
    connectionDiagnosis: ConnectionDiagnosis | None = None
    lastSuccessfulExchangeAt: datetime | None = None
    lastDataAt: datetime | None = None
    dataAgeSec: int | None = None
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
    createdAt: datetime | None = None
    eventType: Literal["state_change", "auth", "system"]
    source: str
    elementKey: str | None = None
    elementName: str | None = None
    previousState: str | None = None
    newState: str | None = None
    description: str | None = None
    rawPayload: dict[str, Any] | None = None


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


class AuthLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: str


class AuthMeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    username: str
    createdAt: datetime


class AuthLoginResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class AuthLogoutResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
