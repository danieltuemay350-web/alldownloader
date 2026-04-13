from __future__ import annotations

import os
import shutil
import tempfile
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


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def _env_path(name: str, default: str, base_dir: Path) -> Path:
    raw = os.getenv(name, default).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _env_runtime_path(name: str, default: Path, anchor_dir: Path) -> Path:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default.resolve()

    path = Path(raw.strip())
    if not path.is_absolute():
        path = anchor_dir / path
    return path.resolve()


def _env_optional_path(name: str, anchor_dir: Path) -> Path | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None

    path = Path(raw.strip())
    if not path.is_absolute():
        path = anchor_dir / path
    return path.resolve()


def _is_dir_writable(path: Path) -> bool:
    probe = path / ".write_test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _default_runtime_dir(base_dir: Path) -> Path:
    configured = os.getenv("RUNTIME_DIR")
    if configured and configured.strip():
        path = Path(configured.strip())
        if not path.is_absolute():
            path = base_dir / path
        return path.resolve()

    if _is_dir_writable(base_dir):
        return base_dir

    return (Path(tempfile.gettempdir()) / "telegram-media-bot").resolve()


@dataclass(slots=True)
class Settings:
    base_dir: Path
    runtime_dir: Path
    log_dir: Path
    bot_token: str
    admin_chat_id: int | None
    telegram_api_id: int | None
    telegram_api_hash: str | None
    service_host: str
    service_port: int
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
    ytdlp_cookie_file: Path | None
    ytdlp_user_agent: str
    ffmpeg_binary: str | None
    ytdlp_proxy: str | None
    ytdlp_http_chunk_size_bytes: int
    ytdlp_fragment_concurrency: int
    ytdlp_retries: int
    ytdlp_extractor_retries: int
    ytdlp_sleep_interval_requests: float
    ytdlp_retry_sleep_seconds: float
    ytdlp_generic_impersonate: str | None
    tiktok_api_hostname: str | None
    tiktok_app_info: str | None
    tiktok_device_id: str | None
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
        runtime_dir = _default_runtime_dir(base_dir)
        path_anchor = base_dir if runtime_dir == base_dir else runtime_dir

        settings = cls(
            base_dir=base_dir,
            runtime_dir=runtime_dir,
            log_dir=_env_runtime_path("LOG_DIR", runtime_dir / "logs", path_anchor),
            bot_token=_env_str("BOT_TOKEN"),
            admin_chat_id=_env_int("ADMIN_CHAT_ID", 0) or None,
            telegram_api_id=_env_int("TELEGRAM_API_ID", 0) or None,
            telegram_api_hash=_env_str("TELEGRAM_API_HASH") or None,
            service_host=_env_str("SERVICE_HOST", "0.0.0.0"),
            service_port=_env_int("PORT", _env_int("SERVICE_PORT", 8080)),
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
            temp_dir=_env_runtime_path("TEMP_DIR", runtime_dir / "storage" / "temp", path_anchor),
            public_dir=_env_runtime_path("PUBLIC_DIR", runtime_dir / "storage" / "public", path_anchor),
            ytdlp_cookie_file=_env_optional_path("YTDLP_COOKIE_FILE", path_anchor) or (path_anchor / "cookies.txt").resolve(),
            ytdlp_user_agent=_env_str(
                "YTDLP_USER_AGENT",
                (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/135.0.0.0 Safari/537.36"
                ),
            ),
            ffmpeg_binary=_env_str("FFMPEG_BINARY") or None,
            ytdlp_proxy=_env_str("YTDLP_PROXY") or None,
            ytdlp_http_chunk_size_bytes=_env_int("YTDLP_HTTP_CHUNK_SIZE_BYTES", 10 * 1024 * 1024),
            ytdlp_fragment_concurrency=_env_int("YTDLP_FRAGMENT_CONCURRENCY", 6),
            ytdlp_retries=_env_int("YTDLP_RETRIES", 5),
            ytdlp_extractor_retries=_env_int("YTDLP_EXTRACTOR_RETRIES", 5),
            ytdlp_sleep_interval_requests=_env_float("YTDLP_SLEEP_INTERVAL_REQUESTS", 0.75),
            ytdlp_retry_sleep_seconds=_env_float("YTDLP_RETRY_SLEEP_SECONDS", 2.0),
            ytdlp_generic_impersonate=_env_str("YTDLP_GENERIC_IMPERSONATE") or None,
            tiktok_api_hostname=_env_str("TIKTOK_API_HOSTNAME") or None,
            tiktok_app_info=_env_str("TIKTOK_APP_INFO") or None,
            tiktok_device_id=_env_str("TIKTOK_DEVICE_ID") or None,
            mtproto_part_size_kb=_env_int("MTPROTO_PART_SIZE_KB", 512),
            log_level=_env_str("LOG_LEVEL", "INFO").upper(),
        )

        if settings.large_file_strategy not in {"link", "split"}:
            settings.large_file_strategy = "link"

        if settings.ffmpeg_binary == "ffmpeg":
            settings.ffmpeg_binary = shutil.which("ffmpeg")

        settings.runtime_dir.mkdir(parents=True, exist_ok=True)
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        settings.temp_dir.mkdir(parents=True, exist_ok=True)
        settings.public_dir.mkdir(parents=True, exist_ok=True)
        return settings
