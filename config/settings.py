from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_path(name: str, default: str, base_dir: Path) -> Path:
    raw = os.getenv(name, default).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


@dataclass(slots=True)
class Settings:
    base_dir: Path
    bot_token: str
    admin_chat_id: int | None
    telegram_api_id: int | None
    telegram_api_hash: str | None
    download_concurrency: int
    download_timeout_seconds: int
    rate_limit_count: int
    rate_limit_window_seconds: int
    max_duration_seconds: int
    max_file_size_bytes: int
    bot_api_limit_bytes: int
    mtproto_limit_bytes: int
    split_chunk_size_bytes: int
    file_ttl_seconds: int
    large_file_strategy: str
    public_host: str
    public_port: int
    public_base_url: str | None
    temp_dir: Path
    public_dir: Path
    ffmpeg_binary: str | None
    ytdlp_proxy: str | None
    ytdlp_http_chunk_size_bytes: int
    ytdlp_fragment_concurrency: int
    ytdlp_retries: int
    mtproto_part_size_kb: int
    log_level: str

    @property
    def public_root_url(self) -> str:
        if self.public_base_url:
            return self.public_base_url.rstrip("/")

        host = self.public_host
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        return f"http://{host}:{self.public_port}"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        base_dir = Path(__file__).resolve().parent.parent

        settings = cls(
            base_dir=base_dir,
            bot_token=_env_str("BOT_TOKEN"),
            admin_chat_id=_env_int("ADMIN_CHAT_ID", 0) or None,
            telegram_api_id=_env_int("TELEGRAM_API_ID", 0) or None,
            telegram_api_hash=_env_str("TELEGRAM_API_HASH") or None,
            download_concurrency=_env_int("DOWNLOAD_CONCURRENCY", 3),
            download_timeout_seconds=_env_int("DOWNLOAD_TIMEOUT_SECONDS", 1800),
            rate_limit_count=_env_int("RATE_LIMIT_COUNT", 5),
            rate_limit_window_seconds=_env_int("RATE_LIMIT_WINDOW_SECONDS", 3600),
            max_duration_seconds=_env_int("MAX_DURATION_SECONDS", 7200),
            max_file_size_bytes=_env_int("MAX_FILE_SIZE_BYTES", 10 * 1024**3),
            bot_api_limit_bytes=_env_int("BOT_API_LIMIT_BYTES", 50 * 1024**2),
            mtproto_limit_bytes=_env_int("MTPROTO_LIMIT_BYTES", 2 * 1024**3),
            split_chunk_size_bytes=_env_int("SPLIT_CHUNK_SIZE_BYTES", int(1.5 * 1024**3)),
            file_ttl_seconds=_env_int("FILE_TTL_SECONDS", 600),
            large_file_strategy=_env_str("LARGE_FILE_STRATEGY", "link").lower(),
            public_host=_env_str("PUBLIC_HOST", "0.0.0.0"),
            public_port=_env_int("PUBLIC_PORT", 8080),
            public_base_url=_env_str("PUBLIC_BASE_URL") or None,
            temp_dir=_env_path("TEMP_DIR", "storage/temp", base_dir),
            public_dir=_env_path("PUBLIC_DIR", "storage/public", base_dir),
            ffmpeg_binary=_env_str("FFMPEG_BINARY") or None,
            ytdlp_proxy=_env_str("YTDLP_PROXY") or None,
            ytdlp_http_chunk_size_bytes=_env_int("YTDLP_HTTP_CHUNK_SIZE_BYTES", 10 * 1024 * 1024),
            ytdlp_fragment_concurrency=_env_int("YTDLP_FRAGMENT_CONCURRENCY", 6),
            ytdlp_retries=_env_int("YTDLP_RETRIES", 5),
            mtproto_part_size_kb=_env_int("MTPROTO_PART_SIZE_KB", 512),
            log_level=_env_str("LOG_LEVEL", "INFO").upper(),
        )

        if settings.large_file_strategy not in {"link", "split"}:
            settings.large_file_strategy = "link"

        if settings.ffmpeg_binary == "ffmpeg":
            settings.ffmpeg_binary = shutil.which("ffmpeg")

        settings.temp_dir.mkdir(parents=True, exist_ok=True)
        settings.public_dir.mkdir(parents=True, exist_ok=True)
        (settings.base_dir / "logs").mkdir(parents=True, exist_ok=True)
        return settings
