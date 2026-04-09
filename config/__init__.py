from .settings import Settings

settings = Settings.from_env()

__all__ = ["Settings", "settings"]

