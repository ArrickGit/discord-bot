"""One-time Spotify login to let the bot read *your* playlists.

Spotify only returns playlist contents for playlists the logged-in user owns or
collaborates on. Run this once on any computer with a browser:

    python -m bot.tools.spotify_auth

1. In the Spotify developer dashboard, add this Redirect URI to your app:
       http://127.0.0.1:8888/callback
2. Run the command, log in, and approve.
3. Copy the printed SPOTIFY_REFRESH_TOKEN line into .env and restart the bot.
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
import sys
import webbrowser
from urllib.parse import urlencode

import aiohttp
from aiohttp import web
from dotenv import load_dotenv

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "playlist-read-private playlist-read-collaborative"


async def _main() -> int:
    load_dotenv(".env")
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print("Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env first.", file=sys.stderr)
        return 1

    state = secrets.token_urlsafe(16)
    result: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def callback(request: web.Request) -> web.Response:
        if request.query.get("state") != state:
            return web.Response(text="State mismatch. Please run the helper again.", status=400)
        if "error" in request.query:
            if not result.done():
                result.set_exception(RuntimeError(request.query["error"]))
            return web.Response(text="Spotify login was cancelled. You can close this tab.")
        if not result.done():
            result.set_result(request.query.get("code", ""))
        return web.Response(text="Done! You can close this tab and go back to the terminal.")

    app = web.Application()
    app.router.add_get("/callback", callback)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8888)
    await site.start()

    url = "https://accounts.spotify.com/authorize?" + urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
        }
    )
    print("Opening Spotify login. If no browser opens, visit:\n" + url + "\n")
    webbrowser.open(url)

    try:
        code = await asyncio.wait_for(result, timeout=300)
    except (asyncio.TimeoutError, RuntimeError) as exc:
        print(f"Login failed: {exc or 'timed out'}", file=sys.stderr)
        await runner.cleanup()
        return 1
    await runner.cleanup()

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI},
            headers={"Authorization": f"Basic {basic}"},
        ) as resp:
            body = await resp.json(content_type=None)
    token = body.get("refresh_token")
    if not token:
        print(f"Spotify didn't return a refresh token: {body.get('error_description') or body}", file=sys.stderr)
        return 1
    print("Success! Add this line to your .env file (keep it secret):\n")
    print(f"SPOTIFY_REFRESH_TOKEN={token}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
