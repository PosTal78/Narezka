"""Locations owned by Narezchik, never by the Windows user profile."""

from __future__ import annotations

import os
from pathlib import Path
import sys


class AppPaths:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.data = root / "data"
        self.projects = self.data / "projects"
        self.models = self.data / "models"
        self.whisper_models = self.models / "whisper"
        self.vision_models = self.models / "vision"
        self.translation_models = self.models / "translation"
        self.cache = self.data / "cache"
        self.logs = self.data / "logs"
        self.ffmpeg = root / "tools" / "ffmpeg"

    def ensure_data_directories(self) -> None:
        for directory in (self.projects, self.models, self.whisper_models, self.vision_models, self.translation_models, self.cache, self.logs):
            directory.mkdir(parents=True, exist_ok=True)


def app_paths() -> AppPaths:
    """Resolve the portable folder, allowing an explicit developer override."""
    override = os.environ.get("NAREZCHIK_HOME")
    if override:
        root = Path(override).expanduser().resolve()
    elif getattr(sys, "frozen", False):
        root = Path(sys.executable).resolve().parent
    else:
        # Keep development data inside the checkout on D:, not in AppData.
        root = Path(__file__).resolve().parents[2]
    return AppPaths(root)
