from __future__ import annotations


class UserFacingError(Exception):
    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class UnsupportedUrlError(UserFacingError):
    pass


class RateLimitError(UserFacingError):
    pass


class DownloadError(UserFacingError):
    pass


class MediaUnavailableError(DownloadError):
    pass


class DownloadCancelledError(DownloadError):
    pass


class MediaTooLongError(DownloadError):
    pass


class MediaTooLargeError(DownloadError):
    pass


class DeliveryError(UserFacingError):
    pass
