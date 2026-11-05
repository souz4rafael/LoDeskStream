import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from PIL import Image


LOGGER = logging.getLogger("lodeskstream.capture")


class CaptureError(RuntimeError):
    pass


class CaptureClosed(CaptureError):
    pass


class MonitorCache:
    def __init__(self, read, ttl=1.0, clock=time.monotonic):
        self._read = read
        self._ttl = ttl
        self._clock = clock
        self._expires_at = float("-inf")
        self._monitors = []
        self._lock = threading.Lock()

    def get(self, force=False):
        with self._lock:
            if force or self._clock() >= self._expires_at:
                self._monitors = [dict(monitor) for monitor in self._read()]
                self._expires_at = self._clock() + self._ttl
            return [dict(monitor) for monitor in self._monitors]


@dataclass(frozen=True)
class CapturedFrame:
    sequence: int
    captured_at: float
    image: Image.Image
    jpeg: Optional[bytes] = None


class CaptureSubscription:
    def __init__(self, worker, jpeg):
        self._worker = worker
        self._jpeg = jpeg
        self._closed = False
        self._sequence = 0

    def next_frame(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        with self._worker.condition:
            while True:
                if self._closed:
                    raise CaptureClosed("Capture subscription is closed")
                if self._worker.stopped:
                    if self._worker.error is not None:
                        raise CaptureError("Desktop capture failed") from self._worker.error
                    raise CaptureClosed("Desktop capture stopped")
                frame = self._worker.latest
                if frame is not None and frame.sequence > self._sequence and (
                    not self._jpeg or frame.jpeg is not None
                ):
                    self._sequence = frame.sequence
                    return frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CaptureError("Desktop capture timed out")
                self._worker.condition.wait(remaining)

    def close(self):
        with self._worker.condition:
            if self._closed:
                return
            self._closed = True
            self._worker.subscribers -= 1
            if self._jpeg:
                self._worker.jpeg_subscribers -= 1
            self._worker.condition.notify_all()


class _CaptureWorker:
    def __init__(self, monitor_index, source_factory, encode_jpeg, fps, idle_timeout):
        self.monitor_index = monitor_index
        self.source_factory = source_factory
        self.encode_jpeg = encode_jpeg
        self.interval = 1.0 / fps
        self.idle_timeout = idle_timeout
        self.condition = threading.Condition()
        self.subscribers = 0
        self.jpeg_subscribers = 0
        self.latest = None
        self.error = None
        self.stopped = False
        self._stop = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name=f"capture-monitor-{monitor_index}", daemon=True
        )

    def subscribe(self, jpeg):
        with self.condition:
            if self.stopped or self._stop.is_set():
                return None
            if not self.subscribers:
                self.latest = None
            self.subscribers += 1
            if jpeg:
                self.jpeg_subscribers += 1
            self.condition.notify_all()
            return CaptureSubscription(self, jpeg)

    def stop(self):
        self._stop.set()
        with self.condition:
            self.stopped = True
            self.condition.notify_all()

    def _active(self):
        with self.condition:
            if not self.subscribers and not self._stop.is_set():
                self.condition.wait_for(
                    lambda: self.subscribers or self._stop.is_set(), self.idle_timeout
                )
            if not self.subscribers or self._stop.is_set():
                self.stopped = True
                self.condition.notify_all()
                return False
            return True

    def _run(self):
        try:
            with self.source_factory(self.monitor_index) as read_frame:
                sequence = 0
                while self._active():
                    started = time.monotonic()
                    with self.condition:
                        wants_jpeg = self.jpeg_subscribers > 0
                    image = read_frame()
                    sequence += 1
                    frame = CapturedFrame(sequence, time.monotonic(), image)
                    with self.condition:
                        self.latest = frame
                        self.condition.notify_all()
                    # Publish RGB before encoding, so JPEG work does not delay video consumers.
                    if wants_jpeg:
                        jpeg = self.encode_jpeg(image)
                        with self.condition:
                            self.latest = CapturedFrame(sequence, frame.captured_at, image, jpeg)
                            self.condition.notify_all()
                    self._stop.wait(max(0.0, self.interval - (time.monotonic() - started)))
        except Exception as exc:
            LOGGER.exception("Capture failed for monitor %s", self.monitor_index)
            with self.condition:
                self.error = exc
        finally:
            with self.condition:
                self.stopped = True
                self.latest = None
                self.condition.notify_all()


class CaptureManager:
    def __init__(self, source_factory, encode_jpeg, fps, idle_timeout=1.0):
        if fps <= 0:
            raise ValueError("Capture FPS must be positive")
        self._source_factory = source_factory
        self._encode_jpeg = encode_jpeg
        self._fps = fps
        self._idle_timeout = idle_timeout
        self._workers = {}
        self._retiring = set()
        self._lock = threading.Lock()
        self._closed = False

    def subscribe(self, monitor_index, jpeg=False):
        with self._lock:
            if self._closed:
                raise CaptureClosed("Capture manager is closed")
            self._retiring = {worker for worker in self._retiring if worker.thread.is_alive()}
            worker = self._workers.get(monitor_index)
            subscription = worker.subscribe(jpeg) if worker else None
            if subscription is None:
                if worker is not None and worker.thread.is_alive():
                    self._retiring.add(worker)
                worker = _CaptureWorker(
                    monitor_index, self._source_factory, self._encode_jpeg,
                    self._fps, self._idle_timeout,
                )
                self._workers[monitor_index] = worker
                subscription = worker.subscribe(jpeg)
                try:
                    worker.thread.start()
                except RuntimeError:
                    worker.stop()
                    subscription.close()
                    del self._workers[monitor_index]
                    raise
            return subscription

    def close(self, timeout=5.0):
        with self._lock:
            self._closed = True
            workers = list(set(self._workers.values()) | self._retiring)
            for worker in workers:
                worker.stop()
        deadline = time.monotonic() + timeout
        for worker in workers:
            worker.thread.join(max(0.0, deadline - time.monotonic()))
            if worker.thread.is_alive():
                raise CaptureError("Capture worker did not stop before the timeout")
