import io
import os
import asyncio
import logging
import secrets
import sys
import threading
import time
import ctypes
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
from av import AudioFrame, VideoFrame

try:
    import numpy as np
    import soundcard as sc
except ImportError:
    np = None
    sc = None

app = Flask(__name__, static_folder=None)
logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger("lodeskstream")
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(BASE_DIR, "web")

HOST = os.getenv("STREAM_HOST", "0.0.0.0")
PORT = int(os.getenv("STREAM_PORT", "8765"))
TOKEN = os.getenv("STREAM_TOKEN") or secrets.token_urlsafe(18)
FPS = max(1, int(os.getenv("STREAM_FPS", "12")))
JPEG_QUALITY = max(20, min(95, int(os.getenv("STREAM_JPEG_QUALITY", "65"))))
ENABLE_AUDIO = os.getenv("STREAM_AUDIO", "1").lower() not in {"0", "false", "no", "off"}
AUDIO_SAMPLE_RATE = 48000
AUDIO_FRAME_SAMPLES = 960

pyautogui.PAUSE = 0

_LAST_MOUSE_MOVE = 0.0
_MOUSE_INTERVAL_SECONDS = 1.0 / 40.0
_STATE_LOCK = threading.Lock()
_SELECTED_MONITOR_INDEX = 1
_PEERS = set()
_WEBRTC_LOOP = asyncio.new_event_loop()


def _run_webrtc_loop() -> None:
    asyncio.set_event_loop(_WEBRTC_LOOP)
    _WEBRTC_LOOP.run_forever()


threading.Thread(target=_run_webrtc_loop, daemon=True).start()


def _token_ok() -> bool:
    header = request.headers.get("X-Auth-Token", "")
    query = request.args.get("token", "")
    return header == TOKEN or query == TOKEN


def _require_token() -> None:
    if not _token_ok():
        abort(401)


def _list_monitors():
    with mss.mss() as sct:
        return sct.monitors[1:]


def _get_selected_monitor_index() -> int:
    global _SELECTED_MONITOR_INDEX
    with _STATE_LOCK:
        monitors = _list_monitors()
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


def _sanitize_monitor_index(requested_index: int) -> int:
    monitors = _list_monitors()
    if not monitors:
        return 1
    return max(1, min(int(requested_index), len(monitors)))


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

    points = [
        (x, y),
        (x, y + 22),
        (x + 5, y + 17),
        (x + 9, y + 27),
        (x + 13, y + 25),
        (x + 9, y + 15),
        (x + 16, y + 15),
    ]
    draw = ImageDraw.Draw(image)
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        draw.polygon([(px + dx, py + dy) for px, py in points], fill="black")
    draw.polygon(points, fill="white", outline="black")


def _capture_frames(monitor_index: int):
    with mss.mss() as sct:
        frame_delay = 1.0 / FPS
        monitor_index = max(1, min(int(monitor_index), len(sct.monitors) - 1))
        monitor = sct.monitors[monitor_index]
        while True:
            started = time.perf_counter()
            shot = sct.grab(monitor)
            image = Image.frombytes("RGB", shot.size, shot.rgb)
            _draw_host_cursor(image, monitor)
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            jpg = buffer.getvalue()

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Cache-Control: no-cache\r\n\r\n" + jpg + b"\r\n"
            )

            elapsed = time.perf_counter() - started
            wait_time = frame_delay - elapsed
            if wait_time > 0:
                time.sleep(wait_time)


class DesktopVideoTrack(VideoStreamTrack):
    def __init__(self, monitor_index: int):
        super().__init__()
        self._sct = mss.mss()
        self._monitor_index = max(1, min(int(monitor_index), len(self._sct.monitors) - 1))
        self._monitor = self._sct.monitors[self._monitor_index]
        self._frame_interval = 1.0 / FPS
        self._next_frame_at = time.perf_counter()
        self._pts = 0
        self._pts_step = int(90000 / FPS)
        self._time_base = Fraction(1, 90000)

    async def recv(self) -> VideoFrame:
        wait_time = self._next_frame_at - time.perf_counter()
        if wait_time > 0:
            await asyncio.sleep(wait_time)
        self._next_frame_at = max(time.perf_counter(), self._next_frame_at) + self._frame_interval

        shot = self._sct.grab(self._monitor)
        image = Image.frombytes("RGB", shot.size, shot.rgb)
        _draw_host_cursor(image, self._monitor)
        frame = VideoFrame.from_image(image)
        frame.pts = self._pts
        frame.time_base = self._time_base
        self._pts += self._pts_step
        return frame

    def stop(self) -> None:
        super().stop()
        self._sct.close()


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
        super().stop()
        self._recorder.__exit__(None, None, None)


def _run_webrtc(coro):
    return asyncio.run_coroutine_threadsafe(coro, _WEBRTC_LOOP).result(timeout=20)


async def _close_peer(peer: RTCPeerConnection) -> None:
    await peer.close()
    _PEERS.discard(peer)


async def _create_webrtc_answer(payload: dict) -> dict:
    monitor_index = _sanitize_monitor_index(int(payload.get("monitor", _get_selected_monitor_index())))
    peer = RTCPeerConnection()
    _PEERS.add(peer)
    peer.addTrack(DesktopVideoTrack(monitor_index))

    audio_enabled = False
    audio_error = ""
    if ENABLE_AUDIO and bool(payload.get("audio", True)):
        try:
            peer.addTrack(SystemAudioTrack())
            audio_enabled = True
        except Exception as exc:
            audio_error = str(exc)
            LOGGER.warning("WebRTC audio unavailable: %s", audio_error)

    @peer.on("connectionstatechange")
    async def on_connectionstatechange():
        if peer.connectionState == "closed":
            _PEERS.discard(peer)
        elif peer.connectionState in {"failed", "disconnected"}:
            await _close_peer(peer)

    offer = RTCSessionDescription(sdp=str(payload["sdp"]), type=str(payload["type"]))
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


def _translate_button(button_code: int) -> str:
    mapping: Dict[int, str] = {0: "left", 1: "middle", 2: "right"}
    return mapping.get(button_code, "left")


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
    return jsonify({"ok": True})


@app.get("/api/monitors")
def monitors():
    _require_token()
    selected = _get_selected_monitor_index()
    items = []
    for idx, monitor in enumerate(_list_monitors(), start=1):
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
    payload = request.get_json(silent=True) or {}
    selected = int(payload.get("index", 1))
    monitors = _list_monitors()
    if selected < 1 or selected > len(monitors):
        return jsonify({"ok": False, "error": "monitor index out of range"}), 400
    global _SELECTED_MONITOR_INDEX
    with _STATE_LOCK:
        _SELECTED_MONITOR_INDEX = selected
    return jsonify({"ok": True, "selectedMonitor": selected})


@app.get("/stream")
def stream():
    _require_token()
    requested = request.args.get("monitor", type=int)
    selected_monitor = _get_selected_monitor_index() if requested is None else _sanitize_monitor_index(requested)
    return Response(
        _capture_frames(selected_monitor),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/webrtc/offer")
def webrtc_offer():
    _require_token()
    payload = request.get_json(silent=True) or {}
    if not payload.get("sdp") or not payload.get("type"):
        return jsonify({"ok": False, "error": "sdp and type are required"}), 400
    try:
        return jsonify(_run_webrtc(_create_webrtc_answer(payload)))
    except Exception as exc:
        LOGGER.exception("WebRTC offer failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/input")
def input_event():
    _require_token()
    payload = request.get_json(silent=True) or {}
    event_type = payload.get("type")
    requested_monitor = payload.get("monitorIndex")
    selected_monitor = _get_selected_monitor_index()
    if requested_monitor is not None:
        selected_monitor = _sanitize_monitor_index(int(requested_monitor))

    if event_type == "mouse_move":
        global _LAST_MOUSE_MOVE
        now = time.perf_counter()
        if now - _LAST_MOUSE_MOVE < _MOUSE_INTERVAL_SECONDS:
            return jsonify({"ok": True, "throttled": True})
        _LAST_MOUSE_MOVE = now
        with mss.mss() as sct:
            monitor = sct.monitors[selected_monitor]
        x, y = _to_screen_xy(float(payload.get("x", 0.0)), float(payload.get("y", 0.0)), monitor)
        pyautogui.moveTo(x, y)
        return jsonify({"ok": True})

    if event_type == "mouse_click":
        with mss.mss() as sct:
            monitor = sct.monitors[selected_monitor]
        x, y = _to_screen_xy(float(payload.get("x", 0.0)), float(payload.get("y", 0.0)), monitor)
        button = _translate_button(int(payload.get("button", 0)))
        clicks = 2 if bool(payload.get("double")) else 1
        preserve_cursor = bool(payload.get("preserveCursor", False))
        if preserve_cursor:
            original = pyautogui.position()
            pyautogui.click(x=x, y=y, button=button, clicks=clicks)
            pyautogui.moveTo(original.x, original.y)
        else:
            pyautogui.click(x=x, y=y, button=button, clicks=clicks)
        return jsonify({"ok": True})

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
            pyautogui.keyDown(key)
        elif action == "up":
            pyautogui.keyUp(key)
        else:
            pyautogui.press(key)
        return jsonify({"ok": True})

    if event_type == "text":
        text = str(payload.get("text", ""))
        pyautogui.write(text, interval=0.0)
        return jsonify({"ok": True})

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
