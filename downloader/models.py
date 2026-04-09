from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class MediaKind(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"


@dataclass(slots=True)
class DownloadRequest:
    user_id: int
    chat_id: int
    url: str
    kind: MediaKind
    format_selector: str | None = None
    output_ext: str | None = None
    audio_bitrate_kbps: int | None = None
    option_label: str | None = None


@dataclass(slots=True)
class MediaMetadata:
    source_url: str
    platform: str
    title: str
    extractor_id: str | None
    duration: int | None
    size_estimate: int | None
    uploader: str | None
    thumbnail_url: str | None = None


@dataclass(slots=True)
class FormatOption:
    option_id: str
    kind: MediaKind
    label: str
    selector: str
    output_ext: str
    audio_bitrate_kbps: int | None = None


@dataclass(slots=True)
class MediaPreview:
    metadata: MediaMetadata
    video_options: list[FormatOption]
    audio_options: list[FormatOption]


@dataclass(slots=True)
class DownloadArtifact:
    file_path: Path
    file_size: int
    kind: MediaKind
    metadata: MediaMetadata


@dataclass(slots=True)
class DeliveryLink:
    name: str
    url: str
    size: int
