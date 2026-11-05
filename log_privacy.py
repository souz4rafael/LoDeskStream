import logging
import re
from urllib.parse import quote, quote_plus, unquote_plus


_QUERY_VALUE = re.compile(r"""([?&])([^=&\s"'\x1b]+)=([^&\s"'\x1b]*)""")
_SENSITIVE_KEYS = {"token", "stream_token", "x-auth-token"}


def redact(message, token):
    def replace(match):
        if unquote_plus(match.group(2)).casefold() in _SENSITIVE_KEYS:
            return f"{match.group(1)}{match.group(2)}=[REDACTED]"
        return match.group(0)

    message = _QUERY_VALUE.sub(replace, message)
    if token:
        for value in {token, quote(token, safe=""), quote_plus(token)}:
            message = message.replace(value, "[REDACTED]")
    return message


class RedactingFilter(logging.Filter):
    def __init__(self, token):
        super().__init__()
        self.token = token

    def filter(self, record):
        record.msg = redact(record.getMessage(), self.token)
        record.args = ()
        return True


class RedactingFormatter(logging.Formatter):
    def __init__(self, formatter, token):
        super().__init__()
        self.formatter = formatter or logging.Formatter()
        self.token = token

    def format(self, record):
        return redact(self.formatter.format(record), self.token)


def install_log_redaction(token):
    logging.getLogger("werkzeug").addFilter(RedactingFilter(token))
    for handler in logging.getLogger().handlers:
        handler.setFormatter(RedactingFormatter(handler.formatter, token))
