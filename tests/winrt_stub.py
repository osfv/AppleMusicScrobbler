"""Stand-ins for the Windows media modules so scrobbler imports on any OS."""
import os
import sys
import types

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
