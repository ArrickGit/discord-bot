"""Play a test beep, then a few seconds of a YouTube song, into the bot owner's
voice channel, and print what the voice connection is doing every second.

Run it with:  sudo /opt/discord-music-bot/scripts/voicetest.sh
(the script pauses the bot service while the test uses the bot account).
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import shlex
import sys
import time

import discord

from ..client import load_opus
from ..config import Config, load_config, load_secrets
from ..sources.ytdlp import YtdlpSource

BEEP_SECONDS = 6
SONG_SECONDS = 8
SONG_QUERY = "Imagine Dragons Thunder"


def _version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "NOT INSTALLED"


def _state(vc: discord.VoiceClient) -> str:
    conn = vc._connection  # discord.py internals: the DAVE (end-to-end encryption) state
    return (
        f"packets={vc.sequence:<5} dave_version={conn.dave_protocol_version} "
        f"e2ee_ready={conn.can_encrypt} privacy_code={vc.voice_privacy_code or '-'}"
    )


async def _play_for(vc: discord.VoiceClient, source: discord.AudioSource, seconds: int) -> None:
    loop = asyncio.get_running_loop()
    done = asyncio.Event()
    errors: list[Exception | None] = []

    def after(error: Exception | None) -> None:
        errors.append(error)
        loop.call_soon_threadsafe(done.set)

    vc.play(source, after=after)
    for second in range(1, seconds + 1):
        await asyncio.sleep(1)
        print(f"   {second:>2}s  playing={vc.is_playing()!s:<5} {_state(vc)}")
        if done.is_set():
            break
    if vc.is_playing():
        vc.stop()
    await asyncio.wait_for(done.wait(), timeout=5)
    if errors and errors[0]:
        print(f"   error: {errors[0]}")


async def _test(client: discord.Client, cfg: Config) -> int:
    channel = None
    for guild in client.guilds:
        for vc_channel in [*guild.voice_channels, *guild.stage_channels]:
            if any(uid in vc_channel.voice_states for uid in cfg.owner_ids):
                channel = vc_channel
                break
        if channel:
            break
    if channel is None:
        print("You're not in a voice channel the bot can see. Join one, then run this again.")
        return 1

    print(f"Joining '{channel.name}' in '{channel.guild.name}'")
    vc = await channel.connect(timeout=20, self_deaf=True)
    try:
        for _ in range(10):  # give the end-to-end encryption handshake a moment
            if vc._connection.can_encrypt:
                break
            await asyncio.sleep(0.5)
        print(f"   connected: encryption mode={vc.mode}  {_state(vc)}")

        print(f"\n1. Test beep for {BEEP_SECONDS}s (listen now)")
        beep = discord.FFmpegPCMAudio(
            f"sine=frequency=440:duration={BEEP_SECONDS + 2}",
            executable=cfg.ffmpeg_path,
            before_options="-f lavfi",
        )
        await _play_for(vc, discord.PCMVolumeTransformer(beep, volume=1.0), BEEP_SECONDS)

        print(f"\n2. First {SONG_SECONDS}s of '{SONG_QUERY}' from YouTube (listen now)")
        src = YtdlpSource(cfg.ytdlp, 0)
        track = await src.search_track(SONG_QUERY, 0, "voicetest")
        await src.refresh_stream(track)
        wait = track.stream_available_at - time.time()
        if wait > 0:
            print(f"   (waiting {wait:.1f}s for YouTube to make the link available)")
            await asyncio.sleep(wait + 0.5)
        # Same FFmpeg options as GuildPlayer._make_source.
        before = ["-nostdin", "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        if track.stream_headers:
            before += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in track.stream_headers.items())]
        song = discord.FFmpegPCMAudio(
            track.stream_url or "",
            executable=cfg.ffmpeg_path,
            before_options=" ".join(shlex.quote(part) for part in before),
            options="-vn -loglevel error",
        )
        await _play_for(vc, discord.PCMVolumeTransformer(song, volume=cfg.default_volume / 100), SONG_SECONDS)
    finally:
        await vc.disconnect(force=True)

    print("\nDid you hear (1) the beep and (2) the song? Tell Claude which ones, along with this output.")
    return 0


async def _run() -> int:
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else "config.yaml")
    secrets = load_secrets(".env")
    print(f"discord.py {discord.__version__} | davey {_version('davey')} | PyNaCl {_version('PyNaCl')}")
    print(f"opus loaded: {load_opus(cfg.opus_library)}")

    intents = discord.Intents.none()
    intents.guilds = True
    intents.voice_states = True
    client = discord.Client(intents=intents)
    result = {"code": 1}

    @client.event
    async def on_ready() -> None:
        try:
            result["code"] = await _test(client, cfg)
        except Exception as exc:
            print(f"Test failed: {exc!r}")
        finally:
            await client.close()

    await client.start(secrets.discord_token)
    return result["code"]


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
