"""Run with: py -m unittest discover tests"""
import asyncio
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from winrt_stub import PAUSED, PLAYING  # noqa: E402
from fake_lastfm import FakeLastfm  # noqa: E402
import scrobbler  # noqa: E402

UTC = dt.timezone.utc


class Player:
    """A media session that behaves like a real one: the timeline is a
    snapshot that only moves on play, pause and seek."""

    aumid = "AppleInc.AppleMusicWin_nzyj5cx40ttqa!App"

    def __init__(self, clock, title="Track", artist="Artist", album="Album", duration=200):
        self.clock = clock
        self.open = True
        self.props = types.SimpleNamespace(artist=artist, title=title, album_title=album)
        self.duration = duration
        self.status = PLAYING
        self.snap_pos = 0.0
        self.snap_time = clock[0]

    def seek(self, pos):
        self.snap_pos, self.snap_time = pos, self.clock[0]

    def pause(self):
        self.seek(self.snap_pos + self.clock[0] - self.snap_time)
        self.status = PAUSED

    # GlobalSystemMediaTransportControlsSession surface
    @property
    def source_app_user_model_id(self):
        return self.aumid

    async def try_get_media_properties_async(self):
        return self.props

    def get_playback_info(self):
        return types.SimpleNamespace(playback_status=self.status)

    def get_timeline_properties(self):
        return types.SimpleNamespace(
            end_time=dt.timedelta(seconds=self.duration),
            position=dt.timedelta(seconds=self.snap_pos),
            last_updated_time=dt.datetime.fromtimestamp(self.snap_time, UTC),
        )


def run_main(player, ticks, script=None, call=None, clock=None):
    """Drive scrobbler.main() for a number of one-second polls.

    script maps tick number -> callable run before that poll. Returns the
    Last.fm calls made, as (method, params, clock).
    """
    clock = clock or player.clock
    calls = []
    state = {"tick": 0}

    class Manager:
        def get_sessions(self):
            return [player] if player and player.open else []

        @staticmethod
        async def request_async():
            return Manager()

    async def fake_sleep(_):
        state["tick"] += 1
        clock[0] += 1
        if script and state["tick"] in script:
            script[state["tick"]]()
        if state["tick"] > ticks:
            raise asyncio.CancelledError

    def fake_call(method, params, post=False):
        calls.append((method, dict(params), clock[0]))
        return call(method, params) if call else {}

    with mock.patch.object(scrobbler, "SessionManager", Manager), \
            mock.patch.object(scrobbler, "get_session_key", lambda: "sk"), \
            mock.patch.object(scrobbler, "call", fake_call), \
            mock.patch.object(scrobbler.asyncio, "sleep", fake_sleep), \
            mock.patch.object(scrobbler.time, "monotonic", lambda: clock[0]), \
            mock.patch.object(scrobbler.time, "time", lambda: clock[0]), \
            mock.patch("builtins.print"):
        try:
            asyncio.run(scrobbler.main())
        except asyncio.CancelledError:
            pass
    return calls


class TempFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        for name, value in (("SESSION_FILE", tmp / "session.json"), ("PENDING_FILE", tmp / "pending.json")):
            p = mock.patch.object(scrobbler, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)
        scrobbler.pending.clear()
        self.addCleanup(scrobbler.pending.clear)
        self.addCleanup(self.tmp.cleanup)


class RequirementsTest(unittest.TestCase):
    def test_collections_projection_is_listed(self):
        # get_sessions() returns an IVectorView, which needs this projection
        reqs = (ROOT / "requirements.txt").read_text().lower().split()
        self.assertIn("winrt-windows.foundation.collections", reqs)


class ConfigTest(unittest.TestCase):
    def _run(self, **env):
        boot = (
            "import sys, runpy; sys.path[:0] = [{root!r}, {tests!r}]; import winrt_stub; "
            "runpy.run_path({script!r}, run_name='__main__')"
        ).format(root=str(ROOT), tests=str(ROOT / "tests"), script=str(ROOT / "scrobbler.py"))
        full = {k: v for k, v in os.environ.items() if not k.startswith(("LASTFM_", "APPLE_"))}
        full.update(env)
        return subprocess.run(
            [sys.executable, "-c", boot], env=full, capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=60,
        )

    def test_missing_keys_exit_with_a_message(self):
        r = self._run(LASTFM_API_KEY="", LASTFM_API_SECRET="")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("LASTFM_API_KEY", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_empty_aumid_match_falls_back_to_apple_music(self):
        code = (
            "import sys; sys.path[:0] = [{root!r}, {tests!r}]; import winrt_stub, scrobbler; "
            "print(scrobbler.AUMID_MATCH)"
        ).format(root=str(ROOT), tests=str(ROOT / "tests"))
        env = dict(os.environ, APPLE_AUMID_MATCH="")
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "applemusicwin", out.stderr)


class LastfmErrorsTest(TempFiles):
    def setUp(self):
        super().setUp()
        self.fake = FakeLastfm()
        self.addCleanup(self.fake.close)
        p = mock.patch.object(scrobbler, "API_ROOT", self.fake.url)
        p.start()
        self.addCleanup(p.stop)

    def test_error_code_survives_http_error_status(self):
        self.fake.reply("track.scrobble", (403, {"error": 9, "message": "Invalid session key"}))
        with self.assertRaises(RuntimeError) as cm:
            scrobbler.call("track.scrobble", {"sk": "x"}, post=True)
        self.assertEqual(getattr(cm.exception, "code", None), 9)

    def test_rejected_session_key_forces_login_on_next_start(self):
        scrobbler.SESSION_FILE.write_text(json.dumps({"key": "revoked", "user": "u"}))
        self.fake.reply("track.scrobble", (403, {"error": 9, "message": "Invalid session key"}))
        scrobbler.pending.append(types.SimpleNamespace(
            artist="A", track="T", album="", album_artist="A", duration=200, started_at=1))
        with mock.patch("builtins.print"):
            scrobbler.flush("revoked")
        self.assertEqual(len(scrobbler.pending), 1)
        self.assertFalse(scrobbler.SESSION_FILE.exists())

    def test_temporary_error_keeps_session(self):
        scrobbler.SESSION_FILE.write_text(json.dumps({"key": "k", "user": "u"}))
        self.fake.reply("track.scrobble", (503, {"error": 16, "message": "Try again later"}))
        scrobbler.pending.append(types.SimpleNamespace(
            artist="A", track="T", album="", album_artist="A", duration=200, started_at=1))
        with mock.patch("builtins.print"):
            scrobbler.flush("k")
        self.assertTrue(scrobbler.SESSION_FILE.exists())
        self.assertEqual(len(scrobbler.pending), 1)

    def test_batch_and_rate_limit_retry(self):
        self.fake.reply(
            "track.scrobble",
            (429, {"error": 29, "message": "Rate limit exceeded"}),
            (200, {"scrobbles": {"@attr": {"accepted": 50, "ignored": 0}}}),
        )
        for i in range(60):
            scrobbler.pending.append(types.SimpleNamespace(
                artist="A", track=f"T{i}", album="Al", album_artist="A", duration=200, started_at=1000 + i))
        with mock.patch("builtins.print"):
            scrobbler.flush("sk")
            self.assertEqual(len(scrobbler.pending), 60)
            scrobbler.flush("sk")
            self.assertEqual(len(scrobbler.pending), 10)
            scrobbler.flush("sk")
        sent = self.fake.calls("track.scrobble")
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[1]["track[49]"], "T49")
        self.assertNotIn("track[50]", sent[1])
        self.assertEqual(sent[2]["track[9]"], "T59")
        self.assertEqual(scrobbler.pending, [])

    def test_signed_requests_verify(self):
        scrobbler.call("track.updateNowPlaying", {"artist": "A", "track": "T", "sk": "s"}, post=True)
        params = dict(self.fake.calls("track.updateNowPlaying")[0])
        params.pop("format")
        sig = params.pop("api_sig")
        self.assertEqual(sig, scrobbler.sign(params))


class TimelineTest(TempFiles):
    def test_replay_detected_with_snapshot_timeline(self):
        clock = [1000.0]
        player = Player(clock, duration=200)
        # repeat-one: the player restarts the track and refreshes the snapshot
        calls = run_main(player, 260, {201: lambda: player.seek(0)})
        nowplaying = [c for c in calls if c[0] == "track.updateNowPlaying"]
        self.assertEqual(len(nowplaying), 2)

    def test_seek_back_mid_track_keeps_progress(self):
        clock = [1000.0]
        player = Player(clock, duration=200)  # threshold 100
        calls = run_main(player, 110, {80: lambda: player.seek(70)})
        self.assertIn("track.scrobble", [c[0] for c in calls])
        self.assertEqual(len([c for c in calls if c[0] == "track.updateNowPlaying"]), 1)


class LoopTimingTest(TempFiles):
    def test_slow_request_is_not_mistaken_for_sleep(self):
        clock = [1000.0]
        player = Player(clock, duration=60)  # threshold 30

        def slow(method, params):
            if method == "track.updateNowPlaying":
                clock[0] += 8  # last.fm took its time answering
            return {}

        calls = run_main(player, 40, call=slow)
        scrobble = next(c for c in calls if c[0] == "track.scrobble")
        self.assertLessEqual(scrobble[2], 1000 + 32)


def _failing_scrobble(state):
    def call(method, params):
        if method == "track.scrobble" and state["fail"]:
            raise RuntimeError("last.fm 16: try again later")
        return {}
    return call


class ShutdownTest(TempFiles):
    def test_unsent_scrobbles_are_saved_on_exit(self):
        clock = [1000.0]
        player = Player(clock, duration=60)
        run_main(player, 40, call=_failing_scrobble({"fail": True}))
        saved = json.loads(scrobbler.PENDING_FILE.read_text())
        self.assertEqual([(p["artist"], p["track"]) for p in saved], [("Artist", "Track")])

    def test_saved_scrobbles_are_sent_on_next_start(self):
        scrobbler.PENDING_FILE.write_text(json.dumps([{
            "artist": "A", "track": "T", "album": "Al", "album_artist": "A",
            "duration": 200, "started_at": 1234,
        }]))
        calls = run_main(None, 40, clock=[1000.0])
        scrobble = next(p for m, p, _ in calls if m == "track.scrobble")
        self.assertEqual((scrobble["artist[0]"], scrobble["timestamp[0]"]), ("A", 1234))
        self.assertFalse(scrobbler.PENDING_FILE.exists())

    def test_pending_flushed_on_exit(self):
        clock = [1000.0]
        player = Player(clock, duration=60)
        state = {"fail": True}
        calls = run_main(player, 40, {39: lambda: state.update(fail=False)}, call=_failing_scrobble(state))
        self.assertEqual(len([c for c in calls if c[0] == "track.scrobble"]), 2)
        self.assertFalse(scrobbler.PENDING_FILE.exists())

    def test_retries_continue_while_apple_music_is_closed(self):
        clock = [1000.0]
        player = Player(clock, duration=60)
        state = {"fail": True}

        def close():
            player.open = False
            state["fail"] = False

        calls = run_main(player, 120, {35: close}, call=_failing_scrobble(state))
        during_run = [c for c in calls if c[0] == "track.scrobble" and c[2] <= 1000 + 120]
        self.assertEqual(len(during_run), 2)


if __name__ == "__main__":
    unittest.main()
