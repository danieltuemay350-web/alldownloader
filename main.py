from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from bot.handlers import build_router
from bot.preview import PreviewStore
from bot.services import DeliveryService, MTProtoUploader
from config import settings
from downloader.queue import DownloadManager
from downloader.service import MediaDownloader
from utils.cleanup import CleanupScheduler
from utils.health_server import HealthServer
from utils.logging import setup_logging
from utils.public_links import PublicFileStore
from utils.ratelimit import RateLimiter
from utils.telegram_alerts import TelegramErrorNotifier, TelegramLogHandler


async def main() -> None:
    setup_logging(settings)
    logger = logging.getLogger(__name__)

    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is required. Add it to your environment or .env file.")

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    me = await bot.get_me()
    dp = Dispatcher()
    error_notifier: TelegramErrorNotifier | None = None
    error_handler: TelegramLogHandler | None = None

    if settings.admin_chat_id:
        error_notifier = TelegramErrorNotifier(
            bot=bot,
            chat_id=settings.admin_chat_id,
            bot_username=me.username,
        )
        await error_notifier.start()
        error_handler = TelegramLogHandler(error_notifier, bot_username=me.username)
        error_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        )
        logging.getLogger().addHandler(error_handler)

    cleanup = CleanupScheduler(interval_seconds=30)
    health_server = HealthServer(settings=settings)
    file_store = PublicFileStore(settings=settings, cleanup=cleanup)
    mtproto = MTProtoUploader(settings=settings)
    downloader = MediaDownloader(settings=settings)
    rate_limiter = RateLimiter(
        max_actions=settings.rate_limit_count,
        window_seconds=settings.rate_limit_window_seconds,
    )
    preview_store = PreviewStore(ttl_seconds=settings.file_ttl_seconds)
    delivery = DeliveryService(
        bot=bot,
        settings=settings,
        mtproto=mtproto,
        file_store=file_store,
        cleanup=cleanup,
        bot_username=me.username,
    )
    manager = DownloadManager(
        bot=bot,
        downloader=downloader,
        delivery=delivery,
        rate_limiter=rate_limiter,
        settings=settings,
    )

    dp.include_router(build_router(manager, downloader, preview_store))

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Show welcome message"),
            BotCommand(command="help", description="Show supported platforms"),
            BotCommand(command="video", description="Download video from a URL"),
            BotCommand(command="audio", description="Download MP3 audio from a URL"),
        ]
    )

    await health_server.start()
    await cleanup.start()
    await mtproto.start()
    await manager.start()

    logger.info("Bot is starting polling")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        logger.info("Shutting down services")
        await manager.stop()
        await mtproto.stop()
        await health_server.stop()
        await file_store.stop()
        await cleanup.stop()
        if error_handler is not None:
            logging.getLogger().removeHandler(error_handler)
        if error_notifier is not None:
            await error_notifier.stop()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
