import io
import os
import asyncio
import logging
import secrets
import sys
import threading
import time
import ctypes
import atexit
import json
from contextlib import contextmanager
from concurrent.futures import TimeoutError as FutureTimeoutError
from fractions import Fraction
from typing import Dict, Tuple

def _enable_dpi_awareness() -> None:
    # Ensure monitor geometry and capture coordinates are in the same DPI space.
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


_enable_dpi_awareness()

import mss
import pyautogui
from flask import Flask, Response, abort, jsonify, request, send_from_directory
from PIL import Image, ImageDraw
from aiortc import AudioStreamTrack, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.mediastreams import MediaStreamError
from av import AudioFrame, VideoFrame
from input_sessions import ControlBusy, InputSessions, InvalidInputSequence, SessionExpired
from input_validation import ValidationError, integer, validate_input, validate_offer
from werkzeug.exceptions import HTTPException
from capture_pipeline import CaptureError, CaptureManager, MonitorCache
from log_privacy import install_log_redaction

try:
    import numpy as np
    import soundcard as sc
except ImportError:
    np = None
    sc = None

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger("lodeskstream")
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(BASE_DIR, "web")

HOST = os.getenv("STREAM_HOST", "0.0.0.0")
PORT = int(os.getenv("STREAM_PORT", "8765"))
TOKEN = os.getenv("STREAM_TOKEN") or secrets.token_urlsafe(18)
INSTANCE_ID = os.getenv("STREAM_INSTANCE_ID") or secrets.token_hex(16)
install_log_redaction(TOKEN)


def _build_version():
    path = os.path.join(BASE_DIR, "build-info.json")
    if not os.path.exists(path):
        return "development"
    with open(path, encoding="utf-8") as metadata:
        info = json.load(metadata)
    if not isinstance(info, dict) or info.get("application") != "LoDeskStream" or not isinstance(info.get("version"), str):
        raise ValueError("Invalid build-info.json")
    return info["version"]


VERSION = _build_version()
STREAM_PROFILE = os.getenv("STREAM_PROFILE", "default").lower()
if STREAM_PROFILE not in {"default", "legacy"}:
    raise ValueError("STREAM_PROFILE must be default or legacy")
FPS = max(1, int(os.getenv("STREAM_FPS", "10" if STREAM_PROFILE == "legacy" else "12")))
JPEG_QUALITY = max(20, min(95, int(os.getenv("STREAM_JPEG_QUALITY", "60" if STREAM_PROFILE == "legacy" else "65"))))
MAX_WIDTH = max(0, int(os.getenv("STREAM_MAX_WIDTH", "1280" if STREAM_PROFILE == "legacy" else "0")))
MAX_HEIGHT = max(0, int(os.getenv("STREAM_MAX_HEIGHT", "720" if STREAM_PROFILE == "legacy" else "0")))
JPEG_OPTIMIZE = os.getenv(
    "STREAM_JPEG_OPTIMIZE", "0" if STREAM_PROFILE == "legacy" else "1"
).lower() not in {"0", "false", "no", "off"}
ENABLE_AUDIO = os.getenv("STREAM_AUDIO", "1").lower() not in {"0", "false", "no", "off"}
AUDIO_SAMPLE_RATE = 48000
AUDIO_FRAME_SAMPLES = 960

pyautogui.PAUSE = 0
pyautogui.FAILSAFE = False
_SUPPORTED_KEYS = frozenset(pyautogui.KEYBOARD_KEYS)

_LAST_MOUSE_MOVE = 0.0
_MOUSE_INTERVAL_SECONDS = 1.0 / 40.0
_STATE_LOCK = threading.Lock()
_SELECTED_MONITOR_INDEX = 1
_PEERS = set()
_PEER_TRACKS = {}
_WEBRTC_LOOP = asyncio.new_event_loop()
_INPUT_SESSIONS = InputSessions(
    lambda button: pyautogui.mouseUp(button=button),
    lambda key: pyautogui.keyUp(key),
)


def _expire_input_sessions() -> None:
    while True:
        time.sleep(1)
        _INPUT_SESSIONS.expire()


def _run_webrtc_loop() -> None:
    asyncio.set_event_loop(_WEBRTC_LOOP)
    _WEBRTC_LOOP.run_forever()


threading.Thread(target=_run_webrtc_loop, daemon=True).start()
threading.Thread(target=_expire_input_sessions, daemon=True).start()


def _token_ok() -> bool:
    header = request.headers.get("X-Auth-Token", "")
    query = request.args.get("token", "")
    return header == TOKEN or query == TOKEN


def _require_token() -> None:
    if not _token_ok():
        abort(401)


def _json_object():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValidationError("Request body must be a JSON object")
    return payload


@app.before_request
def enforce_request_size():
    if request.content_length is not None and request.content_length > app.config["MAX_CONTENT_LENGTH"]:
        abort(413)


@app.errorhandler(ValidationError)
def invalid_request(exc):
    return jsonify({"ok": False, "error": str(exc)}), 400


@app.errorhandler(HTTPException)
def http_error(exc):
    if request.path.startswith(("/api/", "/webrtc/", "/stream")):
        return jsonify({"ok": False, "error": exc.description}), exc.code
    return exc


def _read_monitors():
    with mss.mss() as sct:
        return sct.monitors[1:]


_MONITOR_CACHE = MonitorCache(_read_monitors)


def _list_monitors(force=False):
    return _MONITOR_CACHE.get(force=force)


def _get_selected_monitor_index(monitors=None) -> int:
    global _SELECTED_MONITOR_INDEX
    if monitors is None:
        monitors = _list_monitors()
    with _STATE_LOCK:
        if not monitors:
            _SELECTED_MONITOR_INDEX = 1
            return 1
        max_index = len(monitors)
        _SELECTED_MONITOR_INDEX = max(1, min(_SELECTED_MONITOR_INDEX, max_index))
        return _SELECTED_MONITOR_INDEX


def _to_screen_xy(normalized_x: float, normalized_y: float, monitor: dict) -> Tuple[int, int]:
    width = int(monitor["width"])
    height = int(monitor["height"])
    left = int(monitor["left"])
    top = int(monitor["top"])
    x = left + int(max(0.0, min(1.0, normalized_x)) * (width - 1))
    y = top + int(max(0.0, min(1.0, normalized_y)) * (height - 1))
    return x, y


def _sanitize_monitor_index(requested_index: int, monitors=None) -> int:
    if monitors is None:
        monitors = _list_monitors()
    if not monitors:
        raise ValidationError("No monitor is available")
    return integer(requested_index, "monitor index", 1, len(monitors))


def _draw_host_cursor(image: Image.Image, monitor: dict) -> None:
    position = pyautogui.position()
    left = int(monitor["left"])
    top = int(monitor["top"])
    width = int(monitor["width"])
    height = int(monitor["height"])
    x = int(position.x) - left
    y = int(position.y) - top
    if x < 0 or y < 0 or x >= width or y >= height:
        return

    draw = ImageDraw.Draw(image, "RGBA")
    draw.ellipse(
        (x - 14, y - 14, x + 14, y + 14),
        fill=(255, 255, 255, 48), outline=(0, 0, 0, 160), width=2,
    )
    draw.ellipse(
        (x - 12, y - 12, x + 12, y + 12),
        outline=(255, 255, 255, 210), width=2,
    )
    draw.ellipse(
        (x - 2, y - 2, x + 2, y + 2),
        fill=(255, 255, 255, 230), outline=(0, 0, 0, 200),
    )


def _resize_frame(image):
    scale = min(
        1.0,
        MAX_WIDTH / image.width if MAX_WIDTH else 1.0,
        MAX_HEIGHT / image.height if MAX_HEIGHT else 1.0,
    )
    if scale < 1:
        return image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.Resampling.BOX,
        )
    return image


@contextmanager
def _capture_source(monitor_index):
    with mss.mss() as sct:
        def read_frame():
            monitors = _list_monitors()
            index = _sanitize_monitor_index(monitor_index, monitors)
            monitor = monitors[index - 1]
            shot = sct.grab(monitor)
            image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            _draw_host_cursor(image, monitor)
            return _resize_frame(image)
        yield read_frame


def _encode_jpeg(image):
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=JPEG_OPTIMIZE)
    return buffer.getvalue()


_CAPTURE_MANAGER = CaptureManager(_capture_source, _encode_jpeg, FPS)
atexit.register(_CAPTURE_MANAGER.close)


def _capture_frames(monitor_index: int):
    subscription = _CAPTURE_MANAGER.subscribe(monitor_index, jpeg=True)
    try:
        while True:
            frame = subscription.next_frame()
            jpeg = frame.jpeg
            if jpeg is None:
                raise CaptureError("MJPEG capture did not produce an encoded frame")
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Cache-Control: no-cache\r\n\r\n" + jpeg + b"\r\n"
            )
    finally:
        subscription.close()


class DesktopVideoTrack(VideoStreamTrack):
    def __init__(self, monitor_index: int):
        super().__init__()
        self._monitor_index = monitor_index
        self._subscription = None
        self._started_at = None
        self._pts = -1
        self._time_base = Fraction(1, 90000)

    @staticmethod
    def _read_video(subscription):
        frame = subscription.next_frame()
        return VideoFrame.from_image(frame.image), frame.captured_at

    async def recv(self) -> VideoFrame:
        if self.readyState == "ended":
            raise MediaStreamError
        if self._subscription is None:
            self._subscription = _CAPTURE_MANAGER.subscribe(self._monitor_index)
        try:
            frame, captured_at = await asyncio.to_thread(self._read_video, self._subscription)
        except CaptureError as exc:
            self.stop()
            raise MediaStreamError from exc
        if self.readyState == "ended":
            raise MediaStreamError
        if self._started_at is None:
            self._started_at = captured_at
        self._pts = max(self._pts + 1, round((captured_at - self._started_at) * 90000))
        frame.pts = self._pts
        frame.time_base = self._time_base
        return frame

    def stop(self) -> None:
        if self.readyState == "ended":
            return
        super().stop()
        if self._subscription is not None:
            self._subscription.close()


def _find_loopback_microphone():
    if sc is None:
        raise RuntimeError("soundcard is not installed")

    microphones = sc.all_microphones(include_loopback=True)
    default_speaker_name = getattr(sc.default_speaker(), "name", "").lower()
    for microphone in microphones:
        name = getattr(microphone, "name", "").lower()
        is_loopback = bool(getattr(microphone, "isloopback", False)) or "loopback" in name
        if is_loopback and default_speaker_name and default_speaker_name in name:
            return microphone
    for microphone in microphones:
        name = getattr(microphone, "name", "").lower()
        if bool(getattr(microphone, "isloopback", False)) or "loopback" in name:
            return microphone
    raise RuntimeError("no Windows loopback audio device found")


class SystemAudioTrack(AudioStreamTrack):
    def __init__(self):
        super().__init__()
        if np is None:
            raise RuntimeError("numpy is not installed")

        microphone = _find_loopback_microphone()
        self._recorder = microphone.recorder(samplerate=AUDIO_SAMPLE_RATE, channels=2)
        self._recorder.__enter__()
        self._pts = 0
        self._time_base = Fraction(1, AUDIO_SAMPLE_RATE)

    async def recv(self) -> AudioFrame:
        samples = await asyncio.to_thread(self._recorder.record, AUDIO_FRAME_SAMPLES)
        if samples.ndim == 1:
            samples = np.column_stack((samples, samples))
        if samples.shape[1] == 1:
            samples = np.repeat(samples, 2, axis=1)
        if samples.shape[1] > 2:
            samples = samples[:, :2]

        pcm = np.clip(samples, -1.0, 1.0)
        pcm = (pcm * 32767.0).astype(np.int16, copy=False)
        frame = AudioFrame(format="s16", layout="stereo", samples=pcm.shape[0])
        frame.planes[0].update(pcm.tobytes())
        frame.sample_rate = AUDIO_SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = self._time_base
        self._pts += pcm.shape[0]
        return frame

    def stop(self) -> None:
        if self.readyState == "ended":
            return
        super().stop()
        self._recorder.__exit__(None, None, None)


def _run_webrtc(coro):
    future = asyncio.run_coroutine_threadsafe(coro, _WEBRTC_LOOP)
    try:
        return future.result(timeout=20)
    except FutureTimeoutError:
        future.cancel()
        raise TimeoutError("WebRTC negotiation timed out")


async def _close_peer(peer: RTCPeerConnection) -> None:
    try:
        await peer.close()
    finally:
        _PEERS.discard(peer)
        errors = []
        for track in _PEER_TRACKS.pop(peer, []):
            try:
                track.stop()
            except Exception as exc:
                LOGGER.exception("WebRTC track cleanup failed")
                errors.append(exc)
        if errors:
            raise RuntimeError("Could not release all WebRTC tracks") from errors[0]


async def _create_webrtc_answer(payload: dict) -> dict:
    requested_monitor = payload["monitor"] if "monitor" in payload else _get_selected_monitor_index()
    monitor_index = _sanitize_monitor_index(requested_monitor)
    peer = RTCPeerConnection()
    _PEERS.add(peer)
    tracks = _PEER_TRACKS[peer] = []
    try:
        video = DesktopVideoTrack(monitor_index)
        tracks.append(video)
        peer.addTrack(video)

        @video.on("ended")
        async def on_video_ended():
            await _close_peer(peer)

        audio_enabled = False
        audio_error = ""
        if ENABLE_AUDIO and payload.get("audio", True):
            try:
                audio = SystemAudioTrack()
            except Exception as exc:
                audio_error = str(exc)
                LOGGER.warning("WebRTC audio unavailable: %s", audio_error)
            else:
                tracks.append(audio)
                peer.addTrack(audio)
                audio_enabled = True

        @peer.on("connectionstatechange")
        async def on_connectionstatechange():
            if peer.connectionState in {"closed", "failed", "disconnected"}:
                await _close_peer(peer)

        offer = RTCSessionDescription(sdp=payload["sdp"], type=payload["type"])
        await peer.setRemoteDescription(offer)
        answer = await peer.createAnswer()
        await peer.setLocalDescription(answer)
        return {
            "ok": True,
            "type": peer.localDescription.type,
            "sdp": peer.localDescription.sdp,
            "audio": audio_enabled,
            "audioError": audio_error,
        }
    except (Exception, asyncio.CancelledError):
        try:
            await _close_peer(peer)
        except Exception:
            LOGGER.exception("WebRTC cleanup failed after negotiation failure")
        raise


def _translate_button(button_code: int) -> str:
    mapping: Dict[int, str] = {0: "left", 1: "middle", 2: "right"}
    return mapping[button_code]


def _event_screen_position(payload: dict, monitor: dict) -> Tuple[int, int]:
    return _to_screen_xy(float(payload.get("x", 0.0)), float(payload.get("y", 0.0)), monitor)


def _translate_key(key: str) -> str:
    key = (key or "").lower()
    special = {
        "arrowleft": "left",
        "arrowright": "right",
        "arrowup": "up",
        "arrowdown": "down",
        "escape": "esc",
        " ": "space",
        "control": "ctrl",
        "meta": "win",
    }
    return special.get(key, key)


@app.get("/")
def root():
    response = send_from_directory(WEB_DIR, "index.html")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/health")
def health():
    return jsonify({
        "ok": True, "application": "LoDeskStream", "instanceId": INSTANCE_ID,
        "pid": os.getpid(), "version": VERSION,
    })


@app.get("/api/monitors")
def monitors():
    _require_token()
    available = _list_monitors(force=True)
    selected = _get_selected_monitor_index(available)
    items = []
    for idx, monitor in enumerate(available, start=1):
        items.append(
            {
                "index": idx,
                "name": f"{monitor['width']}x{monitor['height']} @ ({monitor['left']},{monitor['top']})",
                "width": monitor["width"],
                "height": monitor["height"],
                "left": monitor["left"],
                "top": monitor["top"],
                "current": idx == selected,
            }
        )
    items.sort(key=lambda m: (m["left"], m["top"]))
    return jsonify({"ok": True, "selectedMonitor": selected, "monitors": items})


@app.post("/api/monitor")
def set_monitor():
    _require_token()
    payload = _json_object()
    selected = _sanitize_monitor_index(payload.get("index"), _list_monitors(force=True))
    global _SELECTED_MONITOR_INDEX
    with _STATE_LOCK:
        _SELECTED_MONITOR_INDEX = selected
    return jsonify({"ok": True, "selectedMonitor": selected})


@app.get("/stream")
def stream():
    _require_token()
    requested = request.args.get("monitor")
    if requested is not None:
        if not requested.isascii() or not requested.isdecimal() or len(requested) > 10:
            raise ValidationError("monitor must be a positive integer")
        requested = int(requested)
    selected_monitor = _sanitize_monitor_index(_get_selected_monitor_index() if requested is None else requested)
    return Response(
        _capture_frames(selected_monitor),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/webrtc/offer")
def webrtc_offer():
    _require_token()
    payload = validate_offer(_json_object())
    requested_monitor = payload["monitor"] if "monitor" in payload else _get_selected_monitor_index()
    payload["monitor"] = _sanitize_monitor_index(requested_monitor)
    try:
        return jsonify(_run_webrtc(_create_webrtc_answer(payload)))
    except ValueError as exc:
        return jsonify({"ok": False, "error": f"Invalid WebRTC offer: {exc}"}), 400
    except TimeoutError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 504
    except Exception as exc:
        LOGGER.exception("WebRTC offer failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/session", methods=["POST", "DELETE"])
def control_session():
    _require_token()
    if request.method == "POST":
        return jsonify({"ok": True, "sessionId": _INPUT_SESSIONS.create(), "timeoutSeconds": 15})
    try:
        _INPUT_SESSIONS.close(request.headers.get("X-Session-ID", ""))
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True})


@app.post("/api/session/heartbeat")
def session_heartbeat():
    _require_token()
    try:
        _INPUT_SESSIONS.heartbeat(request.headers.get("X-Session-ID", ""))
    except SessionExpired as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    return jsonify({"ok": True})


@app.post("/api/input")
def input_event():
    _require_token()
    payload = validate_input(_json_object(), _SUPPORTED_KEYS, _translate_key)
    monitors = _list_monitors()
    requested_monitor = payload["monitorIndex"] if "monitorIndex" in payload else _get_selected_monitor_index(monitors)
    payload["monitorIndex"] = _sanitize_monitor_index(requested_monitor, monitors)
    monitor = monitors[payload["monitorIndex"] - 1]
    try:
        with _INPUT_SESSIONS.use(request.headers.get("X-Session-ID", ""), payload.get("sequence", 0)) as session:
            return _apply_input_event(session, payload, monitor)
    except (SessionExpired, ControlBusy, InvalidInputSequence) as exc:
        return jsonify({"ok": False, "error": str(exc), "sessionExpired": isinstance(exc, SessionExpired)}), 409
    except RuntimeError as exc:
        LOGGER.exception("Control session cleanup failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


def _apply_input_event(session, payload, monitor):
    event_type = payload.get("type")

    try:
        if event_type == "mouse_move":
            global _LAST_MOUSE_MOVE
            now = time.perf_counter()
            if now - _LAST_MOUSE_MOVE < _MOUSE_INTERVAL_SECONDS:
                return jsonify({"ok": True, "throttled": True})
            _LAST_MOUSE_MOVE = now
            x, y = _event_screen_position(payload, monitor)
            pyautogui.moveTo(x, y)
            return jsonify({"ok": True, "x": x, "y": y})

        if event_type in {"mouse_down", "mouse_up"}:
            button = _translate_button(int(payload.get("button", 0)))
            has_position = "x" in payload and "y" in payload
            x = y = None
            if has_position:
                x, y = _event_screen_position(payload, monitor)
                pyautogui.moveTo(x, y)
            if event_type == "mouse_down":
                session.buttons.add(button)
                pyautogui.mouseDown(button=button)
            else:
                if button in session.buttons:
                    pyautogui.mouseUp(button=button)
                    session.buttons.remove(button)
            return jsonify({"ok": True, "x": x, "y": y, "button": button})

        if event_type == "mouse_click":
            x, y = _event_screen_position(payload, monitor)
            button = _translate_button(int(payload.get("button", 0)))
            clicks = 2 if bool(payload.get("double")) else 1
            preserve_cursor = bool(payload.get("preserveCursor", False))
            if preserve_cursor:
                original = pyautogui.position()
                pyautogui.click(x=x, y=y, button=button, clicks=clicks)
                pyautogui.moveTo(original.x, original.y)
            else:
                pyautogui.click(x=x, y=y, button=button, clicks=clicks)
            return jsonify({"ok": True, "x": x, "y": y, "button": button})

        if event_type == "scroll":
            amount = int(payload.get("amount", 0))
            pyautogui.scroll(amount)
            return jsonify({"ok": True})

        if event_type == "key":
            key = _translate_key(str(payload.get("key", "")))
            action = payload.get("action", "tap")
            if not key:
                return jsonify({"ok": False, "error": "key is required"}), 400
            if action == "down":
                session.keys.add(key)
                pyautogui.keyDown(key)
            elif action == "up":
                if key in session.keys:
                    pyautogui.keyUp(key)
                    session.keys.remove(key)
            else:
                pyautogui.press(key)
            return jsonify({"ok": True, "key": key})

        if event_type == "text":
            text = str(payload.get("text", ""))
            pyautogui.write(text, interval=0.0)
            return jsonify({"ok": True})
    except Exception as exc:
        LOGGER.exception("Input event failed: %s", event_type)
        return jsonify({"ok": False, "error": str(exc), "event": event_type}), 500

    return jsonify({"ok": False, "error": "unknown event type"}), 400


@app.get("/<path:filename>")
def web_asset(filename: str):
    response = send_from_directory(WEB_DIR, filename)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


if __name__ == "__main__":
    if not os.getenv("STREAM_TOKEN"):
        print(f"STREAM_TOKEN was not set; generated session token: {TOKEN}")
    app.run(host=HOST, port=PORT, threaded=True)
