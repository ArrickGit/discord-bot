import logging

import pytest

from bot.config import ConfigError, load_config, load_secrets, parse_config
from bot.logging_setup import RedactingFilter

OWNER = 123456789012345678


def test_minimal_config_defaults(tmp_path):
    cfg = parse_config({"owner_id": OWNER}, base_dir=tmp_path)
    assert cfg.owner_ids == (OWNER,)
    assert cfg.default_volume == 50
    assert cfg.database_path == tmp_path / "data/bot.db"
    assert cfg.command_sync_guild_id is None


def test_owner_id_as_string_or_list():
    assert parse_config({"owner_id": str(OWNER)}).owner_ids == (OWNER,)
    assert parse_config({"owner_ids": [OWNER, OWNER + 1]}).owner_ids == (OWNER, OWNER + 1)


@pytest.mark.parametrize(
    "data,match",
    [
        ({}, "owner_id"),
        ({"owner_id": 123}, "Discord ID"),
        ({"owner_id": "abc"}, "Discord ID"),
        ({"owner_id": OWNER, "default_volume": 0}, "default_volume"),
        ({"owner_id": OWNER, "default_volume": 101}, "default_volume"),
        ({"owner_id": OWNER, "default_volume": "50"}, "whole number"),
        ({"owner_id": OWNER, "default_volume": True}, "whole number"),
        ({"owner_id": OWNER, "maximum_queue_size": 0}, "maximum_queue_size"),
        ({"owner_id": OWNER, "log_level": "LOUD"}, "log_level"),
        ({"owner_id": OWNER, "ytdlp": {"js_runtimes": ["python"]}}, "js_runtimes"),
        ({"owner_id": OWNER, "command_sync_guild_id": 5}, "Discord ID"),
    ],
)
def test_invalid_config(data, match):
    with pytest.raises(ConfigError, match=match):
        parse_config(data)


def test_load_config_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "config.yaml")


def test_load_config_bad_yaml(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("owner_id: [unclosed")
    with pytest.raises(ConfigError, match="YAML"):
        load_config(p)


def test_example_config_is_valid():
    from pathlib import Path

    cfg = load_config(Path(__file__).resolve().parent.parent / "config.example.yaml")
    assert cfg.default_volume == 50


def test_secrets(tmp_path, monkeypatch):
    for key in ("DISCORD_TOKEN", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REFRESH_TOKEN", "LASTFM_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text("DISCORD_TOKEN=abc.def.ghi\nSPOTIFY_CLIENT_ID=\n")
    s = load_secrets(env)
    assert s.discord_token == "abc.def.ghi" and not s.spotify_enabled

    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "id")
    with pytest.raises(ConfigError, match="both"):
        load_secrets(env)


def test_missing_token(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="DISCORD_TOKEN"):
        load_secrets(tmp_path / "nope.env")


def test_log_redaction():
    secret = "super-secret-spotify-key"
    token = "MTIzNDU2Nzg5MDEyMzQ1Njc4.GAbCdE.abcdefghijklmnopqrstuvwxyz0123456789AB"
    f = RedactingFilter([secret])
    record = logging.LogRecord("x", logging.INFO, "", 0, "key=%s token=%s", (secret, token), None)
    f.filter(record)
    msg = record.getMessage()
    assert secret not in msg and token not in msg
    assert msg.count("[REDACTED]") == 2
