from __future__ import annotations

import asyncio
import html
import logging
import sys
from datetime import datetime

from aiogram import Bot


class TelegramErrorNotifier:
    def __init__(self, bot: Bot, chat_id: int, bot_username: str | None = None) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.bot_username = bot_username
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._run(), name="telegram-error-notifier")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def enqueue(self, text: str) -> None:
        await self._queue.put(text)

    def enqueue_from_thread(self, text: str) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        future = asyncio.run_coroutine_threadsafe(self.enqueue(text), self._loop)
        future.add_done_callback(lambda f: f.exception())

    async def _run(self) -> None:
        while True:
            text = await self._queue.get()
            try:
                await self.bot.send_message(self.chat_id, text, disable_web_page_preview=True)
            except Exception as exc:
                sys.stderr.write(f"Telegram error notifier failed: {exc}\n")
            finally:
                self._queue.task_done()


class TelegramLogHandler(logging.Handler):
    def __init__(self, notifier: TelegramErrorNotifier, bot_username: str | None = None) -> None:
        super().__init__(level=logging.ERROR)
        self.notifier = notifier
        self.bot_username = bot_username

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("utils.telegram_alerts"):
            return

        try:
            rendered = self.format(record)
            message = self._build_message(record, rendered)
            self.notifier.enqueue_from_thread(message)
        except Exception as exc:
            sys.stderr.write(f"Telegram log handler failed: {exc}\n")

    def _build_message(self, record: logging.LogRecord, rendered: str) -> str:
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        header = ["<b>Bot Error Alert</b>"]
        if self.bot_username:
            header.append(f"Bot: @{html.escape(self.bot_username)}")
        header.append(f"Logger: <code>{html.escape(record.name)}</code>")
        header.append(f"Time: <code>{timestamp}</code>")
        body = html.escape(rendered)

        max_body_length = 3400
        if len(body) > max_body_length:
            body = f"{body[:max_body_length]}..."

        return "\n".join(header) + f"\n<pre>{body}</pre>"

