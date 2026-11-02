"""Configuration loading and validation.

Ordinary settings live in ``config.yaml``; secrets live in ``.env`` (or the real
environment). Nothing secret is ever read from the YAML file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised when the configuration is missing or invalid."""


@dataclass(frozen=True)
class Secrets:
    discord_token: str
    spotify_client_id: str | None = None
    spotify_client_secret: str | None = None
    spotify_refresh_token: str | None = None
    lastfm_api_key: str | None = None

    @property
    def spotify_enabled(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret)

    def values(self) -> list[str]:
        """Every secret value, used by the log redaction filter."""
        return [
            v
            for v in (
                self.discord_token,
                self.spotify_client_id,
                self.spotify_client_secret,
                self.spotify_refresh_token,
                self.lastfm_api_key,
            )
            if v
        ]


@dataclass(frozen=True)
class YtdlpConfig:
    js_runtimes: tuple[str, ...] = ("deno",)
    cookies_file: str | None = None
    force_ipv4: bool = True
    max_concurrent_extractions: int = 3


@dataclass(frozen=True)
class Config:
    owner_ids: tuple[int, ...]
    default_volume: int = 50
    inactivity_timeout_seconds: int = 300
    alone_timeout_seconds: int = 60
    maximum_queue_size: int = 500
    maximum_playlist_size: int = 200
    maximum_track_duration_seconds: int = 3 * 60 * 60
    command_sync_guild_id: int | None = None
    database_path: Path = Path("data/bot.db")
    backup_directory: Path = Path("data/backups")
    backup_interval_hours: int = 24
    backups_to_keep: int = 7
    log_directory: Path = Path("logs")
    log_level: str = "INFO"
    ffmpeg_path: str = "ffmpeg"
    opus_library: str | None = None
    spotify_match_threshold: float = 45.0
    ytdlp: YtdlpConfig = field(default_factory=YtdlpConfig)


_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_JS_RUNTIMES = {"deno", "node", "bun", "quickjs"}


def _int(data: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"'{key}' must be a whole number (got {value!r})")
    if not lo <= value <= hi:
        raise ConfigError(f"'{key}' must be between {lo} and {hi} (got {value})")
    return value


def _discord_id(value: Any, key: str) -> int:
    # YAML parses unquoted snowflakes as ints; quoted ones arrive as strings.
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"'{key}' must be a Discord ID number (got {value!r})")
    if not 10**15 <= value < 10**20:
        raise ConfigError(f"'{key}' does not look like a Discord ID (got {value})")
    return value


def parse_config(data: dict[str, Any], base_dir: Path | None = None) -> Config:
    """Validate a parsed YAML mapping and build a :class:`Config`."""
    if not isinstance(data, dict):
        raise ConfigError("config.yaml must contain a mapping of settings")

    base_dir = base_dir or Path.cwd()

    raw_owner = data.get("owner_ids", data.get("owner_id"))
    if raw_owner is None:
        raise ConfigError("'owner_id' is required (your Discord user ID)")
    raw_owners = raw_owner if isinstance(raw_owner, list) else [raw_owner]
    if not raw_owners:
        raise ConfigError("'owner_id' must contain at least one Discord ID")
    owner_ids = tuple(_discord_id(v, "owner_id") for v in raw_owners)

    guild = data.get("command_sync_guild_id")
    guild_id = None if guild in (None, "", 0) else _discord_id(guild, "command_sync_guild_id")

    log_level = str(data.get("log_level", "INFO")).upper()
    if log_level not in _VALID_LOG_LEVELS:
        raise ConfigError(f"'log_level' must be one of {sorted(_VALID_LOG_LEVELS)}")

    def path(key: str, default: str) -> Path:
        p = Path(str(data.get(key) or default)).expanduser()
        return p if p.is_absolute() else base_dir / p

    yt = data.get("ytdlp") or {}
    if not isinstance(yt, dict):
        raise ConfigError("'ytdlp' must be a mapping")
    runtimes = yt.get("js_runtimes", ["deno"])
    if isinstance(runtimes, str):
        runtimes = [runtimes]
    if not isinstance(runtimes, list) or any(r not in _JS_RUNTIMES for r in runtimes):
        raise ConfigError(f"'ytdlp.js_runtimes' entries must be from {sorted(_JS_RUNTIMES)}")
    cookies = yt.get("cookies_file")
    if cookies:
        cookies_path = Path(str(cookies)).expanduser()
        cookies = str(cookies_path if cookies_path.is_absolute() else base_dir / cookies_path)
    ytdlp = YtdlpConfig(
        js_runtimes=tuple(runtimes),
        cookies_file=cookies or None,
        force_ipv4=bool(yt.get("force_ipv4", True)),
        max_concurrent_extractions=_int(yt, "max_concurrent_extractions", 3, 1, 16),
    )

    threshold = data.get("spotify_match_threshold", 45.0)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ConfigError("'spotify_match_threshold' must be a number")

    return Config(
        owner_ids=owner_ids,
        default_volume=_int(data, "default_volume", 50, 1, 100),
        inactivity_timeout_seconds=_int(data, "inactivity_timeout_seconds", 300, 10, 86_400),
        alone_timeout_seconds=_int(data, "alone_timeout_seconds", 60, 5, 86_400),
        maximum_queue_size=_int(data, "maximum_queue_size", 500, 1, 10_000),
        maximum_playlist_size=_int(data, "maximum_playlist_size", 200, 1, 5_000),
        maximum_track_duration_seconds=_int(
            data, "maximum_track_duration_seconds", 3 * 60 * 60, 0, 7 * 86_400
        ),
        command_sync_guild_id=guild_id,
        database_path=path("database_path", "data/bot.db"),
        backup_directory=path("backup_directory", "data/backups"),
        backup_interval_hours=_int(data, "backup_interval_hours", 24, 0, 24 * 30),
        backups_to_keep=_int(data, "backups_to_keep", 7, 1, 365),
        log_directory=path("log_directory", "logs"),
        log_level=log_level,
        ffmpeg_path=str(data.get("ffmpeg_path") or "ffmpeg"),
        opus_library=data.get("opus_library") or None,
        spotify_match_threshold=float(threshold),
        ytdlp=ytdlp,
    )


def load_config(path: str | os.PathLike[str] = "config.yaml") -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"{path} not found. Copy config.example.yaml to {path.name} and fill in your owner_id."
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    return parse_config(data, base_dir=path.resolve().parent)


def load_secrets(env_file: str | os.PathLike[str] | None = ".env") -> Secrets:
    if env_file and Path(env_file).exists():
        load_dotenv(env_file, override=False)

    def get(name: str) -> str | None:
        value = os.environ.get(name, "").strip()
        return value or None

    token = get("DISCORD_TOKEN")
    if not token:
        raise ConfigError("DISCORD_TOKEN is missing. Put it in .env (see .env.example).")
    cid, csecret = get("SPOTIFY_CLIENT_ID"), get("SPOTIFY_CLIENT_SECRET")
    if bool(cid) != bool(csecret):
        raise ConfigError("Set both SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET, or neither.")
    return Secrets(
        discord_token=token,
        spotify_client_id=cid,
        spotify_client_secret=csecret,
        spotify_refresh_token=get("SPOTIFY_REFRESH_TOKEN"),
        lastfm_api_key=get("LASTFM_API_KEY"),
    )
