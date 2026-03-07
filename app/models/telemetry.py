from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt


class BoardPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    in_: StrictInt = Field(alias="in")
    inversed: StrictInt
    out: StrictInt

    def to_raw_dict(self) -> dict[str, int]:
        return {
            "in": int(self.in_),
            "inversed": int(self.inversed),
            "out": int(self.out),
        }


class ActPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tifon: StrictBool

    def to_raw_dict(self) -> dict[str, bool]:
        return {"tifon": bool(self.tifon)}


class ChannelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channelKey: str
    channelIndex: int
    signalId: str
    purpose: str
    board: str
    module: str
    photoIndex: int | None = None
    sourceTopic: str


class EventTextConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eventTitle: str
    purpose: str
    breakageCause: str
    shortCause: str
    action: str


class ModuleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    board: str
    title: str


class ModuleGroupConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    modules: list[str]
