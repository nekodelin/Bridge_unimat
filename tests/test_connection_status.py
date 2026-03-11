from datetime import UTC, datetime, timedelta
import unittest

from app.services.connection_status import ConnectionStatusContext, evaluate_connection_statuses


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


if __name__ == "__main__":
    unittest.main()
