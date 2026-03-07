import asyncio
import json
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime

import paho.mqtt.client as mqtt
from pydantic import ValidationError

from app.config import Settings
from app.models import ActPayload, BoardPayload
from app.utils import now_utc

logger = logging.getLogger("unimat.mqtt")


class MQTTBridgeClient:
    def __init__(
        self,
        settings: Settings,
        on_board_message: Callable[[BoardPayload, str], Awaitable[None]],
        on_act_message: Callable[[ActPayload, str], Awaitable[None]],
        on_connection_event: Callable[[str], Awaitable[None]],
        on_raw_message: Callable[[str, str, datetime], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        self.on_board_message = on_board_message
        self.on_act_message = on_act_message
        self.on_connection_event = on_connection_event
        self.on_raw_message = on_raw_message

        self._loop: asyncio.AbstractEventLoop | None = None
        self._connection_lock = threading.Lock()
        self._connected = False
        self._sub_topics = settings.mqtt_topics_to_subscribe()

        client_id = f"unimat-{uuid.uuid4().hex[:10]}"
        self.client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        if settings.mqtt_user:
            self.client.username_pw_set(settings.mqtt_user, settings.mqtt_password)
        if settings.mqtt_tls:
            self.client.tls_set()

    @property
    def connected(self) -> bool:
        with self._connection_lock:
            return self._connected

    def _set_connected(self, value: bool) -> None:
        with self._connection_lock:
            self._connected = value

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        logger.info(
            "Starting MQTT client: host=%s port=%s subs=%s pub=%s",
            self.settings.mqtt_host,
            self.settings.mqtt_port,
            self._sub_topics,
            self.settings.mqtt_topic_act,
        )
        self.client.connect_async(self.settings.mqtt_host, self.settings.mqtt_port, keepalive=30)
        self.client.loop_start()

    def stop(self) -> None:
        logger.info("Stopping MQTT client")
        try:
            self.client.disconnect()
        except Exception:
            logger.exception("MQTT disconnect failed")
        finally:
            self.client.loop_stop()
            self._set_connected(False)

    def publish_tifon(self, value: bool) -> tuple[bool, str | None]:
        if not self.connected:
            return False, "mqtt client is disconnected"

        payload = json.dumps({"tifon": bool(value)}, separators=(",", ":"))
        try:
            info = self.client.publish(
                topic=self.settings.mqtt_topic_act,
                payload=payload,
                qos=0,
                retain=False,
            )
        except Exception as exc:
            logger.exception("MQTT publish failed")
            return False, str(exc)

        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            return False, mqtt.error_string(info.rc)
        return True, None

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: object,
        flags: dict[str, int],
        reason_code: int,
        properties: object = None,
    ) -> None:
        _ = userdata, flags, properties
        rc = int(reason_code)
        if rc == 0:
            self._set_connected(True)
            logger.info("MQTT connected")
            for topic in self._sub_topics:
                sub_rc, _ = client.subscribe(topic, qos=0)
                if sub_rc == mqtt.MQTT_ERR_SUCCESS:
                    logger.info("Subscribed to %s", topic)
                else:
                    logger.error("Failed to subscribe %s: %s", topic, mqtt.error_string(sub_rc))
            self._notify_connection_event("mqtt_connected")
            return

        logger.error("MQTT connect error: %s", rc)
        self._set_connected(False)

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: object,
        reason_code: int,
        properties: object = None,
    ) -> None:
        _ = client, userdata, properties
        self._set_connected(False)
        rc = int(reason_code)
        if rc == 0:
            logger.info("MQTT disconnected")
        else:
            logger.warning("MQTT disconnected unexpectedly: code=%s", rc)
        self._notify_connection_event("mqtt_disconnected")

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: object,
        msg: mqtt.MQTTMessage,
    ) -> None:
        _ = client, userdata
        raw_text = msg.payload.decode("utf-8", errors="replace")
        logger.info("MQTT message topic=%s payload=%s", msg.topic, raw_text)
        if self.on_raw_message is not None:
            self._schedule(self.on_raw_message(msg.topic, raw_text, now_utc()))
        try:
            payload_data = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.exception("Invalid MQTT JSON: topic=%s", msg.topic)
            return

        if msg.topic == self.settings.mqtt_topic_state:
            try:
                payload = BoardPayload.model_validate(payload_data)
            except ValidationError:
                logger.exception("BoardPayload validation failed: topic=%s", msg.topic)
                return
            logger.info(
                "Validated BoardPayload topic=%s in=%s inversed=%s out=%s",
                msg.topic,
                payload.in_,
                payload.inversed,
                payload.out,
            )
            self._schedule(self.on_board_message(payload, msg.topic))
            return

        if msg.topic == self.settings.mqtt_topic_act:
            try:
                payload = ActPayload.model_validate(payload_data)
            except ValidationError:
                logger.exception("ActPayload validation failed: topic=%s", msg.topic)
                return
            logger.info("Validated ActPayload topic=%s tifon=%s", msg.topic, payload.tifon)
            self._schedule(self.on_act_message(payload, msg.topic))
            return

        logger.warning("Unhandled MQTT topic: %s", msg.topic)

    def _notify_connection_event(self, event_name: str) -> None:
        self._schedule(self.on_connection_event(event_name))

    def _schedule(self, coro: Awaitable[None]) -> None:
        if self._loop is None:
            logger.error("Async loop not initialized for MQTT callback")
            return
        self._loop.call_soon_threadsafe(self._create_task, coro)

    @staticmethod
    def _create_task(coro: Awaitable[None]) -> None:
        task = asyncio.create_task(coro)
        task.add_done_callback(_log_task_error)


def _log_task_error(task: asyncio.Task[None]) -> None:
    try:
        task.result()
    except Exception:
        logger.exception("Unhandled async error in MQTT callback")
