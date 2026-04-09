from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message


def message_uses_caption(message: Message) -> bool:
    return bool(message.photo or message.video or message.document or message.audio or message.animation)


async def edit_status_message(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    use_caption: bool,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        if use_caption:
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=text,
                reply_markup=reply_markup,
            )
        else:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise

