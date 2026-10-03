import asyncio
import hashlib
import json
import os
import re
import sys
import time
import types
import webbrowser
from pathlib import Path

import requests
from dotenv import load_dotenv

from presence import DiscordPresence

try:
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SessionManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
    )
except ImportError:
    from winrt.windows.media.control import (  # type: ignore[import-not-found]
        GlobalSystemMediaTransportControlsSessionManager as SessionManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
    )

load_dotenv()
API_KEY = os.environ.get("LASTFM_API_KEY", "").strip()
API_SECRET = os.environ.get("LASTFM_API_SECRET", "").strip()
# an empty value would match every media session, browsers included
AUMID_MATCH = (os.environ.get("APPLE_AUMID_MATCH") or "").strip().lower() or "applemusicwin"

SESSION_FILE = Path(__file__).parent / "session.json"
PENDING_FILE = Path(__file__).parent / "pending.json"
API_ROOT = "https://ws.audioscrobbler.com/2.0/"

POLL = 1.0
MIN_TRACK_SECONDS = 30
SCROBBLE_CAP_SECONDS = 240
FLUSH_EVERY = 30.0
MAX_BATCH = 50
REPLAY_WINDOW = 10  # a jump back to within this many seconds of the start is a replay
INVALID_SESSION = 9


# lastfm API helper

def sign(params):
    # sorted key+value pairs, then the secret. format is not signed.
    raw = "".join(f"{k}{params[k]}" for k in sorted(params) if k != "format")
    return hashlib.md5((raw + API_SECRET).encode("utf-8")).hexdigest()


def call(method, params, post=False):
    p = dict(params)
    p["method"] = method
    p["api_key"] = API_KEY
    p["api_sig"] = sign(p)
    p["format"] = "json"
    r = requests.post(API_ROOT, data=p) if post else requests.get(API_ROOT, params=p)
    # last.fm reports errors with a 4xx status and a json body, read the body
    # first or the error code is lost
    try:
        body = r.json()
    except ValueError:
        r.raise_for_status()
        raise
    if "error" in body:
        raise LastfmError(body["error"], body.get("message"))
    r.raise_for_status()
    return body


class LastfmError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(f"last.fm {code}: {message}")
        self.code = code


def get_session_key():
    if SESSION_FILE.exists():
        return json.loads(SESSION_FILE.read_text())["key"]

    token = call("auth.getToken", {})["token"]
    url = "https://www.last.fm/api/auth/?api_key=" + API_KEY + "&token=" + token
    print("Authorize in your browser, then press Enter here.")
    print(url)
    webbrowser.open(url)
    input()

    sess = call("auth.getSession", {"token": token})["session"]
    SESSION_FILE.write_text(json.dumps({"key": sess["key"], "user": sess["name"]}))
    print(f"Logged in as {sess['name']}")
    return sess["key"]


# METADATA

# apple music crams "artist - album" into the artist field and leaves album blank.
# the dash isn't always the same character.
COMBINED_RE = re.compile(r"\s+[‐-―−-]\s+")


def parse_metadata(props):
    artist = (props.artist or "").strip()
    track = (props.title or "").strip()
    album = (props.album_title or "").strip()

    if not album:
        parts = COMBINED_RE.split(artist, maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            artist, album = parts[0].strip(), parts[1].strip()

    return artist, track, album


# apple music puts the whole credit in the artist field: "1300SAINT & Nine
# Vicious". last.fm wants the primary artist alone, or the play lands on a
# combined artist page that has no art and counts for nobody.
# splitting blindly would wreck "Earth, Wind & Fire", so the split only
# happens when last.fm doesn't recognise the combined name.
SPLIT_CREDITS = True
CREDIT_RE = re.compile(r"\s*(?:,|&|\bx\b|\bwith\b|\bfeat\.?|\bft\.?)\s+", re.I)

_artist_cache = {}


def lastfm_knows(name):
    try:
        body = requests.get(
            API_ROOT,
            params={
                "method": "artist.getInfo",
                "artist": name,
                "api_key": API_KEY,
                "autocorrect": 1,
                "format": "json",
            },
            timeout=5,
        ).json()
        return "error" not in body
    except Exception:
        return True  # a network hiccup is no reason to rewrite the credit


def resolve_artist(credit):
    if not SPLIT_CREDITS or not credit:
        return credit
    if credit in _artist_cache:
        return _artist_cache[credit]

    result = credit
    names = [p.strip() for p in CREDIT_RE.split(credit) if p.strip()]
    if len(names) > 1 and not lastfm_knows(credit):
        result = next((n for n in names if lastfm_knows(n)), names[0])
        print(f"  credit: {credit!r} -> {result!r}")

    _artist_cache[credit] = result
    return result


# state of class Play:

class Play:
    def __init__(self, props, duration):
        self.credit, self.track, self.album = parse_metadata(props)
        self.artist = resolve_artist(self.credit)
        self.album_artist = self.artist
        self.duration = duration
        self.started_at = int(time.time())
        self.elapsed = 0.0
        self.scrobbled = False

    @property
    def key(self):
        return (self.artist, self.track, self.album)

    @property
    def threshold(self):
        if not self.duration or self.duration < MIN_TRACK_SECONDS:
            return None
        return min(self.duration / 2, SCROBBLE_CAP_SECONDS)

    def ready(self):
        t = self.threshold
        return t is not None and not self.scrobbled and self.elapsed >= t


pending = []  # saved to PENDING_FILE on exit if it can't be sent


def load_pending():
    if not PENDING_FILE.exists():
        return []
    try:
        return [types.SimpleNamespace(**p) for p in json.loads(PENDING_FILE.read_text())]
    except Exception as e:
        print(f"  ! couldn't read {PENDING_FILE.name}: {e}")
        return []


def save_pending():
    if not pending:
        PENDING_FILE.unlink(missing_ok=True)
        return
    fields = ("artist", "track", "album", "album_artist", "duration", "started_at")
    PENDING_FILE.write_text(json.dumps([{f: getattr(p, f) for f in fields} for p in pending]))
    print(f"  saved {len(pending)} unsent scrobbles to {PENDING_FILE.name}")


def flush(sk):
    if not pending:
        return
    batch = pending[:MAX_BATCH]

    params = {"sk": sk}
    for i, play in enumerate(batch):
        params[f"artist[{i}]"] = play.artist
        params[f"track[{i}]"] = play.track
        params[f"timestamp[{i}]"] = play.started_at
        if play.album:
            params[f"album[{i}]"] = play.album
        if play.album_artist:
            params[f"albumArtist[{i}]"] = play.album_artist
        if play.duration:
            params[f"duration[{i}]"] = play.duration

    try:
        call("track.scrobble", params, post=True)
    except Exception as e:
        print(f"  ! flush failed, holding {len(batch)}: {e}")
        # a revoked key never recovers, so log in again on the next start
        if getattr(e, "code", None) == INVALID_SESSION and SESSION_FILE.exists():
            SESSION_FILE.unlink()
            print("  ! last.fm rejected the session key. Removed session.json, restart to log in again.")
        return False

    del pending[:len(batch)]
    print(f"  > scrobbled {len(batch)}")
    return True


def position_now(tl, playing, duration):
    # the timeline is a snapshot taken at last_updated_time and players only
    # refresh it on play, pause and seek. while playing, add the time since.
    position = tl.position.total_seconds() if tl.position else 0.0
    updated = getattr(tl, "last_updated_time", None)
    if playing and duration and updated:
        position += max(0.0, time.time() - updated.timestamp())
        position = min(position, duration)
    return position


async def apple_session(mgr):
    for s in mgr.get_sessions():
        if AUMID_MATCH in (s.source_app_user_model_id or "").lower():
            return s
    return None


# the watcher that watches apple music

async def main():
    sk = get_session_key()
    mgr = await SessionManager.request_async()
    presence = DiscordPresence()

    pending.extend(load_pending())

    print("Watching Apple Music. Ctrl+C to stop.\n")

    try:
        await watch(mgr, presence, sk)
    finally:
        while pending and flush(sk):
            pass
        save_pending()


async def watch(mgr, presence, sk):
    current = None
    last_tick = time.monotonic()
    last_flush = 0.0
    last_pos = 0.0

    while True:
        before = time.monotonic()
        await asyncio.sleep(POLL)
        now = time.monotonic()
        dt = now - last_tick
        last_tick = now

        # machine was asleep, don't count the gap as listening. only the sleep
        # itself is checked, time spent waiting on last.fm or artwork still counts.
        if now - before > 5:
            dt = 0.0

        # retries run even while apple music is closed
        if pending and now - last_flush > FLUSH_EVERY:
            flush(sk)
            last_flush = now

        s = await apple_session(mgr)
        if s is None:
            if current is not None:
                await presence.clear()
            current = None
            continue

        try:
            props = await s.try_get_media_properties_async()
        except Exception:
            continue

        pb = s.get_playback_info()
        tl = s.get_timeline_properties()
        playing = pb.playback_status == PlaybackStatus.PLAYING

        duration = int(tl.end_time.total_seconds()) if tl.end_time else 0
        position = position_now(tl, playing, duration)

        candidate = Play(props, duration)

        # position jumping back to the start on the same track means it
        # restarted. a seek back mid-track is still the same play.
        replayed = (
            current is not None
            and candidate.key == current.key
            and position + 2 < last_pos
            and position < REPLAY_WINDOW
        )

        if current is None or candidate.key != current.key or replayed:
            if current is not None:
                mark = "scrobbled" if current.scrobbled else f"skipped @{current.elapsed:.0f}s"
                print(f"  ended: {current.artist} - {current.track} ({mark})")

            current = candidate
            last_pos = position
            print(
                f"now playing: artist={current.artist!r} track={current.track!r} "
                f"album={current.album!r} [{duration}s, threshold {current.threshold}]"
            )
            if playing:
                try:
                    call("track.updateNowPlaying", {
                        "artist": current.artist,
                        "track": current.track,
                        "album": current.album,
                        "duration": duration or "",
                        "sk": sk,
                    }, post=True)
                except Exception as e:
                    print(f"  ! nowplaying failed: {e}")
            await presence.update(current, position, duration)
            continue

        last_pos = position
        if playing:
            current.elapsed += dt
            await presence.update(current, position, duration)
        else:
            await presence.clear()

        if current.ready():
            current.scrobbled = True
            pending.append(current)
            print(f"  + queued at {current.elapsed:.0f}s")
            flush(sk)
            last_flush = now


if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        sys.exit("LASTFM_API_KEY and LASTFM_API_SECRET must be set in .env, see .env.example.")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
