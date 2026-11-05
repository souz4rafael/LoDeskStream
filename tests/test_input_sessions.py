import unittest
from unittest.mock import Mock, patch

from input_sessions import ControlBusy, InputSessions, InvalidInputSequence, SessionExpired


class InputSessionTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.mouse_up = Mock()
        self.key_up = Mock()
        self.sessions = InputSessions(self.mouse_up, self.key_up, clock=lambda: self.now)
        self.session_id = self.sessions.create()

    def hold_input(self):
        with self.sessions.use(self.session_id) as session:
            session.buttons.add("left")
            session.keys.add("ctrl")

    def test_expiry_releases_mouse_and_keys(self):
        self.hold_input()
        self.now = 15
        self.sessions.expire()
        self.mouse_up.assert_called_once_with("left")
        self.key_up.assert_called_once_with("ctrl")
        with self.assertRaises(SessionExpired):
            self.sessions.heartbeat(self.session_id)

    def test_heartbeat_renews_lease(self):
        self.hold_input()
        self.now = 10
        self.sessions.heartbeat(self.session_id)
        self.now = 20
        self.sessions.expire()
        self.mouse_up.assert_not_called()
        self.now = 25
        self.sessions.expire()
        self.mouse_up.assert_called_once_with("left")

    def test_late_heartbeat_cannot_revive_session(self):
        self.hold_input()
        self.now = 16
        with self.assertRaises(SessionExpired):
            self.sessions.heartbeat(self.session_id)
        self.mouse_up.assert_called_once_with("left")

    def test_close_releases_and_is_idempotent(self):
        self.hold_input()
        self.sessions.close(self.session_id)
        self.sessions.close(self.session_id)
        self.mouse_up.assert_called_once_with("left")
        self.key_up.assert_called_once_with("ctrl")

    def test_closed_session_cannot_accept_delayed_input(self):
        self.sessions.close(self.session_id)
        with self.assertRaises(SessionExpired):
            with self.sessions.use(self.session_id):
                self.fail("Closed session accepted input")

    def test_second_session_cannot_interfere_with_held_input(self):
        self.hold_input()
        second = self.sessions.create()
        with self.assertRaises(ControlBusy):
            with self.sessions.use(second):
                self.fail("Other controller accepted during drag")
        self.sessions.close(second)
        self.mouse_up.assert_not_called()
        self.sessions.close(self.session_id)
        self.mouse_up.assert_called_once()

    def test_failed_release_is_logged_and_retried(self):
        self.hold_input()
        self.mouse_up.side_effect = [OSError("Device unavailable"), None]
        with self.assertLogs("lodeskstream.sessions", level="ERROR"):
            with self.assertRaises(RuntimeError):
                self.sessions.close(self.session_id)
        self.key_up.assert_called_once_with("ctrl")
        self.sessions.expire()
        self.assertEqual(self.mouse_up.call_count, 2)
        with self.assertRaises(SessionExpired):
            self.sessions.heartbeat(self.session_id)

    def test_out_of_order_or_replayed_sequence_is_rejected(self):
        for sequence in [None, 0, 2, True, 1.0, "1"]:
            with self.assertRaises(InvalidInputSequence):
                with self.sessions.use(self.session_id, sequence):
                    self.fail("Invalid sequence accepted")
        with self.sessions.use(self.session_id, 1):
            pass
        with self.assertRaises(InvalidInputSequence):
            with self.sessions.use(self.session_id, 1):
                self.fail("Replay accepted")
        with self.sessions.use(self.session_id, 2):
            pass


class SessionApiTests(unittest.TestCase):
    def setUp(self):
        import server
        self.server = server
        self.mouse = Mock()
        self.now = 0.0
        self.sessions = InputSessions(
            lambda button: self.mouse.mouseUp(button=button),
            lambda key: self.mouse.keyUp(key),
            clock=lambda: self.now,
        )
        patches = [
            patch.object(server, "_INPUT_SESSIONS", self.sessions),
            patch.object(server, "pyautogui", self.mouse),
            patch.object(server, "_get_selected_monitor_index", return_value=1),
            patch.object(server, "_sanitize_monitor_index", return_value=1),
            patch.object(server, "_list_monitors", return_value=[
                {"left": 0, "top": 0, "width": 100, "height": 100}
            ]),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.client = server.app.test_client()
        self.headers = {"X-Auth-Token": server.TOKEN}
        self.session_id = self.client.post("/api/session", headers=self.headers).json["sessionId"]
        self.headers["X-Session-ID"] = self.session_id

    def test_authentication_required(self):
        self.assertEqual(self.client.post("/api/session").status_code, 401)
        self.assertEqual(self.client.post("/api/session/heartbeat").status_code, 401)
        self.assertEqual(self.client.delete("/api/session").status_code, 401)

    def test_input_requires_session(self):
        response = self.client.post(
            "/api/input", headers={"X-Auth-Token": self.server.TOKEN},
            json={"type": "mouse_down", "button": 0},
        )
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json["sessionExpired"])
        self.mouse.mouseDown.assert_not_called()

    def test_disconnect_releases_mouse_and_keys(self):
        for sequence, payload in enumerate([
            {"type": "mouse_down", "button": 0, "x": 0.5, "y": 0.5},
            {"type": "key", "key": "Control", "action": "down"},
        ], start=1):
            payload["sequence"] = sequence
            self.assertEqual(self.client.post("/api/input", headers=self.headers, json=payload).status_code, 200)
        self.assertEqual(self.client.delete("/api/session", headers=self.headers).status_code, 200)
        self.mouse.mouseUp.assert_called_once_with(button="left")
        self.mouse.keyUp.assert_called_once_with("ctrl")
        response = self.client.post("/api/input", headers=self.headers, json={"type": "mouse_down"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.mouse.mouseDown.call_count, 1)

    def test_expired_heartbeat_returns_conflict(self):
        self.now = 15
        response = self.client.post("/api/session/heartbeat", headers=self.headers)
        self.assertEqual(response.status_code, 409)

    def test_other_session_cannot_release_owner_button(self):
        self.client.post("/api/input", headers=self.headers, json={"type": "mouse_down", "button": 0, "sequence": 1})
        other = self.sessions.create()
        headers = dict(self.headers, **{"X-Session-ID": other})
        response = self.client.post("/api/input", headers=headers, json={"type": "mouse_up", "button": 0, "sequence": 1})
        self.assertEqual(response.status_code, 409)
        self.mouse.mouseUp.assert_not_called()

    def test_sequence_prevents_replay_and_reordered_mouse_input(self):
        payload = {"type": "mouse_down", "button": 0, "sequence": 2}
        self.assertEqual(self.client.post("/api/input", headers=self.headers, json=payload).status_code, 409)
        self.mouse.mouseDown.assert_not_called()
        payload["sequence"] = 1
        self.assertEqual(self.client.post("/api/input", headers=self.headers, json=payload).status_code, 200)
        self.assertEqual(self.client.post("/api/input", headers=self.headers, json=payload).status_code, 409)
        self.mouse.mouseDown.assert_called_once_with(button="left")
        self.client.delete("/api/session", headers=self.headers)
        self.mouse.mouseUp.assert_called_once_with(button="left")

    def test_input_without_sequence_is_rejected(self):
        response = self.client.post("/api/input", headers=self.headers, json={"type": "mouse_down"})
        self.assertEqual(response.status_code, 409)
        self.mouse.mouseDown.assert_not_called()

    def test_invalid_sequence_types_are_rejected(self):
        for sequence in [None, True, 1.0, "1"]:
            response = self.client.post(
                "/api/input", headers=self.headers,
                json={"type": "mouse_down", "sequence": sequence},
            )
            self.assertEqual(response.status_code, 409)
        self.mouse.mouseDown.assert_not_called()


if __name__ == "__main__":
    unittest.main()
