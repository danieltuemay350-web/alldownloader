from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from config import Settings
from downloader.models import DeliveryLink
from utils.cleanup import CleanupScheduler
from utils.files import sanitize_filename

logger = logging.getLogger(__name__)


class PublicFileStore:
    def __init__(self, settings: Settings, cleanup: CleanupScheduler) -> None:
        self.settings = settings
        self.cleanup = cleanup
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_static("/downloads", str(self.settings.public_dir), show_index=False)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.settings.public_host, self.settings.public_port)
        await self._site.start()
        logger.info(
            "Public file server started at %s serving %s",
            self.settings.public_root_url,
            self.settings.public_dir,
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    async def publish_file(self, source_path: Path) -> list[DeliveryLink]:
        target_dir = self.settings.public_dir / uuid.uuid4().hex
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / sanitize_filename(source_path.name)

        await asyncio.to_thread(shutil.move, str(source_path), str(target_path))
        await self.cleanup.schedule(target_dir, self.settings.file_ttl_seconds)
        return [self._link_for(target_path)]

    async def publish_split(self, source_path: Path, chunk_size: int) -> list[DeliveryLink]:
        target_dir = self.settings.public_dir / uuid.uuid4().hex
        target_dir.mkdir(parents=True, exist_ok=True)
        parts = await asyncio.to_thread(self._split_sync, source_path, target_dir, chunk_size)
        await self.cleanup.schedule(target_dir, self.settings.file_ttl_seconds)
        return [self._link_for(part) for part in parts]

    def _link_for(self, path: Path) -> DeliveryLink:
        relative = path.relative_to(self.settings.public_dir)
        url_path = "/".join(quote(part) for part in relative.parts)
        return DeliveryLink(
            name=path.name,
            url=f"{self.settings.public_root_url}/downloads/{url_path}",
            size=path.stat().st_size,
        )

    def _split_sync(self, source_path: Path, target_dir: Path, chunk_size: int) -> list[Path]:
        base_name = sanitize_filename(source_path.stem)
        suffix = source_path.suffix or ".bin"
        parts: list[Path] = []
        buffer_size = 8 * 1024 * 1024

        with source_path.open("rb") as source:
            part_index = 1
            while True:
                target_path = target_dir / f"{base_name}.part{part_index:03d}{suffix}"
                bytes_written = 0
                with target_path.open("wb") as target:
                    while bytes_written < chunk_size:
                        to_read = min(buffer_size, chunk_size - bytes_written)
                        chunk = source.read(to_read)
                        if not chunk:
                            break
                        target.write(chunk)
                        bytes_written += len(chunk)

                if bytes_written == 0:
                    target_path.unlink(missing_ok=True)
                    break

                parts.append(target_path)
                part_index += 1

        source_path.unlink(missing_ok=True)
        return parts

