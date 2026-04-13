from __future__ import annotations

import asyncio
import html
import logging
import shutil
import time
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile
from telethon import TelegramClient
from telethon.network.connection.tcpabridged import ConnectionTcpAbridged

from config import Settings
from downloader.exceptions import DeliveryError
from downloader.models import DownloadArtifact, MediaKind
from utils.cleanup import CleanupScheduler
from utils.files import human_bytes
from utils.public_links import PublicFileStore

logger = logging.getLogger(__name__)


class LinkReason(str, Enum):
    TOO_LARGE = "too_large"
    DELIVERY_FAILED = "delivery_failed"
    MTPROTO_UNAVAILABLE = "mtproto_unavailable"


class MTProtoUploader:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: TelegramClient | None = None

    @property
    def available(self) -> bool:
        return self._client is not None

    async def start(self) -> None:
        if not (self.settings.telegram_api_id and self.settings.telegram_api_hash):
            logger.warning(
                "TELEGRAM_API_ID / TELEGRAM_API_HASH not configured. "
                "Files larger than the Bot API limit will not be deliverable."
            )
            return

        session_base = self._session_base_path()
        session_base.parent.mkdir(parents=True, exist_ok=True)

        try:
            self._client = self._create_client(session_base)
            await self._client.start(bot_token=self.settings.bot_token)
        except (ValueError, TypeError) as exc:
            if not self._looks_like_incompatible_session(exc):
                raise
            logger.warning(
                "MTProto session file is incompatible with the installed Telethon version. "
                "Backing it up and creating a fresh session."
            )
            await self._close_partial_client()
            self._backup_session_files(session_base)
            self._client = self._create_client(session_base)
            await self._client.start(bot_token=self.settings.bot_token)
        logger.info("MTProto uploader is ready")

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    def _session_base_path(self) -> Path:
        return self.settings.runtime_dir / "state" / "bot_mtproto"

    def _create_client(self, session_base: Path) -> TelegramClient:
        return TelegramClient(
            session=str(session_base),
            api_id=self.settings.telegram_api_id,
            api_hash=self.settings.telegram_api_hash,
            connection=ConnectionTcpAbridged,
            request_retries=3,
            connection_retries=3,
        )

    async def _close_partial_client(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.disconnect()
        except Exception:
            logger.debug("Ignoring MTProto client disconnect failure during session recovery", exc_info=True)
        finally:
            self._client = None

    def _looks_like_incompatible_session(self, exc: Exception) -> bool:
        message = str(exc).lower()
        return "too many values to unpack" in message or "not enough values to unpack" in message

    def _backup_session_files(self, session_base: Path) -> None:
        timestamp = int(time.time())
        for suffix in (".session", ".session-journal"):
            path = session_base.with_suffix(suffix)
            if not path.exists():
                continue
            backup_path = path.with_name(f"{path.name}.bak-{timestamp}")
            path.replace(backup_path)

    async def send_file(
        self,
        chat_id: int,
        artifact: DownloadArtifact,
        caption: str | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        if self._client is None:
            raise DeliveryError(
                "MTProto uploader is not configured. Provide TELEGRAM_API_ID and TELEGRAM_API_HASH."
            )

        uploaded = await self._client.upload_file(
            file=str(artifact.file_path),
            file_size=artifact.file_size,
            file_name=artifact.file_path.name,
            part_size_kb=min(max(self.settings.mtproto_part_size_kb, 32), 512),
            progress_callback=progress_callback,
        )
        await self._client.send_file(
            entity=chat_id,
            file=uploaded,
            caption=caption or "",
            supports_streaming=artifact.kind is MediaKind.VIDEO,
        )


class DeliveryService:
    def __init__(
        self,
        bot: Bot,
        settings: Settings,
        mtproto: MTProtoUploader,
        file_store: PublicFileStore,
        cleanup: CleanupScheduler,
        bot_username: str | None = None,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.mtproto = mtproto
        self.file_store = file_store
        self.cleanup = cleanup
        self.bot_username = bot_username

    async def deliver(
        self,
        chat_id: int,
        artifact: DownloadArtifact,
        status_callback: Callable[[str, bool], Awaitable[None]] | None = None,
    ) -> None:
        async def update_status(text: str, allow_cancel: bool = False) -> None:
            if status_callback is not None:
                await status_callback(text, allow_cancel)
            else:
                await self.bot.send_message(chat_id, text)

        file_size = artifact.file_size
        unavailable_reason = (
            LinkReason.TOO_LARGE if file_size > self.settings.bot_api_limit_bytes else LinkReason.DELIVERY_FAILED
        )

        if file_size <= self.settings.bot_api_limit_bytes:
            try:
                await update_status("Sending your file...", False)
                await self._send_via_bot_api(chat_id, artifact)
                await self._delete_local_copy(artifact)
                await update_status("Done. Your file has been sent.", False)
                return
            except Exception as exc:
                logger.warning("Bot API delivery failed for %s, falling back: %s", artifact.file_path.name, exc)
                unavailable_reason = LinkReason.DELIVERY_FAILED

        if file_size <= self.settings.mtproto_limit_bytes and self.mtproto.available:
            await update_status("This will take a few seconds....", False)
            try:
                await self.mtproto.send_file(
                    chat_id,
                    artifact,
                    caption=self._build_caption(artifact),
                    progress_callback=self._build_mtproto_progress_callback(artifact, update_status),
                )
                await self._delete_local_copy(artifact)
                await update_status("Done. Your file has been sent.", False)
                return
            except Exception as exc:
                logger.warning("MTProto delivery failed for %s: %s", artifact.file_path.name, exc)
                await self._delete_local_copy(artifact)
                await update_status(self._format_unavailable_message(artifact, LinkReason.DELIVERY_FAILED), False)
                return

        if file_size <= self.settings.mtproto_limit_bytes and not self.mtproto.available:
            await self._delete_local_copy(artifact)
            await update_status(
                self._format_unavailable_message(
                    artifact,
                    LinkReason.DELIVERY_FAILED if file_size <= self.settings.bot_api_limit_bytes else LinkReason.MTPROTO_UNAVAILABLE,
                ),
                False,
            )
            return

        await self._delete_local_copy(artifact)
        await update_status(self._format_unavailable_message(artifact, unavailable_reason), False)

    async def _send_via_bot_api(self, chat_id: int, artifact: DownloadArtifact) -> None:
        caption = self._build_caption(artifact)
        input_file = FSInputFile(str(artifact.file_path))

        if artifact.kind is MediaKind.AUDIO:
            await self.bot.send_audio(
                chat_id=chat_id,
                audio=input_file,
                caption=caption,
                title=artifact.metadata.title,
                performer=artifact.metadata.uploader,
                duration=_safe_duration(artifact.metadata.duration),
            )
            return

        if artifact.file_path.suffix.lower() in {".mp4", ".mov", ".webm", ".mkv"}:
            try:
                await self.bot.send_video(
                    chat_id=chat_id,
                    video=input_file,
                    caption=caption,
                    duration=_safe_duration(artifact.metadata.duration),
                    supports_streaming=True,
                )
                return
            except TelegramBadRequest as exc:
                logger.warning("send_video failed for %s, retrying as document: %s", artifact.file_path.name, exc)

        await self.bot.send_document(chat_id=chat_id, document=input_file, caption=caption)

    def _format_unavailable_message(self, artifact: DownloadArtifact, reason: LinkReason) -> str:
        lines = [
            html.escape(artifact.metadata.title),
            f"Size: {human_bytes(artifact.file_size)}",
        ]
        if self.bot_username:
            lines.append(f"Downloaded via @{self.bot_username}")

        if reason is LinkReason.MTPROTO_UNAVAILABLE:
            lines.append("This file is too large to send right now because large-file delivery is not configured.")
        elif reason is LinkReason.DELIVERY_FAILED:
            lines.append("I couldn't send this file directly in Telegram.")
        else:
            lines.append("This file is too large to send in Telegram.")
        return "\n".join(lines)

    def _build_caption(self, artifact: DownloadArtifact) -> str:
        lines = [
            html.escape(artifact.metadata.title),
            f"Platform: {html.escape(artifact.metadata.platform.title())}",
            f"Size: {human_bytes(artifact.file_size)}",
        ]
        if self.bot_username:
            lines.append(f"Downloaded via @{self.bot_username}")
        return "\n".join(lines)

    async def _delete_local_copy(self, artifact: DownloadArtifact) -> None:
        await asyncio.to_thread(shutil.rmtree, artifact.file_path.parent, True)

    def _build_mtproto_progress_callback(
        self,
        artifact: DownloadArtifact,
        status_callback: Callable[[str, bool], Awaitable[None]],
    ) -> Callable[[int, int], None]:
        loop = asyncio.get_running_loop()
        last_percent = -1
        last_emit_at = 0.0

        def callback(sent_bytes: int, total_bytes: int) -> None:
            nonlocal last_percent, last_emit_at
            now = time.monotonic()
            percent = int((sent_bytes / total_bytes) * 100) if total_bytes else last_percent
            should_emit = (
                last_percent < 0
                or percent >= last_percent + 3
                or now - last_emit_at >= 2.0
            )
            if not should_emit:
                return

            last_percent = max(percent, 0)
            last_emit_at = now
            text = (
                f"<b>{html.escape(artifact.metadata.title)}</b>\n"
                f"Uploading to Telegram: {last_percent}%\n"
                f"{human_bytes(sent_bytes)} of {human_bytes(total_bytes)}"
            )
            asyncio.run_coroutine_threadsafe(status_callback(text, False), loop)

        return callback


def _safe_duration(value: int | float | None) -> int | None:
    if value is None:
        return None
    return int(round(value))
