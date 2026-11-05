import logging
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field


LOGGER = logging.getLogger("lodeskstream.sessions")
_UNSEQUENCED = object()


class SessionExpired(Exception):
    pass


class ControlBusy(Exception):
    pass


class InvalidInputSequence(Exception):
    pass


@dataclass
class InputSession:
    last_seen: float
    buttons: set = field(default_factory=set)
    keys: set = field(default_factory=set)
    last_sequence: int = 0


class InputSessions:
    def __init__(self, mouse_up, key_up, timeout=15.0, clock=time.monotonic):
        self._mouse_up = mouse_up
        self._key_up = key_up
        self._timeout = timeout
        self._clock = clock
        self._sessions = {}
        self._lock = threading.RLock()

    def create(self):
        with self._lock:
            session_id = secrets.token_urlsafe(24)
            self._sessions[session_id] = InputSession(self._clock())
            return session_id

    def _get(self, session_id):
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionExpired("Control session expired. Reconnect.")
        if self._clock() - session.last_seen >= self._timeout:
            self._release(session)
            del self._sessions[session_id]
            raise SessionExpired("Control session expired. Reconnect.")
        return session

    def heartbeat(self, session_id):
        with self._lock:
            self._get(session_id).last_seen = self._clock()

    @contextmanager
    def use(self, session_id, sequence=_UNSEQUENCED):
        with self._lock:
            session = self._get(session_id)
            if any(other is not session and (other.buttons or other.keys)
                   for other in self._sessions.values()):
                raise ControlBusy("Another session is holding mouse buttons or keys.")
            if sequence is not _UNSEQUENCED:
                if type(sequence) is not int or sequence != session.last_sequence + 1:
                    raise InvalidInputSequence("Input sequence is invalid or out of order. Reconnect.")
                session.last_sequence = sequence
            session.last_seen = self._clock()
            yield session

    def _release(self, session):
        failures = []
        for held, release in ((session.buttons, self._mouse_up), (session.keys, self._key_up)):
            for value in list(held):
                try:
                    release(value)
                except Exception as exc:
                    LOGGER.exception("Could not release held input: %s", value)
                    failures.append(exc)
                else:
                    held.remove(value)
        if failures:
            raise RuntimeError("Could not release all held inputs; cleanup will retry.") from failures[0]

    def close(self, session_id):
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                # Mark closed before cleanup so queued input cannot revive a failed close.
                session.last_seen = self._clock() - self._timeout
                self._release(session)
                del self._sessions[session_id]

    def expire(self):
        with self._lock:
            for session_id, session in list(self._sessions.items()):
                if self._clock() - session.last_seen >= self._timeout:
                    try:
                        self.close(session_id)
                    except RuntimeError:
                        LOGGER.exception("Session cleanup failed; retrying on the next sweep")
