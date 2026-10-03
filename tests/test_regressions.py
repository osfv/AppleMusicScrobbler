"""Regression tests. Run with: py -m unittest discover tests

The Windows media APIs are stubbed so these run on any OS.
"""
import asyncio
import datetime as dt
import os
import socket
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LASTFM_API_KEY", "test-key")
os.environ.setdefault("LASTFM_API_SECRET", "test-secret")
os.environ["DISCORD_CLIENT_ID"] = ""

PLAYING, PAUSED = 4, 5
_control = types.ModuleType("winrt.windows.media.control")
_control.GlobalSystemMediaTransportControlsSessionManager = object
_control.GlobalSystemMediaTransportControlsSessionPlaybackStatus = types.SimpleNamespace(
    PLAYING=PLAYING, PAUSED=PAUSED
)
for _name in ("winsdk", "winsdk.windows", "winsdk.windows.media", "winsdk.windows.media.control"):
    sys.modules.setdefault(_name, None)
for _name in ("winrt", "winrt.windows", "winrt.windows.media"):
    sys.modules.setdefault(_name, types.ModuleType(_name))
sys.modules["winrt.windows.media.control"] = _control

import presence  # noqa: E402
import scrobbler  # noqa: E402


def _json(body):
    r = mock.Mock()
    r.json.return_value = body
    return r


class LastfmKnowsTest(unittest.TestCase):
    def setUp(self):
        scrobbler._artist_cache.clear()

    def test_api_error_other_than_not_found_keeps_credit(self):
        rate_limited = _json({"error": 29, "message": "Rate limit exceeded"})
        with mock.patch.object(scrobbler.requests, "get", return_value=rate_limited):
            self.assertEqual(scrobbler.resolve_artist("Earth, Wind & Fire"), "Earth, Wind & Fire")

    def test_unknown_artist_still_splits(self):
        def fake_get(url, params, timeout):
            if params["artist"] == "1300SAINT & Nine Vicious":
                return _json({"error": 6, "message": "The artist you supplied could not be found"})
            return _json({"artist": {"name": params["artist"]}})

        with mock.patch.object(scrobbler.requests, "get", side_effect=fake_get):
            self.assertEqual(scrobbler.resolve_artist("1300SAINT & Nine Vicious"), "1300SAINT")


class CallTimeoutTest(unittest.TestCase):
    def test_unresponsive_server_does_not_hang(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        accepted = []
        threading.Thread(target=lambda: accepted.append(srv.accept()), daemon=True).start()

        errors = []

        def go():
            try:
                scrobbler.call("track.scrobble", {"sk": "x"}, post=True)
            except requests.RequestException as e:
                errors.append(e)

        with mock.patch.object(scrobbler, "API_ROOT", f"http://127.0.0.1:{srv.getsockname()[1]}/"), \
                mock.patch.object(scrobbler, "HTTP_TIMEOUT", 1, create=True):
            t = threading.Thread(target=go, daemon=True)
            t.start()
            t.join(10)
        srv.close()
        for conn, _ in accepted:
            conn.close()
        self.assertFalse(t.is_alive(), "call() blocked on a server that never answers")
        self.assertEqual(len(errors), 1)


class PausedStartTest(unittest.TestCase):
    def test_track_first_seen_paused(self):
        state = {"status": PAUSED, "tick": 0}
        clock = [1000.0]
        props = types.SimpleNamespace(artist="Artist", title="Track", album_title="Album")

        class Session:
            source_app_user_model_id = "AppleInc.AppleMusicWin_nzyj5cx40ttqa!App"

            async def try_get_media_properties_async(self):
                return props

            def get_playback_info(self):
                return types.SimpleNamespace(playback_status=state["status"])

            def get_timeline_properties(self):
                return types.SimpleNamespace(
                    end_time=dt.timedelta(seconds=200), position=dt.timedelta(seconds=0)
                )

        class Manager:
            def get_sessions(self):
                return [Session()]

            @staticmethod
            async def request_async():
                return Manager()

        async def fake_sleep(_):
            state["tick"] += 1
            clock[0] += 1
            if state["tick"] == 600:
                state["status"] = PLAYING
            if state["tick"] > 800:
                raise asyncio.CancelledError

        calls = []
        with mock.patch.object(scrobbler, "SessionManager", Manager), \
                mock.patch.object(scrobbler, "get_session_key", lambda: "sk"), \
                mock.patch.object(scrobbler, "call", lambda m, p, post=False: calls.append((m, dict(p))) or {}), \
                mock.patch.object(scrobbler.asyncio, "sleep", fake_sleep), \
                mock.patch.object(scrobbler.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(scrobbler.time, "time", lambda: clock[0]), \
                mock.patch("builtins.print"):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(scrobbler.main())
        scrobbler.pending.clear()

        methods = [m for m, _ in calls]
        self.assertIn("track.updateNowPlaying", methods)
        scrobble = next(p for m, p in calls if m == "track.scrobble")
        self.assertEqual(scrobble["timestamp[0]"], 1600)


class ArtworkCacheTest(unittest.TestCase):
    def setUp(self):
        presence._artwork_cache.clear()

    def test_network_failure_is_not_cached(self):
        found = _json({"results": [{"artistName": "Rich Amiri", "artworkUrl100": "http://x/100x100bb.jpg"}]})
        with mock.patch.object(presence.requests, "get", side_effect=requests.ConnectionError("offline")):
            self.assertIsNone(presence.artwork_url("Rich Amiri", "Never Have I", ""))
        with mock.patch.object(presence.requests, "get", return_value=found):
            self.assertEqual(
                presence.artwork_url("Rich Amiri", "Never Have I", ""), "http://x/512x512bb.jpg"
            )

    def test_genuine_miss_is_cached(self):
        empty = _json({"results": []})
        with mock.patch.object(presence.requests, "get", return_value=empty) as get:
            presence.artwork_url("Nobody", "Nothing", "")
            n = get.call_count
            presence.artwork_url("Nobody", "Nothing", "")
            self.assertEqual(get.call_count, n)


if __name__ == "__main__":
    unittest.main()
