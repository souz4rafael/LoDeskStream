import asyncio
import threading
import unittest
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import Mock, patch

from PIL import Image
from mss.screenshot import ScreenShot

import server
from capture_pipeline import CaptureClosed, CaptureError, CaptureManager, CapturedFrame, MonitorCache
from input_sessions import InputSessions


class SyntheticSource:
    def __init__(self, gated=False):
        self.first = threading.Event()
        self.rest = threading.Event()
        self.entered = threading.Event()
        if not gated:
            self.first.set()
            self.rest.set()
        self.opens = []
        self.closes = []
        self.reads = 0

    @contextmanager
    def open(self, monitor):
        owner = threading.get_ident()
        self.opens.append((monitor, owner))

        def read():
            self.entered.set()
            gate = self.first if not self.reads else self.rest
            if not gate.wait(2):
                raise RuntimeError("Synthetic capture gate timed out")
            self.reads += 1
            return Image.new("RGB", (32, 18), (self.reads % 256, 0, 0))

        try:
            yield read
        finally:
            self.closes.append((monitor, threading.get_ident()))


class HostCursorTests(unittest.TestCase):
    monitor = {"left": -100, "top": 50, "width": 80, "height": 60}

    def draw(self, image, x, y):
        with patch.object(server.pyautogui, "position", return_value=Mock(x=x, y=y)):
            server._draw_host_cursor(image, self.monitor)

    def test_circle_is_centered_translucent_and_contrasts_on_light_and_dark(self):
        for background in (0, 255):
            with self.subTest(background=background):
                image = Image.new("RGB", (80, 60), (background,) * 3)
                self.draw(image, -60, 80)
                self.assertEqual(image.mode, "RGB")
                self.assertGreater(image.getpixel((40, 30))[0], 200)
                self.assertLess(image.getpixel((40, 16))[0], 180)
                self.assertGreater(image.getpixel((40, 18))[0], 180)
                self.assertEqual(image.getpixel((32, 30)), image.getpixel((48, 30)))
                self.assertEqual(image.getpixel((40, 22)), image.getpixel((40, 38)))
                self.assertEqual(image.getpixel((0, 0)), (background,) * 3)
                if background == 0:
                    self.assertGreater(image.getpixel((48, 30))[0], 0)
                    self.assertLess(image.getpixel((48, 30))[0], 100)

    def test_circle_clips_at_all_monitor_corners(self):
        for x, y in ((-100, 50), (-21, 50), (-100, 109), (-21, 109)):
            with self.subTest(x=x, y=y):
                image = Image.new("RGB", (80, 60), "black")
                self.draw(image, x, y)
                self.assertGreater(image.getpixel((x + 100, y - 50))[0], 200)

    def test_cursor_on_another_monitor_does_not_change_frame(self):
        for x, y in ((-101, 80), (-20, 80), (-60, 49), (-60, 110)):
            with self.subTest(x=x, y=y):
                image = Image.new("RGB", (80, 60), "black")
                original = image.tobytes()
                self.draw(image, x, y)
                self.assertEqual(image.tobytes(), original)


class CapturePipelineTests(unittest.TestCase):
    def manager(self, source, encoder=None, fps=100, idle_timeout=0.02):
        manager = CaptureManager(source.open, encoder or Mock(return_value=b"jpeg"), fps, idle_timeout)
        self.addCleanup(manager.close)
        self.addCleanup(source.rest.set)
        self.addCleanup(source.first.set)
        return manager

    def test_two_jpeg_clients_share_capture_and_encoding(self):
        source = SyntheticSource(gated=True)
        encoder = Mock(return_value=b"jpeg")
        manager = self.manager(source, encoder)
        first = manager.subscribe(1, jpeg=True)
        second = manager.subscribe(1, jpeg=True)
        source.first.set()
        a = first.next_frame()
        b = second.next_frame()
        self.assertEqual(len(source.opens), 1)
        self.assertEqual(source.reads, 1)
        encoder.assert_called_once()
        self.assertIs(a, b)
        self.assertNotEqual(source.opens[0][1], threading.get_ident())
        first.close()
        second.close()

    def test_video_only_subscription_does_not_encode_jpeg(self):
        source = SyntheticSource(gated=True)
        encoder = Mock()
        manager = self.manager(source, encoder)
        subscription = manager.subscribe(1)
        source.first.set()
        frame = subscription.next_frame()
        self.assertEqual(frame.image.size, (32, 18))
        self.assertIsNone(frame.jpeg)
        encoder.assert_not_called()
        subscription.close()

    def test_jpeg_client_joining_mid_capture_waits_for_an_encoded_frame(self):
        source = SyntheticSource(gated=True)
        manager = self.manager(source)
        video = manager.subscribe(1)
        self.assertTrue(source.entered.wait(1))
        jpeg = manager.subscribe(1, jpeg=True)
        source.first.set()
        self.assertEqual(video.next_frame().sequence, 1)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(jpeg.next_frame)
            self.assertFalse(pending.done())
            source.rest.set()
            frame = pending.result(timeout=2)
        self.assertGreaterEqual(frame.sequence, 2)
        self.assertEqual(frame.jpeg, b"jpeg")
        video.close()
        jpeg.close()

    def test_slow_consumer_receives_latest_frame_not_a_backlog(self):
        source = SyntheticSource()
        manager = self.manager(source)
        subscription = manager.subscribe(1)
        worker = manager._workers[1]
        with worker.condition:
            self.assertTrue(worker.condition.wait_for(
                lambda: worker.latest is not None and worker.latest.sequence >= 5, timeout=2
            ))
        self.assertGreaterEqual(subscription.next_frame().sequence, 5)
        subscription.close()

    def test_last_subscriber_stops_capture_and_releases_on_owning_thread(self):
        source = SyntheticSource()
        manager = self.manager(source)
        subscription = manager.subscribe(1)
        subscription.next_frame()
        worker = manager._workers[1]
        subscription.close()
        subscription.close()
        worker.thread.join(1)
        self.assertFalse(worker.thread.is_alive())
        self.assertEqual(source.opens, source.closes)
        self.assertIsNone(worker.latest)
        replacement = manager.subscribe(1)
        replacement.next_frame()
        self.assertEqual(len(source.opens), 2)
        replacement.close()

    def test_closing_subscription_wakes_a_waiting_consumer(self):
        source = SyntheticSource(gated=True)
        manager = self.manager(source)
        subscription = manager.subscribe(1)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(subscription.next_frame)
            subscription.close()
            with self.assertRaises(CaptureClosed):
                pending.result(timeout=1)

    def test_idle_worker_reuse_waits_for_a_fresh_frame(self):
        source = SyntheticSource(gated=True)
        manager = self.manager(source, idle_timeout=10)
        first = manager.subscribe(1)
        source.first.set()
        old = first.next_frame()
        first.close()
        replacement = manager.subscribe(1)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(replacement.next_frame)
            self.assertFalse(pending.done())
            source.rest.set()
            fresh = pending.result(timeout=2)
        self.assertGreater(fresh.sequence, old.sequence)
        self.assertEqual(len(source.opens), 1)
        replacement.close()

    def test_capture_error_is_logged_and_propagated(self):
        @contextmanager
        def broken(monitor):
            raise RuntimeError("Capture device unavailable")
            yield

        manager = CaptureManager(broken, Mock(), 12)
        self.addCleanup(manager.close)
        with self.assertLogs("lodeskstream.capture", level="ERROR"):
            subscription = manager.subscribe(1)
            with self.assertRaises(CaptureError):
                subscription.next_frame()

    def test_manager_close_rejects_new_consumers(self):
        source = SyntheticSource()
        manager = self.manager(source)
        manager.close()
        with self.assertRaises(CaptureClosed):
            manager.subscribe(1)


class MonitorCacheTests(unittest.TestCase):
    def test_removed_monitor_rejects_stale_input_and_picker_refresh_selects_remaining_monitor(self):
        remaining = {"left": 0, "top": 0, "width": 100, "height": 100}
        sessions = InputSessions(Mock(), Mock())
        headers = {"X-Auth-Token": server.TOKEN, "X-Session-ID": sessions.create()}
        mouse = Mock()
        with patch.object(server, "_list_monitors", return_value=[remaining]), \
                patch.object(server, "_SELECTED_MONITOR_INDEX", 2), \
                patch.object(server, "_INPUT_SESSIONS", sessions), \
                patch.object(server, "pyautogui", mouse):
            client = server.app.test_client()
            response = client.post("/api/input", headers=headers, json={
                "type": "mouse_click", "x": 0.5, "y": 0.5, "monitorIndex": 2, "sequence": 1,
            })
            self.assertEqual(response.status_code, 400)
            mouse.click.assert_not_called()
            self.assertEqual(client.post("/api/monitor", headers=headers, json={"index": 2}).status_code, 400)
            data = client.get("/api/monitors", headers=headers).get_json()
            self.assertEqual(data["selectedMonitor"], 1)
            self.assertEqual(len(data["monitors"]), 1)
            self.assertTrue(data["monitors"][0]["current"])

    def test_removed_or_last_monitor_ends_capture_without_indexing_or_grabbing_another_screen(self):
        for remaining in ([], [{"left": 0, "top": 0, "width": 100, "height": 100}]):
            with self.subTest(remaining=remaining), \
                    patch.object(server, "_list_monitors", return_value=remaining), \
                    patch.object(server.mss, "mss") as source:
                manager = CaptureManager(server._capture_source, Mock(), 12)
                try:
                    with self.assertLogs("lodeskstream.capture", level="ERROR"):
                        subscription = manager.subscribe(2)
                        with self.assertRaises(CaptureError):
                            subscription.next_frame()
                    source.return_value.__enter__.return_value.grab.assert_not_called()
                finally:
                    manager.close()

    def test_cache_hit_expiry_force_refresh_and_copy_isolation(self):
        now = [0.0]
        read = Mock(return_value=[{"left": 0, "top": 0, "width": 100, "height": 100}])
        cache = MonitorCache(read, clock=lambda: now[0])
        first = cache.get()
        first[0]["width"] = 1
        self.assertEqual(cache.get()[0]["width"], 100)
        read.assert_called_once()
        now[0] = 1
        read.return_value = [{"left": -200, "top": 0, "width": 200, "height": 100}]
        self.assertEqual(cache.get()[0]["left"], -200)
        self.assertEqual(read.call_count, 2)
        cache.get(force=True)
        self.assertEqual(read.call_count, 3)

    def test_input_reuses_geometry_without_constructing_mss(self):
        read = Mock(return_value=[{"left": 100, "top": 0, "width": 200, "height": 100}])
        cache = MonitorCache(read, clock=lambda: 0.0)
        mouse = Mock()
        sessions = InputSessions(Mock(), Mock())
        headers = {"X-Auth-Token": server.TOKEN, "X-Session-ID": sessions.create()}
        with patch.object(server, "_MONITOR_CACHE", cache), \
                patch.object(server, "_INPUT_SESSIONS", sessions), \
                patch.object(server, "pyautogui", mouse), \
                patch.object(server.mss, "mss") as mss:
            client = server.app.test_client()
            for sequence in range(1, 11):
                response = client.post("/api/input", headers=headers, json={
                    "type": "mouse_click", "x": 0.5, "y": 0.5, "monitorIndex": 1,
                    "sequence": sequence
                })
                self.assertEqual(response.status_code, 200)
            read.assert_called_once()
            mss.assert_not_called()
            self.assertEqual(mouse.click.call_count, 10)
            mouse.click.assert_called_with(x=199, y=49, button="left", clicks=1)
            self.assertEqual(client.get("/api/monitors", headers=headers).status_code, 200)
            self.assertEqual(read.call_count, 2)
            self.assertEqual(client.post("/api/monitor", headers=headers, json={"index": 1}).status_code, 200)
            self.assertEqual(read.call_count, 3)


class VideoTrackTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_wait_and_conversion_do_not_block_the_event_loop(self):
        entered = threading.Event()
        release = threading.Event()
        owner = []
        image = Image.new("RGB", (32, 18))
        subscription = Mock()

        def read():
            owner.append(threading.get_ident())
            entered.set()
            if not release.wait(2):
                raise CaptureError("Synthetic video timed out")
            return CapturedFrame(1, 10.0, image)

        subscription.next_frame.side_effect = read
        manager = Mock()
        manager.subscribe.return_value = subscription
        with patch.object(server, "_CAPTURE_MANAGER", manager):
            track = server.DesktopVideoTrack(1)
            manager.subscribe.assert_not_called()
            pending = asyncio.create_task(track.recv())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                self.assertFalse(pending.done())
                release.set()
                frame = await pending
                self.assertNotEqual(owner[0], threading.get_ident())
                self.assertEqual(frame.pts, 0)
                self.assertEqual(frame.width, 32)
                subscription.next_frame.side_effect = None
                subscription.next_frame.return_value = CapturedFrame(5, 10.5, image)
                self.assertEqual((await track.recv()).pts, 45000)
            finally:
                release.set()
                track.stop()
                track.stop()
                await asyncio.gather(pending, return_exceptions=True)
            subscription.close.assert_called_once()

    async def test_capture_failure_ends_video_track_and_releases_subscription(self):
        subscription = Mock()
        subscription.next_frame.side_effect = CaptureError("Capture failed")
        manager = Mock()
        manager.subscribe.return_value = subscription
        with patch.object(server, "_CAPTURE_MANAGER", manager):
            track = server.DesktopVideoTrack(1)
            with self.assertRaises(server.MediaStreamError):
                await track.recv()
            self.assertEqual(track.readyState, "ended")
            subscription.close.assert_called_once()


class ResolutionProfileTests(unittest.TestCase):
    def profile(self, settings):
        env = dict(os.environ)
        for key in ["STREAM_PROFILE", "STREAM_FPS", "STREAM_JPEG_QUALITY",
                    "STREAM_MAX_WIDTH", "STREAM_MAX_HEIGHT", "STREAM_JPEG_OPTIMIZE"]:
            env.pop(key, None)
        env.update(settings)
        result = subprocess.run(
            [sys.executable, "-c",
             "import json,server; print(json.dumps([server.FPS,server.JPEG_QUALITY,"
             "server.MAX_WIDTH,server.MAX_HEIGHT,server.JPEG_OPTIMIZE]))"],
            env=env, capture_output=True, text=True, check=True, timeout=30,
        )
        return json.loads(result.stdout)

    def test_profile_defaults_and_explicit_overrides(self):
        self.assertEqual(self.profile({}), [12, 65, 0, 0, True])
        self.assertEqual(self.profile({"STREAM_PROFILE": "legacy"}), [10, 60, 1280, 720, False])
        self.assertEqual(self.profile({
            "STREAM_PROFILE": "legacy", "STREAM_FPS": "15", "STREAM_JPEG_QUALITY": "70",
            "STREAM_MAX_WIDTH": "1600", "STREAM_MAX_HEIGHT": "900", "STREAM_JPEG_OPTIMIZE": "1",
        }), [15, 70, 1600, 900, True])

    def test_default_preserves_native_resolution(self):
        image = Image.new("RGB", (2400, 1600))
        with patch.object(server, "MAX_WIDTH", 0), patch.object(server, "MAX_HEIGHT", 0):
            self.assertIs(server._resize_frame(image), image)

    def test_legacy_limits_preserve_aspect_and_never_upscale(self):
        image = Image.new("RGB", (2400, 1600))
        small = Image.new("RGB", (320, 200))
        with patch.object(server, "MAX_WIDTH", 1280), patch.object(server, "MAX_HEIGHT", 720):
            resized = server._resize_frame(image)
            self.assertEqual(resized.size, (1080, 720))
            self.assertIs(server._resize_frame(small), small)

    def test_native_bgrx_conversion_preserves_rgb_pixels(self):
        source = Image.new("RGB", (4, 2))
        source.putdata([
            (255, 0, 0), (0, 255, 0), (0, 0, 255), (1, 2, 3),
            (255, 255, 255), (0, 0, 0), (10, 20, 30), (100, 150, 200),
        ])
        shot = ScreenShot.from_size(bytearray(source.tobytes("raw", "BGRX")), 4, 2)
        monitor = {"left": 0, "top": 0, "width": 4, "height": 2}
        with patch.object(server.mss, "mss") as factory, \
                patch.object(server, "_list_monitors", return_value=[monitor]), \
                patch.object(server, "_draw_host_cursor"), \
                patch.object(server, "MAX_WIDTH", 0), patch.object(server, "MAX_HEIGHT", 0):
            factory.return_value.__enter__.return_value.grab.return_value = shot
            with server._capture_source(1) as read:
                self.assertEqual(read().tobytes(), source.tobytes())
