"""Find out why YouTube playback fails on this server.

On the server run:  sudo /opt/discord-music-bot/scripts/diagnose.sh

It resolves one song the same way the bot does, then tries to read the audio
two ways: with FFmpeg straight from the stream link (what the bot does), and
with yt-dlp's own downloader. Which one fails tells us where the problem is.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import yt_dlp

from ..config import Config, load_config
from ..models import Track
from ..sources.ytdlp import YtdlpSource

QUERY = "Imagine Dragons Thunder"


def _first_line(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return ((out.stdout or out.stderr).strip().splitlines() or ["(no output)"])[0]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"NOT FOUND ({exc.__class__.__name__})"


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "NOT INSTALLED"


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


async def _resolve(src: YtdlpSource) -> Track:
    track = await src.search_track(QUERY, 0, "diagnose")
    await src.refresh_stream(track)
    return track


def _ffmpeg_test(cfg: Config, track: Track) -> tuple[bool, str]:
    """Read 5 seconds of audio exactly the way the bot's player does."""
    cmd = [cfg.ffmpeg_path, "-nostdin", "-loglevel", "error"]
    cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    if track.stream_headers:
        cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in track.stream_headers.items())]
    cmd += ["-i", track.stream_url or "", "-t", "5", "-vn", "-f", "null", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return False, "timed out after 90s"
    if out.returncode == 0:
        return True, "read 5 seconds of audio"
    lines = [ln for ln in out.stderr.splitlines() if "http" not in ln] or out.stderr.splitlines()
    return False, _short(" | ".join(lines[-3:]))


def _ytdlp_download_test(src: YtdlpSource, track: Track) -> tuple[bool, str]:
    """Let yt-dlp download the audio itself, with its own downloader."""
    with tempfile.TemporaryDirectory() as tmp:
        opts = src._options(
            format="bestaudio/best",
            skip_download=False,
            outtmpl=str(Path(tmp) / "test.%(ext)s"),
        )
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([track.url])
        except Exception as exc:  # report anything yt-dlp raises
            return False, _short(str(exc))
        files = list(Path(tmp).iterdir())
        size = sum(f.stat().st_size for f in files)
        return (size > 0), f"downloaded {size // 1024} KB"


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="  [log] %(message)s")
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else "config.yaml")

    print("Versions")
    print(f"  python      {sys.version.split()[0]}")
    print(f"  yt-dlp      {yt_dlp.version.__version__}")
    print(f"  yt-dlp-ejs  {_package_version('yt-dlp-ejs')}")
    print(f"  deno        {_first_line(['deno', '--version'])}")
    print(f"  ffmpeg      {_first_line([cfg.ffmpeg_path, '-version'])}")
    print(f"  force_ipv4  {cfg.ytdlp.force_ipv4}")
    print(f"  cookies     {'yes' if cfg.ytdlp.cookies_file else 'no'}")

    src = YtdlpSource(cfg.ytdlp, 0)
    print(f"\n1. Looking up '{QUERY}' like the bot does")
    try:
        track = asyncio.run(_resolve(src))
    except Exception as exc:
        print(f"   FAIL  {_short(str(exc))}")
        print("\nResult: yt-dlp can't even get a link. Run scripts/update.sh, then try again.")
        return 1
    query = parse_qs(urlsplit(track.stream_url or "").query)
    print(f"   OK    {track.title}")
    print(f"         client={query.get('c', ['?'])[0]}  headers={len(track.stream_headers or {})}")

    print("\n2. FFmpeg reading the link directly (what the bot does)")
    ff_ok, ff_msg = _ffmpeg_test(cfg, track)
    print(f"   {'OK  ' if ff_ok else 'FAIL'}  {ff_msg}")

    print("\n3. yt-dlp downloading the same song itself")
    dl_ok, dl_msg = _ytdlp_download_test(src, track)
    print(f"   {'OK  ' if dl_ok else 'FAIL'}  {dl_msg}")

    print("\nResult: ", end="")
    if ff_ok:
        print("YouTube playback works here. If the bot still fails, the problem is in the service setup.")
    elif dl_ok:
        print("yt-dlp can download, but FFmpeg can't use the link directly. The bot should stream through yt-dlp.")
    else:
        print("YouTube is refusing this server entirely (see the [log] lines above for why).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
