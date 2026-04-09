from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from downloader.models import FormatOption, MediaPreview


@dataclass(slots=True)
class PreviewSession:
    token: str
    user_id: int
    chat_id: int
    preview: MediaPreview
    created_at: float


class PreviewStore:
    def __init__(self, ttl_seconds: int = 1800) -> None:
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, PreviewSession] = {}

    def create(self, user_id: int, chat_id: int, preview: MediaPreview) -> PreviewSession:
        self._purge_expired()
        token = uuid.uuid4().hex[:10]
        session = PreviewSession(
            token=token,
            user_id=user_id,
            chat_id=chat_id,
            preview=preview,
            created_at=time.monotonic(),
        )
        self._sessions[token] = session
        return session

    def get(self, token: str) -> PreviewSession | None:
        self._purge_expired()
        return self._sessions.get(token)

    def get_option(self, token: str, option_id: str) -> tuple[PreviewSession | None, FormatOption | None]:
        session = self.get(token)
        if session is None:
            return None, None

        for option in session.preview.video_options + session.preview.audio_options:
            if option.option_id == option_id:
                return session, option
        return session, None

    def _purge_expired(self) -> None:
        now = time.monotonic()
        expired = [
            token
            for token, session in self._sessions.items()
            if session.created_at + self.ttl_seconds <= now
        ]
        for token in expired:
            self._sessions.pop(token, None)

