from __future__ import annotations

import logging
from dataclasses import dataclass, field

from aiohttp import web

from config import Settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class HealthServer:
    settings: Settings
    version: str = "1.0"
    _runner: web.AppRunner | None = field(init=False, default=None, repr=False)
    _site: web.TCPSite | None = field(init=False, default=None, repr=False)

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._root)
        app.router.add_get("/healthz", self._healthz)
        app.router.add_get("/readyz", self._healthz)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.settings.service_host, self.settings.service_port)
        await self._site.start()
        logger.info(
            "Health server listening on http://%s:%s",
            self.settings.service_host,
            self.settings.service_port,
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    async def _root(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "service": "telegram-media-bot",
                "version": self.version,
            }
        )

    async def _healthz(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})
