import asyncio
import hashlib
import json
import os
import re
import time
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
API_KEY = os.environ["LASTFM_API_KEY"]
API_SECRET = os.environ["LASTFM_API_SECRET"]
AUMID_MATCH = os.environ.get("APPLE_AUMID_MATCH", "applemusicwin").lower()

SESSION_FILE = Path(__file__).parent / "session.json"
API_ROOT = "https://ws.audioscrobbler.com/2.0/"

POLL = 1.0
MIN_TRACK_SECONDS = 30
SCROBBLE_CAP_SECONDS = 240
FLUSH_EVERY = 30.0
MAX_BATCH = 50


# ---------- last.fm plumbing ----------

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
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"last.fm {body['error']}: {body.get('message')}")
    return body


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


# ---------- metadata ----------

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
#
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


# ---------- state ----------

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


pending = []  # dropped on exit, good enough for now


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
        return

    del pending[:len(batch)]
    print(f"  > scrobbled {len(batch)}")


async def apple_session(mgr):
    for s in mgr.get_sessions():
        if AUMID_MATCH in (s.source_app_user_model_id or "").lower():
            return s
    return None


# ---------- the watcher ----------

async def main():
    sk = get_session_key()
    mgr = await SessionManager.request_async()
    presence = DiscordPresence()

    current = None
    last_tick = time.monotonic()
    last_flush = 0.0
    last_pos = 0.0

    print("Watching Apple Music. Ctrl+C to stop.\n")

    while True:
        await asyncio.sleep(POLL)
        now = time.monotonic()
        dt = now - last_tick
        last_tick = now

        # machine was asleep, don't count the gap as listening
        if dt > 5:
            dt = 0.0

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
        position = tl.position.total_seconds() if tl.position else 0.0

        candidate = Play(props, duration)

        # position going backwards on the same track means it restarted
        replayed = (
            current is not None
            and candidate.key == current.key
            and position + 2 < last_pos
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

        if now - last_flush > FLUSH_EVERY:
            flush(sk)
            last_flush = now


asyncio.run(main())