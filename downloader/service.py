from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError as YtDlpDownloadError

from config import Settings
from downloader.exceptions import DownloadCancelledError, DownloadError, MediaTooLargeError, MediaTooLongError, MediaUnavailableError
from downloader.models import DownloadArtifact, DownloadRequest, FormatOption, MediaKind, MediaMetadata, MediaPreview
from downloader.platforms import normalize_url, require_supported_platform
from utils.files import human_bytes, newest_media_file, sanitize_filename

logger = logging.getLogger(__name__)


class MediaDownloader:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def normalize_url(self, url: str) -> str:
        normalized = normalize_url(url)
        require_supported_platform(normalized)
        return normalized

    async def inspect(self, request: DownloadRequest) -> MediaMetadata:
        info = await self._extract_info(request.url)
        return self._build_metadata(request, info)

    async def preview(self, url: str) -> MediaPreview:
        normalized = self.normalize_url(url)
        probe_request = DownloadRequest(
            user_id=0,
            chat_id=0,
            url=normalized,
            kind=MediaKind.VIDEO,
        )
        info = await self._extract_info(normalized)
        metadata = self._build_metadata(probe_request, info)
        return MediaPreview(
            metadata=metadata,
            video_options=self._build_video_options(info),
            audio_options=self._build_audio_options(info),
        )

    async def _extract_info(self, url: str) -> dict[str, Any]:
        attempts = 3 if self._is_tiktok_url(url) else 1
        for attempt in range(1, attempts + 1):
            try:
                return await asyncio.to_thread(self._extract_info_sync, url)
            except YtDlpDownloadError as exc:
                if attempt < attempts and self._is_temporary_source_failure_message(url, str(exc)):
                    delay = min(2.0 * attempt, 6.0)
                    logger.warning(
                        "Temporary source extraction failure for %s, retrying in %.1fs (%s/%s): %s",
                        url,
                        delay,
                        attempt,
                        attempts,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise MediaUnavailableError(self._classify_source_error(exc, url)) from exc
            except DownloadError:
                raise
            except Exception as exc:
                if self._looks_like_ytdlp_failure(exc):
                    raise MediaUnavailableError(self._classify_source_error(exc, url)) from exc
                logger.exception("Unexpected metadata extraction failure for %s", url)
                raise DownloadError("Failed to read media metadata from the source URL.") from exc

        raise DownloadError("Failed to read media metadata from the source URL.")

    def _build_metadata(self, request: DownloadRequest, info: dict[str, Any]) -> MediaMetadata:
        platform = require_supported_platform(request.url)

        duration = info.get("duration")
        if (
            self.settings.max_duration_seconds > 0
            and duration
            and duration > self.settings.max_duration_seconds
        ):
            raise MediaTooLongError(
                f"The media is longer than {self._format_duration_limit(self.settings.max_duration_seconds)} "
                "and cannot be processed."
            )

        size_estimate = self._estimate_size(info, request.kind)
        if size_estimate and size_estimate > self._max_sendable_bytes():
            raise MediaTooLargeError(self._delivery_limit_message())
        if size_estimate and size_estimate > self.settings.max_file_size_bytes:
            raise MediaTooLargeError("The media is larger than the 10 GB processing limit.")

        return MediaMetadata(
            source_url=request.url,
            platform=platform,
            title=sanitize_filename(info.get("title") or info.get("id") or "download"),
            extractor_id=info.get("id"),
            duration=duration,
            size_estimate=size_estimate,
            uploader=info.get("uploader") or info.get("channel") or info.get("creator"),
            thumbnail_url=info.get("thumbnail"),
        )

    async def download(
        self,
        request: DownloadRequest,
        metadata: MediaMetadata,
        progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> DownloadArtifact:
        job_dir = self.settings.temp_dir / f"{uuid.uuid4().hex}"
        job_dir.mkdir(parents=True, exist_ok=True)

        request_variants = self._request_variants_for_fallback(request)

        attempts = 2 if self._is_tiktok_url(request.url) else 1
        file_path: Path | None = None
        try:
            for variant_index, current_request in enumerate(request_variants):
                requested_format_unavailable = False
                for attempt in range(1, attempts + 1):
                    try:
                        file_path = await self._download_with_progress(
                            current_request,
                            metadata,
                            job_dir,
                            progress_callback,
                            cancel_requested,
                        )
                        break
                    except DownloadCancelledError:
                        raise
                    except YtDlpDownloadError as exc:
                        message = str(exc).lower()
                        if "cancel" in message:
                            raise DownloadCancelledError("Canceled. The download was stopped.") from exc
                        if (
                            "requested format is not available" in message
                            and variant_index + 1 < len(request_variants)
                        ):
                            requested_format_unavailable = True
                            logger.warning(
                                "Requested format unavailable for %s (%s); retrying with fallback selector %s/%s",
                                request.url,
                                request.kind.value,
                                variant_index + 2,
                                len(request_variants),
                            )
                            break
                        if attempt < attempts and self._is_temporary_source_failure_message(request.url, message):
                            delay = min(2.0 * attempt, 6.0)
                            logger.warning(
                                "Temporary download failure for %s (%s), retrying in %.1fs (%s/%s): %s",
                                request.url,
                                request.kind.value,
                                delay,
                                attempt,
                                attempts,
                                exc,
                            )
                            await asyncio.sleep(delay)
                            continue
                        logger.exception("yt-dlp download failed for %s (%s)", request.url, request.kind.value)
                        if "ffmpeg is not installed" in message:
                            raise DownloadError(
                                "FFmpeg is required for this download but was not detected. "
                                "Set FFMPEG_BINARY in .env to the full ffmpeg.exe path or restart after adding FFmpeg to PATH."
                            ) from exc
                        raise MediaUnavailableError(self._classify_source_error(exc, request.url)) from exc
                else:
                    continue
                if file_path is not None:
                    break
                if requested_format_unavailable:
                    continue
            else:
                raise DownloadError("The selected quality could not be downloaded from the source platform.")
        except (DownloadCancelledError, DownloadError, MediaUnavailableError):
            raise
        except Exception as exc:
            logger.exception("Unexpected download failure for %s (%s)", request.url, request.kind.value)
            raise DownloadError("The download failed before completion.") from exc

        file_size = file_path.stat().st_size
        if file_size <= 0:
            raise DownloadError("Download finished with an empty output file.")
        if file_size > self._max_sendable_bytes():
            raise MediaTooLargeError(self._delivery_limit_message())
        if file_size > self.settings.max_file_size_bytes:
            raise MediaTooLargeError("The resulting file is larger than the 10 GB processing limit.")

        return DownloadArtifact(
            file_path=file_path,
            file_size=file_size,
            kind=request.kind,
            metadata=metadata,
        )

    async def _download_with_progress(
        self,
        request: DownloadRequest,
        metadata: MediaMetadata,
        job_dir: Path,
        progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Path:
        loop = asyncio.get_running_loop()
        progress_hook = self._build_progress_hook(loop, progress_callback, cancel_requested)
        return await asyncio.to_thread(self._download_sync, request, metadata, job_dir, progress_hook)

    def _extract_info_sync(self, url: str) -> dict[str, Any]:
        options = self._base_ytdlp_options(url, skip_download=True)
        options.update(
            {
                "extract_flat": False,
                "skip_download": True,
            }
        )

        with YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=False)

    def _download_sync(
        self,
        request: DownloadRequest,
        metadata: MediaMetadata,
        job_dir: Path,
        progress_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> Path:
        base_name = sanitize_filename(metadata.title)[:120]
        if metadata.extractor_id:
            base_name = f"{base_name} [{metadata.extractor_id}]"

        options: dict[str, Any] = self._base_ytdlp_options(request.url, skip_download=False)
        options.update(
            {
                "fragment_retries": self.settings.ytdlp_retries,
                "outtmpl": str(job_dir / f"{base_name}.%(ext)s"),
                "concurrent_fragment_downloads": self.settings.ytdlp_fragment_concurrency,
                "continuedl": True,
                "http_chunk_size": self.settings.ytdlp_http_chunk_size_bytes,
            }
        )
        ffmpeg_location = self._resolve_ffmpeg_location()
        if ffmpeg_location:
            options["ffmpeg_location"] = ffmpeg_location
        if progress_hook is not None:
            options["progress_hooks"] = [progress_hook]

        if request.kind is MediaKind.AUDIO:
            audio_quality = request.audio_bitrate_kbps or 320
            options.update(
                {
                    "format": request.format_selector or "bestaudio[acodec!=none]/bestaudio/best",
                }
            )
            if (request.output_ext or "mp3").lower() == "mp3":
                options.update(
                    {
                        "final_ext": "mp3",
                        "postprocessors": [
                            {
                                "key": "FFmpegExtractAudio",
                                "preferredcodec": "mp3",
                                "preferredquality": str(audio_quality),
                            }
                        ],
                        "postprocessor_args": ["-b:a", f"{audio_quality}k"],
                        "prefer_ffmpeg": True,
                        "keepvideo": False,
                    }
                )
        else:
            options.update(
                {
                    "format": request.format_selector or "bestvideo*+bestaudio/best",
                    "merge_output_format": request.output_ext or "mp4",
                }
            )

        with YoutubeDL(options) as ydl:
            ydl.extract_info(request.url, download=True)

        file_path = newest_media_file(job_dir)
        if file_path is None:
            raise DownloadError("Download finished but no media file was produced.")
        return file_path

    def _estimate_size(self, info: dict[str, Any], kind: MediaKind) -> int | None:
        if kind is MediaKind.AUDIO:
            duration = info.get("duration")
            if duration:
                return int((duration * 320_000 / 8) * 1.03)
            return info.get("filesize") or info.get("filesize_approx")

        direct_size = info.get("filesize") or info.get("filesize_approx")
        if direct_size:
            return int(direct_size)

        requested_formats = info.get("requested_formats") or []
        if requested_formats:
            total = 0
            for fmt in requested_formats:
                total += fmt.get("filesize") or fmt.get("filesize_approx") or 0
            if total:
                return int(total)

        formats = info.get("formats") or []
        if not formats:
            return None

        best_audio = 0
        for fmt in formats:
            if fmt.get("vcodec") == "none":
                best_audio = max(best_audio, fmt.get("filesize") or fmt.get("filesize_approx") or 0)

        ranked_video = sorted(
            (fmt for fmt in formats if fmt.get("vcodec") not in {None, "none"}),
            key=lambda item: (item.get("height") or 0, item.get("tbr") or 0),
            reverse=True,
        )
        for fmt in ranked_video:
            size = fmt.get("filesize") or fmt.get("filesize_approx")
            if size:
                return int(size + best_audio)
        return None

    def _build_video_options(self, info: dict[str, Any]) -> list[FormatOption]:
        formats = info.get("formats") or []
        heights = sorted(
            {
                int(fmt.get("height"))
                for fmt in formats
                if fmt.get("vcodec") not in {None, "none"} and fmt.get("height")
            },
            reverse=True,
        )
        if not heights:
            return []

        chosen_heights: list[int] = []
        best_height = heights[0]
        chosen_heights.append(best_height)

        balanced_height = next((height for height in heights if height <= 720), None)
        if balanced_height and balanced_height not in chosen_heights:
            chosen_heights.append(balanced_height)

        small_height = next((height for height in heights if height <= 480), None)
        if small_height and small_height not in chosen_heights:
            chosen_heights.append(small_height)

        for candidate in heights:
            if len(chosen_heights) >= 3:
                break
            if candidate not in chosen_heights:
                chosen_heights.append(candidate)

        options: list[FormatOption] = []
        for index, height in enumerate(chosen_heights, start=1):
            size_hint = self._estimate_video_size_for_height(formats, height)
            size_text = f", ~{human_bytes(size_hint)}" if size_hint else ""
            if index == 1:
                label = f"Best quality video ({height}p MP4{size_text})"
            elif height <= 480:
                label = f"Smaller video ({height}p MP4{size_text})"
            else:
                label = f"Balanced video ({height}p MP4{size_text})"
            options.append(
                FormatOption(
                    option_id=f"v{index}",
                    kind=MediaKind.VIDEO,
                    label=label,
                    selector=self._build_video_selector(formats, height),
                    output_ext="mp4",
                )
            )
        return options

    def _build_audio_options(self, info: dict[str, Any]) -> list[FormatOption]:
        formats = info.get("formats") or []
        has_audio = any(fmt.get("acodec") not in {None, "none"} for fmt in formats)
        if not has_audio:
            return []

        duration = int(round(info.get("duration") or 0))
        original_size = self._estimate_best_audio_size(formats)
        original_text = f", ~{human_bytes(original_size)}" if original_size else ""
        mp3_320_size = self._estimate_mp3_size(duration, 320)
        mp3_192_size = self._estimate_mp3_size(duration, 192)
        mp3_128_size = self._estimate_mp3_size(duration, 128)

        return [
            FormatOption(
                option_id="aorig",
                kind=MediaKind.AUDIO,
                label=f"Original audio (fastest{original_text})",
                selector=self._build_audio_selector(formats, prefer_container="m4a"),
                output_ext="m4a",
            ),
            FormatOption(
                option_id="a320",
                kind=MediaKind.AUDIO,
                label=f"MP3 audio (320 kbps, ~{human_bytes(mp3_320_size)})",
                selector=self._build_audio_selector(formats),
                output_ext="mp3",
                audio_bitrate_kbps=320,
            ),
            FormatOption(
                option_id="a192",
                kind=MediaKind.AUDIO,
                label=f"MP3 audio (192 kbps, ~{human_bytes(mp3_192_size)})",
                selector=self._build_audio_selector(formats),
                output_ext="mp3",
                audio_bitrate_kbps=192,
            ),
            FormatOption(
                option_id="a128",
                kind=MediaKind.AUDIO,
                label=f"Fast MP3 audio (128 kbps, ~{human_bytes(mp3_128_size)})",
                selector=self._build_audio_selector(formats),
                output_ext="mp3",
                audio_bitrate_kbps=128,
            ),
        ]

    def _estimate_best_audio_size(self, formats: list[dict[str, Any]]) -> int | None:
        sizes = [
            fmt.get("filesize") or fmt.get("filesize_approx")
            for fmt in formats
            if fmt.get("acodec") not in {None, "none"}
        ]
        parsed = [int(size) for size in sizes if size]
        return max(parsed) if parsed else None

    def _estimate_mp3_size(self, duration_seconds: int, bitrate_kbps: int) -> int:
        if duration_seconds <= 0:
            return bitrate_kbps * 1024
        return int((duration_seconds * bitrate_kbps * 1000 / 8) * 1.03)

    def _estimate_video_size_for_height(self, formats: list[dict[str, Any]], max_height: int) -> int | None:
        audio_size = self._estimate_best_audio_size(formats) or 0
        video_sizes: list[int] = []
        for fmt in formats:
            if fmt.get("vcodec") in {None, "none"}:
                continue
            height = fmt.get("height")
            if not height or int(height) > max_height:
                continue
            size = fmt.get("filesize") or fmt.get("filesize_approx")
            if size:
                video_sizes.append(int(size))
        if not video_sizes:
            return None
        return max(video_sizes) + audio_size

    def _build_video_selector(self, formats: list[dict[str, Any]], max_height: int) -> str:
        selectors: list[str] = []

        video_only = self._best_video_only_format(formats, max_height, preferred_exts=("mp4", "webm"))
        preferred_audio = self._best_audio_format(formats, preferred_exts=("m4a", "mp4", "webm"))
        fallback_audio = self._best_audio_format(formats, preferred_exts=("webm", "m4a", "mp4"))
        progressive = self._best_progressive_format(formats, max_height, preferred_exts=("mp4", "webm"))

        if video_only and preferred_audio:
            selectors.append(f"{video_only['format_id']}+{preferred_audio['format_id']}")
        if video_only and fallback_audio:
            selectors.append(f"{video_only['format_id']}+{fallback_audio['format_id']}")
        if progressive:
            selectors.append(str(progressive["format_id"]))

        selectors.extend(
            [
                f"best[height<={max_height}]",
                f"bestvideo*[height<={max_height}]+bestaudio/best",
                "best",
            ]
        )
        return "/".join(self._unique_preserve_order(selectors))

    def _build_audio_selector(
        self,
        formats: list[dict[str, Any]],
        *,
        prefer_container: str | None = None,
    ) -> str:
        selectors: list[str] = []
        preferred_exts = tuple(
            ext
            for ext in (
                prefer_container,
                "m4a",
                "webm",
                "mp4",
            )
            if ext
        )
        preferred_audio = self._best_audio_format(formats, preferred_exts=preferred_exts or ("m4a", "webm", "mp4"))
        fallback_audio = self._best_audio_format(formats, preferred_exts=("webm", "m4a", "mp4"))

        if preferred_audio:
            selectors.append(str(preferred_audio["format_id"]))
        if fallback_audio:
            selectors.append(str(fallback_audio["format_id"]))

        selectors.extend(["bestaudio/best", "best"])
        return "/".join(self._unique_preserve_order(selectors))

    def _best_progressive_format(
        self,
        formats: list[dict[str, Any]],
        max_height: int,
        *,
        preferred_exts: tuple[str, ...],
    ) -> dict[str, Any] | None:
        candidates = [
            fmt
            for fmt in formats
            if fmt.get("format_id")
            and fmt.get("vcodec") not in {None, "none"}
            and fmt.get("acodec") not in {None, "none"}
            and (fmt.get("height") or 0) <= max_height
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda fmt: (
                self._ext_preference(fmt.get("ext"), preferred_exts),
                -(fmt.get("height") or 0),
                -(fmt.get("fps") or 0),
                -(fmt.get("tbr") or 0),
                -int(fmt.get("filesize") or fmt.get("filesize_approx") or 0),
            ),
        )

    def _best_video_only_format(
        self,
        formats: list[dict[str, Any]],
        max_height: int,
        *,
        preferred_exts: tuple[str, ...],
    ) -> dict[str, Any] | None:
        candidates = [
            fmt
            for fmt in formats
            if fmt.get("format_id")
            and fmt.get("vcodec") not in {None, "none"}
            and fmt.get("acodec") == "none"
            and (fmt.get("height") or 0) <= max_height
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda fmt: (
                self._ext_preference(fmt.get("ext"), preferred_exts),
                -(fmt.get("height") or 0),
                -(fmt.get("fps") or 0),
                -(fmt.get("tbr") or 0),
                -int(fmt.get("filesize") or fmt.get("filesize_approx") or 0),
            ),
        )

    def _best_audio_format(
        self,
        formats: list[dict[str, Any]],
        *,
        preferred_exts: tuple[str, ...],
    ) -> dict[str, Any] | None:
        candidates = [
            fmt
            for fmt in formats
            if fmt.get("format_id") and fmt.get("acodec") not in {None, "none"}
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda fmt: (
                self._ext_preference(fmt.get("ext"), preferred_exts),
                -(fmt.get("abr") or 0),
                -(fmt.get("tbr") or 0),
                -int(fmt.get("filesize") or fmt.get("filesize_approx") or 0),
            ),
        )

    def _ext_preference(self, ext: str | None, preferred_exts: tuple[str, ...]) -> int:
        if not ext:
            return len(preferred_exts)
        try:
            return preferred_exts.index(ext)
        except ValueError:
            return len(preferred_exts)

    def _unique_preserve_order(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result

    def _base_ytdlp_options(self, url: str, *, skip_download: bool) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 30,
            "retries": max(2, self.settings.ytdlp_retries // 2) if skip_download else self.settings.ytdlp_retries,
            "extractor_retries": self.settings.ytdlp_extractor_retries,
        }

        if self.settings.ytdlp_retry_sleep_seconds > 0:
            options["retry_sleep_functions"] = {
                "http": self.settings.ytdlp_retry_sleep_seconds,
                "extractor": self.settings.ytdlp_retry_sleep_seconds,
                "fragment": self.settings.ytdlp_retry_sleep_seconds,
            }

        if self.settings.ytdlp_sleep_interval_requests > 0:
            options["sleep_interval_requests"] = self.settings.ytdlp_sleep_interval_requests

        if self.settings.ytdlp_proxy:
            options["proxy"] = self.settings.ytdlp_proxy

        options["http_headers"] = {
            "User-Agent": self.settings.ytdlp_user_agent,
        }

        cookie_file = self._resolve_cookie_file_for_ytdlp()
        if cookie_file:
            options["cookiefile"] = cookie_file

        extractor_args = self._build_extractor_args(url)
        if extractor_args:
            options["extractor_args"] = extractor_args

        return options

    def _build_extractor_args(self, url: str) -> dict[str, dict[str, list[str]]]:
        extractor_args: dict[str, dict[str, list[str]]] = {}

        if self.settings.ytdlp_generic_impersonate:
            extractor_args["generic"] = {
                "impersonate": [self.settings.ytdlp_generic_impersonate],
            }

        if self._is_tiktok_url(url):
            tiktok_args: dict[str, list[str]] = {}
            if self.settings.tiktok_api_hostname:
                tiktok_args["api_hostname"] = [self.settings.tiktok_api_hostname]
            if self.settings.tiktok_app_info:
                tiktok_args["app_info"] = [self.settings.tiktok_app_info]
            if self.settings.tiktok_device_id:
                tiktok_args["device_id"] = [self.settings.tiktok_device_id]
            if tiktok_args:
                extractor_args["tiktok"] = tiktok_args

        return extractor_args

    def _max_sendable_bytes(self) -> int:
        if self.settings.telegram_api_id and self.settings.telegram_api_hash:
            return self.settings.mtproto_limit_bytes
        return self.settings.bot_api_limit_bytes

    def _delivery_limit_message(self) -> str:
        limit = human_bytes(self._max_sendable_bytes())
        if self.settings.telegram_api_id and self.settings.telegram_api_hash:
            return f"This file is too large to send in Telegram. Please choose a smaller option (up to {limit})."
        return (
            "This file is too large to send with the current setup. "
            f"Without MTProto configured, the direct-send limit is {limit}."
        )

    def _resolve_ffmpeg_location(self) -> str | None:
        candidate = self.settings.ffmpeg_binary
        if candidate:
            path = Path(candidate)
            if path.exists():
                return str(path)
            resolved = shutil.which(candidate)
            if resolved:
                return resolved

        return shutil.which("ffmpeg")

    def _resolve_cookie_file_for_ytdlp(self) -> str | None:
        cookie_path = self.settings.ytdlp_cookie_file
        if cookie_path is None:
            return None

        if not cookie_path.exists():
            logger.debug("Cookies file %s was not found. Continuing without authenticated cookies.", cookie_path)
            return None
        if not cookie_path.is_file():
            logger.warning("Configured cookies path %s is not a file. Continuing without authenticated cookies.", cookie_path)
            return None

        try:
            first_line = cookie_path.read_text(encoding="utf-8", errors="ignore").splitlines()[:1]
        except OSError as exc:
            logger.warning(
                "Configured cookies file %s could not be read. Continuing without authenticated cookies.",
                cookie_path,
                exc_info=exc,
            )
            return None

        if cookie_path.stat().st_size <= 0:
            logger.warning("Configured cookies file %s is empty. Continuing without authenticated cookies.", cookie_path)
            return None

        if first_line:
            header = first_line[0].strip().lower()
            if header and "netscape" not in header and "http cookie file" not in header:
                logger.warning(
                    "Configured cookies file %s is not in Netscape cookies.txt format. "
                    "Continuing without authenticated cookies.",
                    cookie_path,
                )
                return None

        runtime_cookie_dir = self.settings.runtime_dir / "state"
        runtime_cookie_dir.mkdir(parents=True, exist_ok=True)
        runtime_cookie_path = runtime_cookie_dir / "yt-dlp-cookies.txt"

        try:
            if cookie_path.resolve() != runtime_cookie_path.resolve():
                shutil.copyfile(cookie_path, runtime_cookie_path)
        except OSError as exc:
            logger.warning(
                "Cookies file %s could not be copied into writable runtime storage. "
                "Continuing without authenticated cookies.",
                cookie_path,
                exc_info=exc,
            )
            return None

        return str(runtime_cookie_path)

    def _build_progress_hook(
        self,
        loop: asyncio.AbstractEventLoop,
        progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None,
        cancel_requested: Callable[[], bool] | None,
    ) -> Callable[[dict[str, Any]], None]:
        last_emit_at = 0.0
        last_percent = -1

        def hook(data: dict[str, Any]) -> None:
            nonlocal last_emit_at, last_percent
            if cancel_requested and cancel_requested():
                raise DownloadCancelledError("Canceled. The download was stopped.")
            if progress_callback is None:
                return

            status = data.get("status")
            now = time.monotonic()

            if status == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                downloaded = data.get("downloaded_bytes") or 0
                percent = int((downloaded / total) * 100) if total else last_percent
                should_emit = (
                    last_percent < 0
                    or percent >= last_percent + 3
                    or now - last_emit_at >= 2.0
                )
                if not should_emit:
                    return

                last_emit_at = now
                last_percent = percent
                payload = {
                    "status": "downloading",
                    "percent": max(percent, 0),
                    "downloaded_bytes": downloaded,
                    "total_bytes": total,
                    "speed": data.get("speed"),
                    "eta": data.get("eta"),
                }
                asyncio.run_coroutine_threadsafe(progress_callback(payload), loop)
                return

            if status == "finished":
                payload = {"status": "processing"}
                asyncio.run_coroutine_threadsafe(progress_callback(payload), loop)

        return hook

    def _format_duration_limit(self, total_seconds: int) -> str:
        hours, remainder = divmod(int(total_seconds), 3600)
        minutes, _ = divmod(remainder, 60)
        if hours and minutes:
            return f"{hours}h {minutes}m"
        if hours:
            return f"{hours} hour(s)"
        if minutes:
            return f"{minutes} minute(s)"
        return f"{total_seconds} second(s)"

    def _classify_source_error(self, exc: Exception, url: str = "") -> str:
        message = str(exc).lower()

        if self._is_temporary_source_failure_message(url, message):
            if self._is_tiktok_url(url):
                return (
                    "TikTok closed the connection before the bot could fetch the media. "
                    "This is usually temporary blocking or a network issue. Please try again in a moment."
                )
            return "The source platform temporarily refused the connection. Please try again in a moment."

        if self._is_youtube_signin_challenge(url, message):
            return (
                "YouTube is asking the bot to sign in before it will serve this video. "
                "Add an authorized YouTube cookies file to the bot configuration and try again."
            )

        if "requested format is not available" in message:
            return (
                "That exact quality is not available from the source right now. "
                "Please try another quality option."
            )

        if any(
            token in message
            for token in (
                "private video",
                "is private",
                "private content",
                "private post",
                "private account",
                "private",
                "followers only",
                "friends only",
            )
        ):
            return "This content is private or restricted to approved viewers."

        if any(
            token in message
            for token in (
                "not available in your country",
                "not available from your location",
                "geo-blocked",
                "geoblocked",
                "blocked in your country",
                "blocked in your region",
                "region locked",
            )
        ):
            return "This content appears to be geo-blocked and is not available from the bot's current region."

        if self._looks_like_login_requirement(message):
            return "This content requires you to be logged in, and the bot does not have authorized access for it."

        if any(
            token in message
            for token in (
                "video unavailable",
                "content unavailable",
                "not available",
                "has been removed",
                "was removed",
                "deleted",
                "does not exist",
                "not found",
                "404",
            )
        ):
            return "This content looks deleted, unavailable, or no longer accessible at this link."

        return "The content could not be accessed from the source platform."

    def _looks_like_login_requirement(self, message: str) -> bool:
        direct_tokens = (
            "login required",
            "authentication required",
            "requires login",
            "requires authentication",
            "members only",
            "membership required",
            "age-restricted",
            "confirm your age",
        )
        if any(token in message for token in direct_tokens):
            return True

        sign_in_tokens = (
            "sign in to continue",
            "please sign in",
            "must sign in",
            "log in to view",
            "login to view",
            "login to access",
        )
        return any(token in message for token in sign_in_tokens)

    def _is_temporary_source_failure_message(self, url: str, message: str) -> bool:
        lowered = message.lower()
        if not self._is_tiktok_url(url):
            return False

        return any(
            token in lowered
            for token in (
                "connection aborted",
                "connection reset",
                "forcibly closed by the remote host",
                "timed out",
                "transporterror",
                "remote end closed connection",
                "unable to download webpage",
            )
        )

    def _is_tiktok_url(self, url: str) -> bool:
        lowered_url = url.lower()
        return "tiktok.com" in lowered_url or "vm.tiktok" in lowered_url

    def _is_youtube_url(self, url: str) -> bool:
        lowered_url = url.lower()
        return "youtube.com" in lowered_url or "youtu.be" in lowered_url

    def _relax_request_for_format_fallback(self, request: DownloadRequest) -> DownloadRequest | None:
        if request.kind is MediaKind.VIDEO:
            height_match = re.search(r"height<=([0-9]+)", request.format_selector or "")
            if height_match:
                height = height_match.group(1)
                selector = f"best[height<={height}]/bestvideo*+bestaudio/best"
            else:
                selector = "bestvideo*+bestaudio/best"
            if selector != (request.format_selector or ""):
                return dataclasses.replace(request, format_selector=selector)

        if request.kind is MediaKind.AUDIO and (request.output_ext or "").lower() == "m4a":
            selector = "bestaudio/best"
            if selector != (request.format_selector or ""):
                return dataclasses.replace(request, format_selector=selector)

        return None

    def _request_variants_for_fallback(self, request: DownloadRequest) -> list[DownloadRequest]:
        variants = [request]

        relaxed_request = self._relax_request_for_format_fallback(request)
        if relaxed_request is not None and relaxed_request.format_selector != variants[-1].format_selector:
            variants.append(relaxed_request)

        if request.kind is MediaKind.VIDEO:
            final_selector = "best"
        elif (request.output_ext or "").lower() == "m4a":
            final_selector = "bestaudio/best"
        else:
            final_selector = None

        if final_selector and all(final_selector != (variant.format_selector or "") for variant in variants):
            variants.append(dataclasses.replace(request, format_selector=final_selector))

        return variants

    def _is_youtube_signin_challenge(self, url: str, message: str) -> bool:
        lowered_url = url.lower()
        if "youtube.com" not in lowered_url and "youtu.be" not in lowered_url:
            return False

        lowered = message.lower()
        return any(
            token in lowered
            for token in (
                "sign in to confirm you're not a bot",
                "sign in to confirm you’re not a bot",
                "sign in to confirm you???re not a bot",
                "not a bot",
                "cookies-from-browser",
                "use --cookies",
            )
        )

    def _looks_like_ytdlp_failure(self, exc: Exception) -> bool:
        module = exc.__class__.__module__.lower()
        name = exc.__class__.__name__.lower()
        return module.startswith("yt_dlp") or "downloaderror" in name or "extractorerror" in name
