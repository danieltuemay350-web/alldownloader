from __future__ import annotations

import re
from pathlib import Path


INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1F]+')
MULTISPACE = re.compile(r"\s+")


def sanitize_filename(value: str, fallback: str = "download") -> str:
    text = INVALID_FILENAME_CHARS.sub("_", value).strip().strip(".")
    text = MULTISPACE.sub(" ", text)
    return text[:180] or fallback


def human_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    units = ["KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        value /= 1024
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
    return f"{size} B"


def newest_media_file(directory: Path) -> Path | None:
    candidates = [
        path
        for path in directory.rglob("*")
        if path.is_file()
        and path.suffix.lower() not in {".part", ".ytdl", ".temp", ".json", ".jpg", ".png", ".webp"}
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_mtime)

