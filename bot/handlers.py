from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.message_tools import edit_status_message, message_uses_caption
from bot.preview import PreviewStore
from downloader.exceptions import DownloadError, RateLimitError, UnsupportedUrlError
from downloader.models import DownloadRequest, FormatOption, MediaKind, MediaMetadata, MediaPreview
from downloader.queue import DownloadManager
from downloader.service import MediaDownloader
from utils.files import human_bytes


WELCOME_TEXT = (
    "Send a link from YouTube, TikTok, Instagram, or Facebook and I will prepare a download card for you.\n\n"
    "You will see the cover image when it is available, then you can tap the exact video or audio version you want.\n\n"
    "Commands:\n"
    "/video &lt;url&gt; - open video choices for a link\n"
    "/audio &lt;url&gt; - open audio choices for a link\n"
    "/help - show supported platforms and limits"
)

HELP_TEXT = (
    "Supported platforms:\n"
    "- YouTube\n"
    "- TikTok\n"
    "- Instagram\n"
    "- Facebook\n\n"
    "How it works:\n"
    "- Send a plain link to see both video and audio choices\n"
    "- Each button shows the format, quality, and approximate size\n"
    "- Tap the option you want and I will download that version for you\n"
    "- You can cancel a queued or active download from the status card\n"
    "- Faster options like original audio and smaller video are included when available\n\n"
    "Delivery rules:\n"
    f"- Up to {human_bytes(50 * 1024**2)}: sent directly in Telegram\n"
    f"- Up to {human_bytes(2 * 1024**3)}: sent with MTProto when configured\n"
    "- Above 2 GB: too large to send in Telegram\n\n"
    "Limits:\n"
    "- 5 download requests per hour per user by default\n"
    "- Maximum duration: unlimited by default\n"
    "- Maximum file size: 10 GB"
)


def build_router(
    manager: DownloadManager,
    downloader: MediaDownloader,
    preview_store: PreviewStore,
) -> Router:
    router = Router(name="downloader")

    @router.message(CommandStart())
    async def start_handler(message: Message) -> None:
        await message.answer(WELCOME_TEXT)

    @router.message(Command("help"))
    async def help_handler(message: Message) -> None:
        await message.answer(HELP_TEXT)

    @router.message(Command("audio"))
    async def audio_handler(message: Message, command: CommandObject) -> None:
        await _show_preview(message, downloader, preview_store, command.args if command else None, MediaKind.AUDIO)

    @router.message(Command("video"))
    async def video_handler(message: Message, command: CommandObject) -> None:
        await _show_preview(message, downloader, preview_store, command.args if command else None, MediaKind.VIDEO)

    @router.message(F.text.regexp(r"^https?://"))
    async def url_handler(message: Message) -> None:
        await _show_preview(message, downloader, preview_store, message.text, None)

    @router.callback_query(F.data.startswith("pick:"))
    async def option_handler(callback: CallbackQuery) -> None:
        await _handle_option_pick(callback, manager, preview_store)

    @router.callback_query(F.data.startswith("cancel:"))
    async def cancel_handler(callback: CallbackQuery) -> None:
        await _handle_cancel(callback, manager)

    @router.message(F.text)
    async def fallback_handler(message: Message) -> None:
        await message.answer("Send a valid media link and I will show you download choices.")

    return router


async def _show_preview(
    message: Message,
    downloader: MediaDownloader,
    preview_store: PreviewStore,
    raw_url: str | None,
    focus_kind: MediaKind | None,
) -> None:
    if message.from_user is None:
        await message.answer("This chat type is not supported for downloads.")
        return

    url = (raw_url or "").strip()
    if not url:
        command = focus_kind.value if focus_kind else "video"
        await message.answer(f"Usage: /{command} &lt;url&gt;")
        return

    try:
        preview = await downloader.preview(url)
    except UnsupportedUrlError as exc:
        await message.answer(exc.user_message)
        return
    except DownloadError as exc:
        await message.answer(exc.user_message)
        return

    session = preview_store.create(message.from_user.id, message.chat.id, preview)
    keyboard = _build_preview_keyboard(session.token, preview, focus_kind)
    caption = _render_preview_caption(preview.metadata, focus_kind)

    if keyboard is None:
        await message.answer("I could read the link, but there are no downloadable formats available for it.")
        return

    if preview.metadata.thumbnail_url:
        try:
            await message.answer_photo(
                photo=preview.metadata.thumbnail_url,
                caption=caption,
                reply_markup=keyboard,
            )
            return
        except TelegramBadRequest:
            pass

    await message.answer(caption, reply_markup=keyboard)


async def _handle_option_pick(
    callback: CallbackQuery,
    manager: DownloadManager,
    preview_store: PreviewStore,
) -> None:
    if callback.from_user is None or callback.message is None or callback.data is None:
        await callback.answer()
        return

    _, token, option_id = callback.data.split(":", maxsplit=2)
    session, option = preview_store.get_option(token, option_id)
    if session is None or option is None:
        await callback.answer("That choice expired. Please send the link again.", show_alert=True)
        return

    if session.user_id != callback.from_user.id:
        await callback.answer("This preview belongs to a different user.", show_alert=True)
        return

    try:
        submission = await manager.submit(
            DownloadRequest(
                user_id=session.user_id,
                chat_id=session.chat_id,
                url=session.preview.metadata.source_url,
                kind=option.kind,
                format_selector=option.selector,
                output_ext=option.output_ext,
                audio_bitrate_kbps=option.audio_bitrate_kbps,
                option_label=option.label,
            ),
            status_message_id=callback.message.message_id,
            status_uses_caption=message_uses_caption(callback.message),
        )
    except UnsupportedUrlError as exc:
        await callback.answer(exc.user_message, show_alert=True)
        return
    except RateLimitError as exc:
        await callback.answer(exc.user_message, show_alert=True)
        return

    await callback.answer("Good choice.")
    await edit_status_message(
        bot=callback.bot,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        text=_friendly_queue_message(option, submission.position),
        use_caption=message_uses_caption(callback.message),
        reply_markup=_cancel_keyboard(submission.job_id),
    )


async def _handle_cancel(callback: CallbackQuery, manager: DownloadManager) -> None:
    if callback.from_user is None or callback.message is None or callback.data is None:
        await callback.answer()
        return

    _, job_id = callback.data.split(":", maxsplit=1)
    result = await manager.cancel(job_id, callback.from_user.id)
    if result is None:
        await callback.answer("This download is already gone.", show_alert=True)
        return
    if result == "forbidden":
        await callback.answer("You can only cancel your own downloads.", show_alert=True)
        return
    if result == "already":
        await callback.answer("This download is already canceled.", show_alert=True)
        return

    if result == "queued":
        await callback.answer("Canceled.")
    else:
        await callback.answer("Cancel requested.")


def _build_preview_keyboard(
    token: str,
    preview: MediaPreview,
    focus_kind: MediaKind | None,
) -> InlineKeyboardMarkup | None:
    builder = InlineKeyboardBuilder()
    options: list[FormatOption] = []

    if focus_kind in {None, MediaKind.VIDEO}:
        options.extend(preview.video_options)
    if focus_kind in {None, MediaKind.AUDIO}:
        options.extend(preview.audio_options)

    if not options:
        return None

    for option in options:
        builder.button(
            text=option.label,
            callback_data=f"pick:{token}:{option.option_id}",
        )
    builder.adjust(1)
    return builder.as_markup()


def _render_preview_caption(metadata: MediaMetadata, focus_kind: MediaKind | None) -> str:
    lines = [
        f"<b>{html.escape(metadata.title)}</b>",
        f"Platform: {html.escape(metadata.platform.title())}",
    ]
    if metadata.uploader:
        lines.append(f"Creator: {html.escape(metadata.uploader)}")
    if metadata.duration:
        lines.append(f"Length: {_format_duration(metadata.duration)}")
    if metadata.size_estimate:
        lines.append(f"Estimated size: {human_bytes(metadata.size_estimate)}")

    lines.append("")
    if focus_kind is MediaKind.AUDIO:
        lines.append("Choose the audio version you want.")
    elif focus_kind is MediaKind.VIDEO:
        lines.append("Choose the video version you want.")
    else:
        lines.append("Choose the version you want. Each button shows the format and approximate size.")
    return "\n".join(lines)


def _friendly_queue_message(option: FormatOption, position: int) -> str:
    if position <= 1:
        return (
            f"<b>{html.escape(option.label)}</b>\n"
            "I am starting this download now. If you change your mind, tap Cancel below."
        )
    ahead = position - 1
    noun = "request" if ahead == 1 else "requests"
    return (
        f"<b>{html.escape(option.label)}</b>\n"
        "Nice choice. I added it to the queue.\n"
        f"There {'is' if ahead == 1 else 'are'} {ahead} {noun} ahead of yours, so it may take a little time."
    )


def _cancel_keyboard(job_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Cancel", callback_data=f"cancel:{job_id}")
    return builder.as_markup()


def _format_duration(total_seconds: int) -> str:
    total_seconds = int(round(total_seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"
