"""Entry point: ``python -m bot``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

import discord

from .config import ConfigError, load_config, load_secrets
from .logging_setup import setup_logging

log = logging.getLogger("bot")


async def _run(config, secrets) -> None:  # type: ignore[no-untyped-def]
    from .client import MusicBot

    bot = MusicBot(config, secrets)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    async with bot:
        runner = asyncio.create_task(bot.start(secrets.discord_token))
        stopper = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait({runner, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if stopper in done:
            log.info("Received stop signal")
            await bot.close()
        else:
            stopper.cancel()
        await runner


def main() -> int:
    parser = argparse.ArgumentParser(description="Discord music bot")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--env", default=".env", help="Path to the .env file with secrets")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
        secrets = load_secrets(args.env)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(config.log_directory, config.log_level, secrets.values())
    try:
        asyncio.run(_run(config, secrets))
    except discord.LoginFailure:
        log.error("Discord rejected the token. Check DISCORD_TOKEN in .env.")
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
