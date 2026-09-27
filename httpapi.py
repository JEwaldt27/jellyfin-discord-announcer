"""A small authenticated HTTP endpoint for posting announcements.

Exists so a maintenance notice can be fired from the shell you are already in
when running updates, or from a pre-upgrade script, without opening Discord.

Security posture: it is a shared-secret endpoint, so anyone holding the token
can post as the bot. It refuses to start without a token, and docker-compose
publishes it on 127.0.0.1 by default. Do not put it behind a public reverse
proxy.
"""

from __future__ import annotations

import hmac
import logging

from aiohttp import web

import announce
from announce import AnnounceError
from version import __version__

log = logging.getLogger(__name__)

DEFAULT_PORT = 8765


class AnnounceAPI:
    def __init__(self, bot, token: str, port: int = DEFAULT_PORT):
        self.bot = bot
        self._token = token
        self.port = port
        self._runner: web.AppRunner | None = None

    # ------------------------------------------------------------- plumbing

    def _authorised(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not value:
            return False
        # Constant-time: a plain == leaks the token prefix through timing.
        return hmac.compare_digest(value, self._token)

    async def _guild(self, payload: dict):
        """Pick the target guild.

        With one guild there is nothing to choose. With several, refusing is
        better than guessing and announcing to the wrong server.
        """
        guilds = list(self.bot.guilds)
        requested = payload.get("guild")

        if requested is not None:
            try:
                wanted = int(requested)
            except (TypeError, ValueError):
                raise AnnounceError("guild must be a numeric Discord guild id.")
            guild = self.bot.get_guild(wanted)
            if guild is None:
                raise AnnounceError(f"The bot is not in guild {wanted}.")
            return guild

        if not guilds:
            raise AnnounceError("The bot is not in any guild yet.")
        if len(guilds) > 1:
            names = ", ".join(f"{g.name} ({g.id})" for g in guilds)
            raise AnnounceError(f'Several guilds - pass "guild". Options: {names}')
        return guilds[0]

    async def _channel(self, guild):
        channel = await announce.announce_channel_for(self.bot.db, guild)
        if channel is None:
            raise AnnounceError(
                f"No announcement channel set for {guild.name}. "
                "Run /channels announce in Discord first."
            )
        return channel

    @staticmethod
    async def _payload(request: web.Request) -> dict:
        if not request.can_read_body:
            return {}
        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001 - any malformed body
            raise AnnounceError("Body must be valid JSON.") from exc
        if not isinstance(payload, dict):
            raise AnnounceError("Body must be a JSON object.")
        return payload

    @web.middleware
    async def _middleware(self, request: web.Request, handler):
        if request.path != "/health":
            if not self._authorised(request):
                log.warning("Rejected unauthenticated %s %s", request.method, request.path)
                return web.json_response({"error": "unauthorised"}, status=401)
            if not self.bot.is_ready():
                return web.json_response(
                    {"error": "bot is not connected to Discord yet"}, status=503
                )
        try:
            return await handler(request)
        except AnnounceError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except web.HTTPException:
            # aiohttp signals 404 and 405 by raising. Letting these reach the
            # catch-all below would turn a wrong URL into a 500 plus a logged
            # traceback, which reads as a bug in the bot.
            raise
        except Exception:  # noqa: BLE001
            log.exception("Unhandled error serving %s", request.path)
            return web.json_response({"error": "internal error"}, status=500)

    # -------------------------------------------------------------- handlers

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"status": "ok", "version": __version__, "ready": self.bot.is_ready()}
        )

    async def post_announce(self, request: web.Request) -> web.Response:
        payload = await self._payload(request)
        guild = await self._guild(payload)
        channel = await self._channel(guild)

        embed = announce.build_announcement(
            payload.get("message"),
            title=payload.get("title"),
            author=payload.get("author") or "the server",
        )
        message = await announce.send(channel, embed)
        log.info("Announcement posted to #%s via HTTP", channel.name)
        return web.json_response(
            {"ok": True, "channel": channel.name, "message_id": message.id}
        )

    async def post_maintenance_start(self, request: web.Request) -> web.Response:
        payload = await self._payload(request)
        guild = await self._guild(payload)
        channel = await self._channel(guild)

        back_at = None
        if payload.get("duration"):
            back_at = announce.utcnow() + announce.parse_duration(str(payload["duration"]))

        reason = payload.get("reason") or payload.get("message")
        embed = announce.build_maintenance(
            reason, back_at=back_at, author=payload.get("author") or "the server"
        )
        message = await announce.send(channel, embed)
        await self.bot.db.start_maintenance(
            guild.id,
            reason=reason.strip(),
            channel_id=channel.id,
            message_id=message.id,
        )
        log.info("Maintenance started in #%s via HTTP", channel.name)
        return web.json_response(
            {"ok": True, "channel": channel.name, "message_id": message.id}
        )

    async def post_maintenance_done(self, request: web.Request) -> web.Response:
        payload = await self._payload(request)
        guild = await self._guild(payload)
        cfg = await self.bot.db.get_config(guild.id)

        if not cfg.in_maintenance:
            raise AnnounceError("No maintenance is currently in progress.")

        channel = (
            announce.resolve_channel(guild, cfg.maintenance_channel_id)
            or await self._channel(guild)
        )
        jump = None
        if cfg.maintenance_channel_id and cfg.maintenance_message_id:
            jump = (
                f"https://discord.com/channels/{guild.id}/"
                f"{cfg.maintenance_channel_id}/{cfg.maintenance_message_id}"
            )

        embed = announce.build_resolved(
            note=payload.get("note"),
            started_at=cfg.maintenance_started_at,
            reason=cfg.maintenance_reason,
            jump_url=jump,
            author=payload.get("author") or "the server",
        )
        message = await announce.send(channel, embed)
        await self.bot.db.clear_maintenance(guild.id)
        log.info("Maintenance cleared in #%s via HTTP", channel.name)
        return web.json_response(
            {"ok": True, "channel": channel.name, "message_id": message.id}
        )

    # ------------------------------------------------------------ lifecycle

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._middleware])
        app.add_routes(
            [
                web.get("/health", self.health),
                web.post("/announce", self.post_announce),
                web.post("/maintenance/start", self.post_maintenance_start),
                web.post("/maintenance/done", self.post_maintenance_done),
            ]
        )
        return app

    async def start(self) -> None:
        self._runner = web.AppRunner(self.build_app(), access_log=None)
        await self._runner.setup()
        # 0.0.0.0 inside the container; what the outside world can reach is
        # decided by the port publish in docker-compose, not here.
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await site.start()
        log.info("Announce endpoint listening on port %d", self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
