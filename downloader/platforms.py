from __future__ import annotations

from urllib.parse import urlparse

from downloader.exceptions import UnsupportedUrlError


SUPPORTED_HOSTS = {
    "youtube": ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"),
    "tiktok": ("tiktok.com", "www.tiktok.com", "vm.tiktok.com"),
    "instagram": ("instagram.com", "www.instagram.com"),
    "facebook": ("facebook.com", "www.facebook.com", "fb.watch", "m.facebook.com"),
}


def normalize_url(url: str) -> str:
    value = url.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise UnsupportedUrlError("Please send a valid HTTP or HTTPS URL.")
    return value


def detect_platform(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    host = host.split(":")[0]
    host = host.removeprefix("www.")
    host = host.removeprefix("m.")

    for platform, hosts in SUPPORTED_HOSTS.items():
        for candidate in hosts:
            normalized = candidate.removeprefix("www.").removeprefix("m.")
            if host == normalized or host.endswith(f".{normalized}"):
                return platform
    return None


def require_supported_platform(url: str) -> str:
    platform = detect_platform(url)
    if platform is None:
        raise UnsupportedUrlError(
            "Unsupported URL. Supported platforms are YouTube, TikTok, Instagram, and Facebook."
        )
    return platform

