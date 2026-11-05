import unittest
from unittest.mock import Mock, patch

import server
from input_sessions import InputSessions
from input_validation import MAX_SDP_LENGTH, MAX_TEXT_LENGTH, ValidationError, validate_input, validate_offer


class InputValidationTests(unittest.TestCase):
    def validate(self, payload):
        return validate_input(payload, server._SUPPORTED_KEYS, server._translate_key)

    def test_valid_inputs_are_normalized(self):
        cases = [
            {"type": "mouse_move", "x": 0, "y": 1},
            {"type": "mouse_click", "x": 0.5, "y": 0.5, "button": 2, "double": True},
            {"type": "mouse_down", "button": 1},
            {"type": "mouse_up", "button": 1},
            {"type": "scroll", "amount": -120},
            {"type": "key", "key": "Control", "action": "down"},
            {"type": "text", "text": "Hello!\n"}
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual(self.validate(payload)["type"], payload["type"])
        self.assertEqual(self.validate({"type": "key", "key": "Control"})["key"], "ctrl")

    def test_invalid_inputs_are_rejected_without_coercion(self):
        cases = [
            {}, {"type": []}, {"type": "unknown"},
            {"type": "mouse_move", "x": True, "y": 0},
            {"type": "mouse_move", "x": "0.5", "y": 0},
            {"type": "mouse_move", "x": float("nan"), "y": 0},
            {"type": "mouse_move", "x": float("inf"), "y": 0},
            {"type": "mouse_move", "x": 10 ** 400, "y": 0},
            {"type": "mouse_move", "x": -0.1, "y": 0},
            {"type": "mouse_move", "x": 0.1, "y": 1.1},
            {"type": "mouse_move", "x": 0.5},
            {"type": "mouse_down", "x": 0.5},
            {"type": "mouse_up", "y": 0.5},
            {"type": "mouse_down", "button": 3},
            {"type": "mouse_down", "button": True},
            {"type": "mouse_down", "monitorIndex": "1"},
            {"type": "mouse_down", "monitorIndex": 0},
            {"type": "mouse_click", "x": 0, "y": 0, "double": "false"},
            {"type": "mouse_click", "x": 0, "y": 0, "preserveCursor": 1},
            {"type": "scroll", "amount": 1001},
            {"type": "scroll", "amount": 2.5},
            {"type": "scroll", "amount": True},
            {"type": "key", "key": []},
            {"type": "key", "key": "MadeUpKey"},
            {"type": "key", "key": "a", "action": "typo"},
            {"type": "key", "key": "a", "action": []},
            {"type": "text", "text": ""},
            {"type": "text", "text": "x" * (MAX_TEXT_LENGTH + 1)},
            {"type": "text", "text": "\u00e9"},
            {"type": "text", "text": 123}
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    self.validate(payload)

    def test_offer_requires_bounded_video_sdp_and_boolean_audio(self):
        valid = {"type": "offer", "sdp": "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n", "audio": False}
        self.assertEqual(validate_offer(valid), valid)
        for field, value in [
            ("type", "answer"), ("sdp", []), ("sdp", "invalid"),
            ("sdp", "x" * (MAX_SDP_LENGTH + 1)), ("audio", "false"), ("monitor", True)
        ]:
            with self.subTest(field=field, value=str(value)[:20]):
                with self.assertRaises(ValidationError):
                    validate_offer(dict(valid, **{field: value}))


class ApiValidationTests(unittest.TestCase):
    def setUp(self):
        self.mouse = Mock()
        self.sessions = InputSessions(Mock(), Mock())
        self.client = server.app.test_client()
        self.headers = {"X-Auth-Token": server.TOKEN}
        self.headers["X-Session-ID"] = self.sessions.create()
        for item in [
            patch.object(server, "pyautogui", self.mouse),
            patch.object(server, "_INPUT_SESSIONS", self.sessions),
            patch.object(server, "_list_monitors", return_value=[
                {"left": 0, "top": 0, "width": 100, "height": 100}
            ]),
            patch.object(server, "_get_selected_monitor_index", return_value=1),
        ]:
            item.start()
            self.addCleanup(item.stop)

    def assert_bad(self, response, status=400):
        self.assertEqual(response.status_code, status)
        self.assertFalse(response.json["ok"])
        self.assertIsInstance(response.json["error"], str)

    def test_non_object_and_malformed_json_have_consistent_errors(self):
        for path in ["/api/input", "/api/monitor", "/webrtc/offer"]:
            for body in ["[]", "null", "true", "{"]:
                with self.subTest(path=path, body=body):
                    self.assert_bad(self.client.post(path, headers=self.headers, data=body, content_type="application/json"))

    def test_invalid_monitor_values_never_reach_desktop_input(self):
        for index in [None, "1", True, 0, -1, 2, 1.5]:
            with self.subTest(index=index):
                self.assert_bad(self.client.post("/api/monitor", headers=self.headers, json={"index": index}))
                self.assert_bad(self.client.post(
                    "/api/input", headers=self.headers,
                    json={"type": "mouse_down", "monitorIndex": index, "sequence": 1},
                ))
        self.assertEqual(self.mouse.mock_calls, [])

    def test_invalid_input_does_not_consume_sequence(self):
        response = self.client.post(
            "/api/input", headers=self.headers,
            json={"type": "mouse_move", "x": "bad", "y": 0, "sequence": 1},
        )
        self.assert_bad(response)
        with self.sessions.use(self.headers["X-Session-ID"], 1):
            pass

    def test_stream_query_is_strict(self):
        for index in ["bad", "1.5", "0", "2", "-1", "999999999999999", "\u0661"]:
            with self.subTest(index=index):
                self.assert_bad(self.client.get("/stream", headers=self.headers, query_string={"monitor": index}))

    def test_large_requests_and_authentication_errors_are_json(self):
        for path in ["/api/input", "/api/session"]:
            self.assert_bad(self.client.post(
                path, headers=self.headers,
                data='{"text":"' + "x" * (256 * 1024) + '"}', content_type="application/json",
            ), 413)
        self.assert_bad(self.client.post("/api/session"), 401)

    def test_webrtc_validation_failure_does_not_create_peer(self):
        with patch.object(server, "RTCPeerConnection") as constructor:
            self.assert_bad(self.client.post(
                "/webrtc/offer", headers=self.headers,
                json={"type": "offer", "sdp": "invalid", "monitor": 1},
            ))
        constructor.assert_not_called()

    def test_webrtc_parse_errors_and_timeouts_have_distinct_statuses(self):
        payload = {"type": "offer", "sdp": "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n", "audio": False}
        for error, status in [(ValueError("Invalid SDP"), 400), (TimeoutError("Timed out"), 504)]:
            def run(coro):
                coro.close()
                raise error
            with self.subTest(status=status), patch.object(server, "_run_webrtc", side_effect=run):
                self.assert_bad(self.client.post("/webrtc/offer", headers=self.headers, json=payload), status)
