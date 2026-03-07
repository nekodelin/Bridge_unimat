from functools import lru_cache
from typing import Any

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = Field(default="local", alias="APP_ENV")
    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default=8000, alias="API_PORT")

    mqtt_host: str = Field(default="localhost", alias="MQTT_HOST")
    mqtt_port: int = Field(default=1883, alias="MQTT_PORT")
    mqtt_user: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MQTT_USER", "MQTT_USERNAME"),
    )
    mqtt_password: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MQTT_PASSWORD", "MQTT_PASS"),
    )
    mqtt_topic_state: str = Field(
        default="puma_board",
        validation_alias=AliasChoices("MQTT_TOPIC_STATE", "MQTT_SUB_TOPIC"),
    )
    mqtt_topic_act: str = Field(
        default="puma_board_act",
        validation_alias=AliasChoices("MQTT_TOPIC_ACT", "MQTT_PUB_TOPIC"),
    )
    mqtt_tls: bool = Field(default=False, alias="MQTT_TLS")

    frontend_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        validation_alias=AliasChoices("FRONTEND_ORIGINS", "ALLOWED_ORIGINS"),
    )

    mock_mode: bool = Field(default=False, alias="MOCK_MODE")
    journal_limit: int = Field(default=500, alias="JOURNAL_LIMIT")
    ws_heartbeat_sec: int = Field(default=15, alias="WS_HEARTBEAT_SEC")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    @field_validator("frontend_origins", mode="before")
    @classmethod
    def parse_frontend_origins(cls, value: Any) -> list[str]:
        defaults = ["http://localhost:5173", "http://127.0.0.1:5173"]
        if value is None:
            return defaults
        if isinstance(value, str):
            parsed = [item.strip() for item in value.split(",") if item.strip()]
            return parsed or defaults
        if isinstance(value, list):
            parsed = [str(item).strip() for item in value if str(item).strip()]
            return parsed or defaults
        raise TypeError("FRONTEND_ORIGINS must be comma-separated string or list")

    def cors_origins(self) -> list[str]:
        defaults = {"http://localhost:5173", "http://127.0.0.1:5173"}
        return sorted(defaults.union(self.frontend_origins))

    def mqtt_topics_to_subscribe(self) -> list[str]:
        topics = [self.mqtt_topic_state, self.mqtt_topic_act]
        unique: list[str] = []
        seen: set[str] = set()
        for topic in topics:
            cleaned = topic.strip()
            if cleaned and cleaned not in seen:
                unique.append(cleaned)
                seen.add(cleaned)
        return unique


@lru_cache
def get_settings() -> Settings:
    return Settings()
