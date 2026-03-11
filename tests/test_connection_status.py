from datetime import UTC, datetime, timedelta
import unittest

from app.schemas import ConnectionStatusItem
from app.services.connection_status import (
    ConnectionStatusContext,
    build_connection_diagnosis,
    evaluate_connection_statuses,
)


class ConnectionStatusTest(unittest.TestCase):
    def _map_by_key(self, items):
        return {item.key: item for item in items}

    def test_no_data_returns_unknown_statuses(self) -> None:
        now = datetime(2026, 3, 11, 12, 0, 0, tzinfo=UTC)
        statuses, data_age_sec = evaluate_connection_statuses(
            ConnectionStatusContext(
                now=now,
                mock_mode=False,
                mqtt_connected=False,
                last_data_at=None,
                last_successful_exchange_at=None,
                realtime_clients=0,
                last_realtime_publish_at=None,
            )
        )

        by_key = self._map_by_key(statuses)
        self.assertIsNone(data_age_sec)
        self.assertEqual(by_key["board_online"].state, "unknown")
        self.assertEqual(by_key["incoming_data"].state, "unknown")
        self.assertEqual(by_key["backend_available"].state, "ok")
        self.assertEqual(by_key["interface_updates"].state, "unknown")
        self.assertEqual(by_key["data_fresh"].state, "unknown")

    def test_fresh_data_reports_ok(self) -> None:
        now = datetime(2026, 3, 11, 12, 0, 0, tzinfo=UTC)
        last_data_at = now - timedelta(seconds=5)
        last_publish_at = now - timedelta(seconds=3)
        statuses, data_age_sec = evaluate_connection_statuses(
            ConnectionStatusContext(
                now=now,
                mock_mode=False,
                mqtt_connected=True,
                last_data_at=last_data_at,
                last_successful_exchange_at=last_data_at,
                realtime_clients=2,
                last_realtime_publish_at=last_publish_at,
            )
        )

        by_key = self._map_by_key(statuses)
        self.assertEqual(data_age_sec, 5)
        self.assertEqual(by_key["board_online"].state, "ok")
        self.assertEqual(by_key["incoming_data"].state, "ok")
        self.assertEqual(by_key["data_fresh"].state, "ok")
        self.assertEqual(by_key["interface_updates"].state, "ok")

    def test_stale_data_reports_error(self) -> None:
        now = datetime(2026, 3, 11, 12, 0, 0, tzinfo=UTC)
        last_data_at = now - timedelta(seconds=70)
        statuses, data_age_sec = evaluate_connection_statuses(
            ConnectionStatusContext(
                now=now,
                mock_mode=False,
                mqtt_connected=False,
                last_data_at=last_data_at,
                last_successful_exchange_at=last_data_at,
                realtime_clients=1,
                last_realtime_publish_at=now - timedelta(seconds=40),
            )
        )

        by_key = self._map_by_key(statuses)
        self.assertEqual(data_age_sec, 70)
        self.assertEqual(by_key["board_online"].state, "error")
        self.assertEqual(by_key["incoming_data"].state, "error")
        self.assertEqual(by_key["data_fresh"].state, "error")

    def test_mock_mode_board_online_unknown(self) -> None:
        now = datetime(2026, 3, 11, 12, 0, 0, tzinfo=UTC)
        last_data_at = now - timedelta(seconds=3)
        statuses, _ = evaluate_connection_statuses(
            ConnectionStatusContext(
                now=now,
                mock_mode=True,
                mqtt_connected=True,
                last_data_at=last_data_at,
                last_successful_exchange_at=last_data_at,
                realtime_clients=0,
                last_realtime_publish_at=None,
            )
        )

        by_key = self._map_by_key(statuses)
        self.assertEqual(by_key["board_online"].state, "unknown")
        self.assertEqual(by_key["incoming_data"].state, "ok")


class ConnectionDiagnosisTest(unittest.TestCase):
    def _statuses(
        self,
        *,
        board_online: str = "ok",
        incoming_data: str = "ok",
        backend_available: str = "ok",
        interface_updates: str = "ok",
        data_fresh: str = "ok",
    ) -> list[ConnectionStatusItem]:
        return [
            ConnectionStatusItem(key="board_online", label="board_online", state=board_online),
            ConnectionStatusItem(key="incoming_data", label="incoming_data", state=incoming_data),
            ConnectionStatusItem(key="backend_available", label="backend_available", state=backend_available),
            ConnectionStatusItem(key="interface_updates", label="interface_updates", state=interface_updates),
            ConnectionStatusItem(key="data_fresh", label="data_fresh", state=data_fresh),
        ]

    def test_board_unavailable_rule(self) -> None:
        diagnosis = build_connection_diagnosis(self._statuses(board_online="error"))
        self.assertEqual(diagnosis.problemTitle, "Плата недоступна")
        self.assertEqual(diagnosis.severity, "error")

    def test_no_orange_data_rule(self) -> None:
        diagnosis = build_connection_diagnosis(self._statuses(incoming_data="error"))
        self.assertEqual(diagnosis.problemTitle, "Нет данных от Orange")
        self.assertEqual(diagnosis.severity, "error")

    def test_backend_unavailable_rule(self) -> None:
        diagnosis = build_connection_diagnosis(self._statuses(backend_available="error"))
        self.assertEqual(diagnosis.problemTitle, "Сервер недоступен")
        self.assertEqual(diagnosis.severity, "error")

    def test_ui_updates_rule(self) -> None:
        diagnosis = build_connection_diagnosis(self._statuses(interface_updates="error"))
        self.assertEqual(diagnosis.problemTitle, "Данные не доходят до веб-интерфейса")
        self.assertEqual(diagnosis.severity, "error")

    def test_data_stale_warn_rule(self) -> None:
        diagnosis = build_connection_diagnosis(self._statuses(data_fresh="warn"))
        self.assertEqual(diagnosis.problemTitle, "Данные устарели")
        self.assertEqual(diagnosis.severity, "warn")

    def test_data_stale_error_rule(self) -> None:
        diagnosis = build_connection_diagnosis(self._statuses(data_fresh="error"))
        self.assertEqual(diagnosis.problemTitle, "Данные устарели")
        self.assertEqual(diagnosis.severity, "error")

    def test_unknown_rule(self) -> None:
        diagnosis = build_connection_diagnosis(self._statuses(board_online="unknown"))
        self.assertEqual(diagnosis.problemTitle, "Недостаточно данных для диагностики")
        self.assertEqual(diagnosis.severity, "warn")

    def test_ok_rule(self) -> None:
        diagnosis = build_connection_diagnosis(self._statuses())
        self.assertEqual(diagnosis.problemTitle, "Не обнаружена")
        self.assertEqual(diagnosis.recommendedAction, "Система работает штатно")
        self.assertEqual(diagnosis.severity, "ok")


if __name__ == "__main__":
    unittest.main()
