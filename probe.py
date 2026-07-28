import asyncio

try:
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    )
except ImportError:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    )


async def main():
    mgr = await SessionManager.request_async()
    print("Polling. Play something in Apple Music.\n")
    while True:
        sessions = mgr.get_sessions()
        if not sessions:
            print("(no media sessions)")
        for s in sessions:
            aumid = s.source_app_user_model_id
            try:
                props = await s.try_get_media_properties_async()
            except Exception as e:
                print(f"{aumid}: props failed: {e}")
                continue
            pb = s.get_playback_info()
            tl = s.get_timeline_properties()
            print(
                f"AUMID   : {aumid}\n"
                f"  track : {props.artist!r} - {props.title!r}\n"
                f"  album : {props.album_title!r} (albumArtist={props.album_artist!r})\n"
                f"  status: {pb.playback_status}\n"
                f"  pos   : {tl.position}  end: {tl.end_time}  updated: {tl.last_updated_time}\n"
            )
        await asyncio.sleep(1.0)


asyncio.run(main())