import io
import logging
import unittest

from log_privacy import RedactingFilter, RedactingFormatter, redact


class LogPrivacyTests(unittest.TestCase):
    def test_query_values_are_redacted_including_encoded_keys(self):
        for query in ["token=unknown%20secret", "%74oken=secret", "TOKEN=secret", "stream_token=secret"]:
            value = redact(f'GET /stream?{query}&monitor=1 HTTP/1.1', "different-secret")
            self.assertNotIn("=secret", value)
            self.assertNotIn("unknown", value)
            self.assertIn("[REDACTED]&monitor=1", value)

    def test_known_token_is_redacted_in_plain_and_encoded_forms(self):
        value = redact("secret=a b+c /stream?x=a%20b%2Bc /other?x=a+b%2Bc", "a b+c")
        self.assertNotIn("a b+c", value)
        self.assertNotIn("a%20b%2Bc", value)
        self.assertNotIn("a+b%2Bc", value)

    def test_werkzeug_formatted_arguments_are_redacted(self):
        record = logging.LogRecord("werkzeug", logging.INFO, "", 0, '"%s" %s',
                                   ("GET /stream?token=hidden&monitor=1 HTTP/1.1", 200), None)
        self.assertTrue(RedactingFilter("hidden").filter(record))
        self.assertNotIn("hidden", record.getMessage())
        self.assertIn("200", record.getMessage())

    def test_tracebacks_are_redacted_without_losing_error_details(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(RedactingFormatter(logging.Formatter("%(levelname)s:%(message)s"), "hidden"))
        logger = logging.Logger("isolated")
        logger.addHandler(handler)
        try:
            raise RuntimeError("Request failed /stream?token=hidden")
        except RuntimeError:
            logger.exception("Failure for %s", "hidden")
        self.assertNotIn("hidden", stream.getvalue())
        self.assertIn("RuntimeError", stream.getvalue())
        self.assertIn("[REDACTED]", stream.getvalue())

    def test_health_identifies_instance_but_never_returns_credentials(self):
        import server
        response = server.app.test_client().get("/health")
        self.assertEqual(response.json["application"], "LoDeskStream")
        self.assertEqual(response.json["instanceId"], server.INSTANCE_ID)
        self.assertNotIn("token", response.json)
        self.assertNotIn("password", response.json)
