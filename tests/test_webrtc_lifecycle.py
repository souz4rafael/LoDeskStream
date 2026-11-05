import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import server


class WebRtcLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.video = Mock()
        self.audio = Mock()
        self.peer = Mock()
        self.peer.close = AsyncMock()
        self.peer.setRemoteDescription = AsyncMock()
        self.peer.createAnswer = AsyncMock(return_value=object())
        self.peer.setLocalDescription = AsyncMock()
        self.peer.localDescription = SimpleNamespace(type="answer", sdp="answer-sdp")
        self.peer.on = lambda event: lambda callback: callback
        self.payload = {"type": "offer", "sdp": "offer-sdp", "audio": True, "monitor": 1}
        for item in [
            patch.object(server, "_PEERS", set()),
            patch.object(server, "_PEER_TRACKS", {}),
            patch.object(server, "_sanitize_monitor_index", return_value=1),
            patch.object(server, "_get_selected_monitor_index", return_value=1),
            patch.object(server, "RTCPeerConnection", return_value=self.peer),
            patch.object(server, "DesktopVideoTrack", return_value=self.video),
            patch.object(server, "SystemAudioTrack", return_value=self.audio),
            patch.object(server, "ENABLE_AUDIO", True),
        ]:
            item.start()
            self.addCleanup(item.stop)

    def assert_released(self):
        self.assertNotIn(self.peer, server._PEERS)
        self.assertNotIn(self.peer, server._PEER_TRACKS)
        self.peer.close.assert_awaited_once()

    async def test_each_negotiation_failure_closes_peer_and_tracks(self):
        for stage in ["setRemoteDescription", "createAnswer", "setLocalDescription"]:
            with self.subTest(stage=stage):
                method = getattr(self.peer, stage)
                method.side_effect = ValueError("Invalid negotiation")
                with self.assertRaises(ValueError):
                    await server._create_webrtc_answer(self.payload)
                self.assert_released()
                self.video.stop.assert_called_once()
                self.audio.stop.assert_called_once()
                method.side_effect = None
                self.peer.close.reset_mock()
                self.video.stop.reset_mock()
                self.audio.stop.reset_mock()

    async def test_video_constructor_failure_closes_peer(self):
        with patch.object(server, "DesktopVideoTrack", side_effect=RuntimeError("Capture unavailable")):
            with self.assertRaises(RuntimeError):
                await server._create_webrtc_answer(self.payload)
        self.assert_released()

    async def test_add_track_failure_releases_constructed_track(self):
        self.peer.addTrack.side_effect = RuntimeError("Cannot add track")
        with self.assertRaises(RuntimeError):
            await server._create_webrtc_answer(self.payload)
        self.assert_released()
        self.video.stop.assert_called_once()

    async def test_cancellation_releases_all_tracks(self):
        started = asyncio.Event()

        async def blocked(description):
            started.set()
            await asyncio.Event().wait()

        self.peer.setRemoteDescription.side_effect = blocked
        task = asyncio.create_task(server._create_webrtc_answer(self.payload))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_released()
        self.video.stop.assert_called_once()
        self.audio.stop.assert_called_once()

    async def test_audio_unavailable_preserves_video_only_support(self):
        with patch.object(server, "SystemAudioTrack", side_effect=RuntimeError("No loopback")):
            with self.assertLogs("lodeskstream", level="WARNING"):
                result = await server._create_webrtc_answer(self.payload)
        self.assertFalse(result["audio"])
        self.assertEqual(result["audioError"], "No loopback")
        await server._close_peer(self.peer)
        self.video.stop.assert_called_once()

    async def test_successful_peer_keeps_tracks_until_explicit_close(self):
        result = await server._create_webrtc_answer(self.payload)
        self.assertTrue(result["ok"])
        self.assertIn(self.peer, server._PEERS)
        self.video.stop.assert_not_called()
        await server._close_peer(self.peer)
        await server._close_peer(self.peer)
        self.video.stop.assert_called_once()
        self.audio.stop.assert_called_once()

    async def test_one_track_cleanup_failure_does_not_skip_other_tracks(self):
        await server._create_webrtc_answer(self.payload)
        self.video.stop.side_effect = RuntimeError("Release failed")
        with self.assertLogs("lodeskstream", level="ERROR"):
            with self.assertRaises(RuntimeError):
                await server._close_peer(self.peer)
        self.audio.stop.assert_called_once()

    def test_request_timeout_cancels_the_scheduled_coroutine(self):
        future = Mock()
        future.result.side_effect = TimeoutError()
        with patch.object(server.asyncio, "run_coroutine_threadsafe", return_value=future):
            with self.assertRaisesRegex(TimeoutError, "timed out"):
                server._run_webrtc(object())
        future.cancel.assert_called_once()
