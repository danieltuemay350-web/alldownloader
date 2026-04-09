from __future__ import annotations

import asyncio
import dataclasses
import logging
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.message_tools import edit_status_message
from config import Settings
from downloader.exceptions import DownloadCancelledError, DownloadError, RateLimitError, UnsupportedUrlError, UserFacingError
from downloader.models import DownloadArtifact, DownloadRequest
from downloader.service import MediaDownloader
from utils.ratelimit import RateLimiter

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Job:
    job_id: str
    request: DownloadRequest
    enqueued_at: datetime
    status_message_id: int
    status_uses_caption: bool
    canceled: bool = False
    started: bool = False


@dataclass(slots=True)
class SubmissionInfo:
    job_id: str
    position: int


class DownloadManager:
    def __init__(
        self,
        bot: Bot,
        downloader: MediaDownloader,
        delivery,
        rate_limiter: RateLimiter,
        settings: Settings,
    ) -> None:
        self.bot = bot
        self.downloader = downloader
        self.delivery = delivery
        self.rate_limiter = rate_limiter
        self.settings = settings
        self.queue: asyncio.Queue[Job] = asyncio.Queue()
        self.workers: list[asyncio.Task[None]] = []
        self.jobs: dict[str, Job] = {}

    async def start(self) -> None:
        if self.workers:
            return
        for worker_id in range(self.settings.download_concurrency):
            task = asyncio.create_task(self._worker(worker_id + 1), name=f"download-worker-{worker_id + 1}")
            self.workers.append(task)
        logger.info("Started %s download workers", len(self.workers))

    async def stop(self) -> None:
        for task in self.workers:
            task.cancel()
        if self.workers:
            await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()
        self.jobs.clear()

    async def submit(
        self,
        request: DownloadRequest,
        status_message_id: int,
        status_uses_caption: bool,
    ) -> SubmissionInfo:
        prepared = self._prepare_request(request)

        allowed, remaining, retry_after = await self.rate_limiter.consume(prepared.user_id)
        if not allowed:
            raise RateLimitError(
                f"Rate limit reached. You can submit a new request in about {retry_after // 60 or 1} minute(s)."
            )

        job_id = uuid.uuid4().hex[:12]
        position = self.queue.qsize() + 1
        job = Job(
            job_id=job_id,
            request=prepared,
            enqueued_at=datetime.now(UTC),
            status_message_id=status_message_id,
            status_uses_caption=status_uses_caption,
        )
        self.jobs[job_id] = job
        await self.queue.put(job)
        logger.info(
            "Queued download job_id=%s user_id=%s kind=%s url=%s queue_position=%s remaining=%s",
            job_id,
            prepared.user_id,
            prepared.kind.value,
            prepared.url,
            position,
            remaining,
        )
        return SubmissionInfo(job_id=job_id, position=position)

    async def cancel(self, job_id: str, user_id: int) -> str | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        if job.request.user_id != user_id:
            return "forbidden"
        if job.canceled:
            return "already"

        job.canceled = True
        if job.started:
            await self._update_status(
                job,
                "Cancel requested. I will stop before sending anything back.",
                allow_cancel=False,
            )
            return "running"

        await self._update_status(
            job,
            "Canceled. This download has been removed from the queue.",
            allow_cancel=False,
        )
        return "queued"

    def _prepare_request(self, request: DownloadRequest) -> DownloadRequest:
        normalized = self.downloader.normalize_url(request.url)
        return dataclasses.replace(request, url=normalized)

    async def _worker(self, worker_id: int) -> None:
        while True:
            job = await self.queue.get()
            try:
                if job.canceled:
                    continue
                await asyncio.wait_for(
                    self._process(job, worker_id),
                    timeout=self.settings.download_timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning("Download timed out job_id=%s user_id=%s", job.job_id, job.request.user_id)
                await self._update_status(
                    job,
                    "This download took too long, so I stopped it. Please try a smaller or shorter file.",
                    allow_cancel=False,
                )
            except Exception:
                logger.exception("Unexpected worker failure for job_id=%s user_id=%s", job.job_id, job.request.user_id)
                await self._update_status(
                    job,
                    "Something went wrong while processing this download.",
                    allow_cancel=False,
                )
            finally:
                self.jobs.pop(job.job_id, None)
                self.queue.task_done()

    async def _process(self, job: Job, worker_id: int) -> None:
        request = job.request
        job.started = True
        logger.info("Worker %s processing job_id=%s user_id=%s url=%s", worker_id, job.job_id, request.user_id, request.url)

        try:
            await self._update_status(
                job,
                self._status_text(request.option_label, "Checking the link and getting it ready."),
                allow_cancel=True,
            )
            metadata = await self.downloader.inspect(request)

            if job.canceled:
                await self._update_status(job, "Canceled before the download started.", allow_cancel=False)
                return

            await self._update_status(
                job,
                self._status_text(request.option_label, "Downloading now. This can take a little time."),
                allow_cancel=True,
            )
            artifact = await self.downloader.download(
                request,
                metadata,
                progress_callback=lambda payload: self._handle_progress(job, payload),
                cancel_requested=lambda: job.canceled,
            )

            if job.canceled:
                await self._cleanup_artifact(artifact)
                await self._update_status(job, "Canceled. I stopped before sending the file.", allow_cancel=False)
                return

            await self._update_status(
                job,
                self._status_text(request.option_label, "Finishing up and preparing delivery."),
                allow_cancel=False,
            )
            await self.delivery.deliver(
                request.chat_id,
                artifact,
                status_callback=lambda text, allow_cancel=False: self._update_status(job, text, allow_cancel),
            )
            logger.info(
                "Completed job_id=%s user_id=%s platform=%s kind=%s size=%s",
                job.job_id,
                request.user_id,
                metadata.platform,
                request.kind.value,
                artifact.file_size,
            )
        except DownloadCancelledError:
            await self._update_status(job, "Canceled. The active download was stopped.", allow_cancel=False)
        except (UnsupportedUrlError, RateLimitError, DownloadError, UserFacingError) as exc:
            logger.warning("User-facing failure job_id=%s user_id=%s error=%s", job.job_id, request.user_id, exc.user_message)
            await self._update_status(job, exc.user_message, allow_cancel=False)

    async def _update_status(self, job: Job, text: str, allow_cancel: bool) -> None:
        keyboard = self._cancel_keyboard(job.job_id) if allow_cancel and not job.canceled else None
        await edit_status_message(
            bot=self.bot,
            chat_id=job.request.chat_id,
            message_id=job.status_message_id,
            text=text,
            use_caption=job.status_uses_caption,
            reply_markup=keyboard,
        )

    def _cancel_keyboard(self, job_id: str):
        builder = InlineKeyboardBuilder()
        builder.button(text="Cancel", callback_data=f"cancel:{job_id}")
        return builder.as_markup()

    async def _cleanup_artifact(self, artifact: DownloadArtifact) -> None:
        await asyncio.to_thread(shutil.rmtree, artifact.file_path.parent, True)

    def _status_text(self, option_label: str | None, detail: str) -> str:
        if option_label:
            return f"<b>{option_label}</b>\n{detail}"
        return detail

    async def _handle_progress(self, job: Job, payload: dict) -> None:
        if job.canceled:
            return

        status = payload.get("status")
        if status == "processing":
            await self._update_status(
                job,
                self._status_text(job.request.option_label, "Download finished. Processing the media now."),
                allow_cancel=True,
            )
            return

        if status != "downloading":
            return

        percent = payload.get("percent")
        downloaded = payload.get("downloaded_bytes") or 0
        total = payload.get("total_bytes") or 0
        speed = payload.get("speed")
        eta = payload.get("eta")

        details = [f"Downloading: {percent}%"]
        if total:
            details.append(f"{self._human_bytes(downloaded)} of {self._human_bytes(total)}")
        elif downloaded:
            details.append(f"Downloaded: {self._human_bytes(downloaded)}")
        if speed:
            details.append(f"Speed: {self._human_bytes(int(speed))}/s")
        if eta is not None:
            details.append(f"ETA: {self._format_eta(int(eta))}")

        await self._update_status(
            job,
            self._status_text(job.request.option_label, "\n".join(details)),
            allow_cancel=True,
        )

    def _human_bytes(self, value: int) -> str:
        size = float(value)
        if size < 1024:
            return f"{int(size)} B"
        units = ["KB", "MB", "GB", "TB"]
        for unit in units:
            size /= 1024
            if size < 1024 or unit == units[-1]:
                return f"{size:.2f} {unit}"
        return f"{value} B"

    def _format_eta(self, seconds: int) -> str:
        minutes, secs = divmod(max(seconds, 0), 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"
