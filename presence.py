import asyncio
import os
import re
import sys
import time
import uuid
from urllib.parse import quote_plus

import requests

IMPORT_ERROR = None

# AioPresence, not Presence. The sync client calls run_until_complete() on the
# loop the scrobbler is already running inside, which blows up.
try:
    from pypresence import AioPresence
except ImportError as e:
    AioPresence = None
    IMPORT_ERROR = e

# Activity type 2 = Listening. The payload goes out by hand instead of through
# rpc.update(), which has no slot for the type field and always renders
# as "Playing".
LISTENING = 2

# Discord rate-limits RPC updates to roughly one per 15 seconds.
MIN_INTERVAL = 15.0

# Asset keys from the developer portal, Rich Presence > Art Assets.
# FALLBACK_IMAGE shows when the artwork lookup misses, SMALL_IMAGE is the
# little corner badge.
FALLBACK_IMAGE = "applemusic"
SMALL_IMAGE = "applemusic"

# Buttons only render for other people viewing your profile, never for you.
SHOW_BUTTONS = True
LASTFM_SEARCH = "https://www.last.fm/search?q="

ITUNES = "https://itunes.apple.com/search"
LASTFM_API = "https://ws.audioscrobbler.com/2.0/"

# last.fm serves this blank star image when it has no real cover
LASTFM_BLANK = "2a96cbd8b46e442fc41c2b86b821562f"

FEAT_RE = re.compile(r"\s*[\(\[](?:feat|ft|with)\.?[^\)\]]*[\)\]]", re.I)
SUFFIX_RE = re.compile(r"\s+-\s+(?:single|ep)\s*$", re.I)
SPLIT_RE = re.compile(r"\s*(?:,|&|/|feat\.|ft\.)\s+", re.I)

_artwork_cache = {}


def tidy(s):
    """Drop the parts that make catalogue search miss: feat. tags, Single/EP suffixes."""
    s = FEAT_RE.sub("", s or "")
    s = SUFFIX_RE.sub("", s)
    return s.strip()


def norm(s):
    """Comparison form: letters and digits only, so TOX!C becomes toxc."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def plain(s):
    """Query form: punctuation becomes spaces, since it tanks catalogue search."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]+", " ", s or "")).strip()


def artist_matches(candidate, artist, solo):
    c, a, s = norm(candidate), norm(artist), norm(solo)
    if not c or not a:
        return False
    return c in a or a in c or (len(s) > 2 and (c in s or s in c))


def _itunes(term, entity, artist, solo):
    r = requests.get(
        ITUNES,
        params={"term": term, "media": "music", "entity": entity, "limit": 25},
        timeout=5,
    )
    for item in r.json().get("results") or []:
        raw = item.get("artworkUrl100") or ""
        # without this check, searching a common title returns whoever covered it
        if raw and artist_matches(item.get("artistName"), artist, solo):
            return raw.replace("100x100bb", "512x512bb")
    return None


def _itunes_catalogue(name, track, album, artist, solo):
    """Pull the artist's own catalogue, then find the title inside it.

    A combined term search dies on generic titles: "fflex 99 problems" comes
    back full of the famous one and nothing by fflex. Searching the artist as
    an artist and matching titles locally sidesteps the popularity ranking.
    """
    for entity, field in (("song", "trackName"), ("album", "collectionName")):
        r = requests.get(
            ITUNES,
            params={
                "term": name,
                "media": "music",
                "entity": entity,
                "attribute": "artistTerm",
                "limit": 200,
            },
            timeout=8,
        )
        wanted = [norm(x) for x in (track, album) if x]
        for item in r.json().get("results") or []:
            raw = item.get("artworkUrl100") or ""
            if not raw or not artist_matches(item.get("artistName"), artist, solo):
                continue
            title = norm(tidy(item.get(field) or ""))
            if title and any(title == w or w in title or title in w for w in wanted):
                return raw.replace("100x100bb", "512x512bb")
    return None


def _lastfm(artist, solo, track, album):
    """Second opinion, using the key already in .env for scrobbling."""
    key = os.environ.get("LASTFM_API_KEY", "")
    if not key:
        return None

    attempts = []
    for name in dict.fromkeys([artist, solo]):
        if not name:
            continue
        if album:
            attempts.append(("album.getInfo", {"artist": name, "album": album}))
        attempts.append(("track.getInfo", {"artist": name, "track": track}))

    for method, params in attempts:
        try:
            params.update(method=method, api_key=key, format="json", autocorrect="1")
            data = requests.get(LASTFM_API, params=params, timeout=5).json()
            node = data.get("album") or (data.get("track") or {}).get("album") or {}
            # the image list runs small -> extralarge, so walk it backwards
            for img in reversed(node.get("image") or []):
                url = img.get("#text") or ""
                if url and LASTFM_BLANK not in url:
                    return url
        except Exception:
            continue
    return None


def artwork_url(artist, track, album):
    key = (artist, album or track)
    if key in _artwork_cache:
        return _artwork_cache[key]

    a, t, alb = tidy(artist), tidy(track), tidy(album)
    # "A, B & C" matches nothing in the catalogue. the lead artist usually does.
    solo = SPLIT_RE.split(a, maxsplit=1)[0]

    queries = []
    for name in dict.fromkeys([a, solo, plain(a), plain(solo)]):
        if not name:
            continue
        if alb:
            queries.append((name + " " + alb, "album"))
            queries.append((name + " " + plain(alb), "album"))
        queries.append((name + " " + t, "song"))
        queries.append((name + " " + plain(t), "song"))

    url = None
    for term, entity in dict.fromkeys(queries):
        try:
            url = _itunes(term, entity, a, solo)
        except Exception:
            url = None
        if url:
            break

    if not url:
        for name in dict.fromkeys([a, solo, plain(a), plain(solo)]):
            if not name:
                continue
            try:
                url = _itunes_catalogue(name, t, alb, a, solo)
            except Exception:
                url = None
            if url:
                break

    if not url:
        url = _lastfm(a, solo, t, alb)

    _artwork_cache[key] = url
    return url


class DiscordPresence:
    def __init__(self, client_id=None):
        self.client_id = client_id or os.environ.get("DISCORD_CLIENT_ID", "")
        self.rpc = None
        self.last_sent = 0.0
        self.last_key = None
        self.warned = False
        self.logged_art = False

    def _warn(self, msg):
        if not self.warned:
            print(msg)
            self.warned = True

    async def _connect(self):
        if AioPresence is None:
            self._warn(f"discord: pypresence unusable ({IMPORT_ERROR})")
            return False
        if not self.client_id:
            self._warn("discord: DISCORD_CLIENT_ID missing from .env")
            return False
        try:
            self.rpc = AioPresence(self.client_id)
            await self.rpc.connect()
            print("discord: connected")
            return True
        except Exception as e:
            self.rpc = None
            self._warn(f"discord: not connected ({e})")
            return False

    async def update(self, play, position, duration):
        if self.rpc is None and not await self._connect():
            return

        now = time.time()
        same_track = play.key == self.last_key
        if same_track and now - self.last_sent < MIN_INTERVAL:
            return

        # blocking http call, keep it off the loop
        image = await asyncio.to_thread(
            artwork_url, play.artist, play.track, play.album
        )
        if not self.logged_art:
            print(f"discord: artwork {image or 'not found, falling back to ' + FALLBACK_IMAGE}")
            self.logged_art = True

        activity = {
            "type": LISTENING,
            "details": play.track,
            "state": getattr(play, "credit", None) or play.artist,
            "assets": {
                "large_image": image or FALLBACK_IMAGE,
                "large_text": play.album or play.artist,
                "small_image": SMALL_IMAGE,
                "small_text": "Apple Music",
            },
        }

        # start + end together render the progress bar
        if duration:
            start = int(now - position)
            activity["timestamps"] = {"start": start, "end": start + duration}

        if SHOW_BUTTONS:
            q = quote_plus(play.artist + " " + play.track)
            activity["buttons"] = [
                {"label": "Search on Last.fm", "url": LASTFM_SEARCH + q}
            ]

        try:
            await self._send(activity)
            self.last_sent = now
            self.last_key = play.key
        except Exception as e:
            print(f"discord: update failed ({e})")
            self.rpc = None

    async def _send(self, activity):
        payload = {
            "cmd": "SET_ACTIVITY",
            "args": {"pid": os.getpid(), "activity": activity},
            "nonce": str(uuid.uuid4()),
        }
        self.rpc.send_data(1, payload)
        await self.rpc.read_output()

    async def clear(self):
        # no-op if nothing is showing, otherwise this fires every poll while paused
        if self.rpc is None or self.last_key is None:
            return
        self.last_key = None
        try:
            await self._send(None)
        except Exception:
            self.rpc = None


# diagnostic entry point, the scrobbler is still the real one
if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    if len(sys.argv) < 3:
        print('usage: py presence.py "artist" "track" ["album"]')
    else:
        album = sys.argv[3] if len(sys.argv) > 3 else ""
        print(artwork_url(sys.argv[1], sys.argv[2], album) or "no artwork found")