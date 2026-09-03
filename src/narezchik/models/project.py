"""Portable project state and pure segment rules for the first MVP stage."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePath
from typing import Any, Iterable


PROJECT_FORMAT_VERSION = 1


class ProjectFormatError(ValueError):
    """The project file does not conform to a supported Narezchik format."""


class SegmentStatus(StrEnum):
    READY = "ready"
    NEEDS_TTS = "needs_tts"
    STALE = "stale"
    EXCLUDED = "excluded"
    FILE_MISSING = "file_missing"
    ERROR = "error"


def normalize_comparison_text(text: str) -> str:
    """Ignore only non-visible whitespace changes when matching old audio."""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def audio_path_for_order(order: int) -> str:
    return f"audio/{order:03d}.mp3"


def _validate_relative_path(path: str | None, field_name: str) -> None:
    if path is None:
        return
    candidate = PurePath(path.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise ProjectFormatError(f"{field_name} must be a project-relative path")


@dataclass(frozen=True, slots=True)
class TTSSettings:
    language: str = "ru-RU"
    voice: str = "ru-RU-DmitryNeural"
    rate: str = "+0%"
    volume: str = "+0%"
    pitch: str = "+0Hz"

    def __post_init__(self) -> None:
        for field_name, value in (
            ("language", self.language), ("voice", self.voice), ("rate", self.rate),
            ("volume", self.volume), ("pitch", self.pitch),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ProjectFormatError(f"TTS setting {field_name} must be a non-empty string")

    def to_dict(self) -> dict[str, str]:
        return {"language": self.language, "voice": self.voice, "rate": self.rate,
                "volume": self.volume, "pitch": self.pitch}

    @classmethod
    def from_dict(cls, data: Any) -> "TTSSettings":
        if not isinstance(data, dict):
            raise ProjectFormatError("tts_settings must be an object")
        try:
            return cls(**{key: data[key] for key in ("language", "voice", "rate", "volume", "pitch")})
        except KeyError as error:
            raise ProjectFormatError(f"tts_settings is missing {error.args[0]}") from error


@dataclass(slots=True)
class Segment:
    segment_id: int
    order: int
    text: str
    excluded_from_tts: bool = False
    status: SegmentStatus = SegmentStatus.NEEDS_TTS
    audio: str | None = None
    duration: float | None = None
    tts_settings: TTSSettings | None = None
    error: str | None = None
    status_before_exclusion: SegmentStatus | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.segment_id, int) or not isinstance(self.order, int):
            raise ProjectFormatError("segment_id and order must be integers")
        if self.segment_id < 1 or self.order < 1:
            raise ProjectFormatError("segment_id and order must be positive")
        if not isinstance(self.text, str):
            raise ProjectFormatError("segment text must be a string")
        if not self.text.strip():
            raise ProjectFormatError("segment text must not be empty")
        _validate_relative_path(self.audio, "segment audio")
        if self.duration is not None:
            if not isinstance(self.duration, (int, float)) or isinstance(self.duration, bool):
                raise ProjectFormatError("segment duration must be a number")
            if self.duration < 0:
                raise ProjectFormatError("segment duration cannot be negative")
        if self.excluded_from_tts != (self.status is SegmentStatus.EXCLUDED):
            raise ProjectFormatError("excluded_from_tts and excluded status must agree")

    @property
    def can_play(self) -> bool:
        return self.status is SegmentStatus.READY and self.audio is not None

    @property
    def has_current_audio(self) -> bool:
        return self.can_play and self.duration is not None

    def mark_stale(self) -> None:
        if self.excluded_from_tts:
            self.status_before_exclusion = SegmentStatus.STALE
        elif self.audio is None:
            self.status = SegmentStatus.NEEDS_TTS
        else:
            self.status = SegmentStatus.STALE
        self.error = None

    def edit_text(self, text: str) -> None:
        if not isinstance(text, str) or not text.strip():
            raise ProjectFormatError("segment text must not be empty")
        if normalize_comparison_text(self.text) != normalize_comparison_text(text):
            self.mark_stale()
        self.text = text

    def exclude(self) -> None:
        if not self.excluded_from_tts:
            self.status_before_exclusion = self.status
            self.excluded_from_tts = True
            self.status = SegmentStatus.EXCLUDED
            self.error = None

    def include(self) -> None:
        if self.excluded_from_tts:
            self.excluded_from_tts = False
            self.status = self.status_before_exclusion or SegmentStatus.NEEDS_TTS
            self.status_before_exclusion = None

    def mark_ready(self, duration: float, settings: TTSSettings) -> None:
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration < 0:
            raise ProjectFormatError("segment duration must be a non-negative number")
        self.excluded_from_tts = False
        self.status = SegmentStatus.READY
        self.audio = audio_path_for_order(self.order)
        self.duration = duration
        self.tts_settings = settings
        self.error = None
        self.status_before_exclusion = None

    def mark_file_missing(self) -> None:
        if self.excluded_from_tts:
            self.status_before_exclusion = SegmentStatus.FILE_MISSING
        elif self.audio is not None:
            self.status = SegmentStatus.FILE_MISSING
        self.error = "Аудиофайл не найден в папке проекта."

    def to_dict(self) -> dict[str, Any]:
        return {"segment_id": self.segment_id, "order": self.order, "text": self.text,
                "excluded_from_tts": self.excluded_from_tts, "status": self.status.value,
                "audio": self.audio, "duration": self.duration,
                "tts_settings": self.tts_settings.to_dict() if self.tts_settings else None,
                "error": self.error,
                "status_before_exclusion": self.status_before_exclusion.value if self.status_before_exclusion else None}

    @classmethod
    def from_dict(cls, data: Any) -> "Segment":
        if not isinstance(data, dict):
            raise ProjectFormatError("segment must be an object")
        try:
            prior = data.get("status_before_exclusion")
            return cls(segment_id=data["segment_id"], order=data["order"], text=data["text"],
                excluded_from_tts=data["excluded_from_tts"], status=SegmentStatus(data["status"]),
                audio=data.get("audio"), duration=data.get("duration"),
                tts_settings=TTSSettings.from_dict(data["tts_settings"]) if data.get("tts_settings") else None,
                error=data.get("error"), status_before_exclusion=SegmentStatus(prior) if prior else None)
        except KeyError as error:
            raise ProjectFormatError(f"segment is missing {error.args[0]}") from error
        except ValueError as error:
            raise ProjectFormatError(f"segment has an unknown status: {error}") from error


@dataclass(slots=True)
class Project:
    name: str
    tts_settings: TTSSettings = field(default_factory=TTSSettings)
    segments: list[Segment] = field(default_factory=list)
    next_segment_id: int = 1
    format_version: int = PROJECT_FORMAT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ProjectFormatError("project name must be a non-empty string")
        if self.format_version != PROJECT_FORMAT_VERSION:
            raise ProjectFormatError(f"unsupported project format version: {self.format_version}")
        self._validate_segments()

    @property
    def script_text(self) -> str:
        return "\n".join(segment.text for segment in self.segments)

    @property
    def can_generate_tts(self) -> bool:
        return any(not segment.excluded_from_tts for segment in self.segments)

    def add_segment(self, text: str, *, index: int | None = None) -> Segment:
        if not isinstance(text, str):
            raise ProjectFormatError("segment text must be a string")
        segment = Segment(self.next_segment_id, 1, text)
        self.next_segment_id += 1
        self.segments.insert(len(self.segments) if index is None else index, segment)
        self._reindex()
        return segment

    def remove_segment(self, segment_id: int) -> Segment:
        removed = self.segments.pop(self._index_for_id(segment_id))
        self._reindex()
        return removed

    def edit_segment(self, segment_id: int, text: str) -> None:
        self.segments[self._index_for_id(segment_id)].edit_text(text)

    def set_segment_excluded(self, segment_id: int, excluded: bool) -> None:
        (self.segments[self._index_for_id(segment_id)].exclude() if excluded
         else self.segments[self._index_for_id(segment_id)].include())

    def split_segment(self, segment_id: int, parts: Iterable[str]) -> list[Segment]:
        values = list(parts)
        if len(values) < 2 or not all(isinstance(value, str) and value.strip() for value in values):
            raise ProjectFormatError("splitting requires at least two non-empty text parts")
        index = self._index_for_id(segment_id)
        original = self.segments[index]
        original.edit_text(values[0])
        created = [original]
        for offset, text in enumerate(values[1:], 1):
            segment = Segment(self.next_segment_id, 1, text)
            self.next_segment_id += 1
            self.segments.insert(index + offset, segment)
            created.append(segment)
        self._reindex()
        return created

    def merge_with_next(self, segment_id: int) -> Segment:
        index = self._index_for_id(segment_id)
        if index == len(self.segments) - 1:
            raise ProjectFormatError("the last segment cannot be merged with a next segment")
        primary = self.segments[index]
        following = self.segments.pop(index + 1)
        primary.edit_text(f"{primary.text}\n{following.text}")
        self._reindex()
        return primary

    def set_tts_settings(self, settings: TTSSettings) -> None:
        if settings == self.tts_settings:
            return
        self.tts_settings = settings
        for segment in self.segments:
            if segment.tts_settings != settings:
                segment.mark_stale()

    def replace_segments(self, texts: Iterable[str]) -> None:
        values = list(texts)
        if not all(isinstance(text, str) for text in values):
            raise ProjectFormatError("segment text must be strings")
        reusable: dict[str, deque[Segment]] = defaultdict(deque)
        for segment in self.segments:
            if segment.tts_settings == self.tts_settings:
                reusable[normalize_comparison_text(segment.text)].append(segment)
        replacement: list[Segment] = []
        for text in values:
            matches = reusable[normalize_comparison_text(text)]
            if matches:
                segment = matches.popleft()
                segment.text = text
            else:
                segment = Segment(self.next_segment_id, 1, text)
                self.next_segment_id += 1
            replacement.append(segment)
        self.segments = replacement
        self._reindex()

    def reconcile_audio_files(self, project_root: Path) -> None:
        for segment in self.segments:
            if segment.audio and not (project_root / segment.audio).is_file():
                segment.mark_file_missing()

    def to_dict(self) -> dict[str, Any]:
        self._validate_segments()
        return {"format_version": self.format_version, "name": self.name,
                "tts_settings": self.tts_settings.to_dict(), "script_text": self.script_text,
                "next_segment_id": self.next_segment_id,
                "segments": [segment.to_dict() for segment in self.segments]}

    @classmethod
    def from_dict(cls, data: Any) -> "Project":
        if not isinstance(data, dict):
            raise ProjectFormatError("project.json must contain an object")
        try:
            return cls(name=data["name"], format_version=data["format_version"],
                tts_settings=TTSSettings.from_dict(data["tts_settings"]),
                next_segment_id=data["next_segment_id"],
                segments=[Segment.from_dict(value) for value in data["segments"]])
        except KeyError as error:
            raise ProjectFormatError(f"project is missing {error.args[0]}") from error

    def _index_for_id(self, segment_id: int) -> int:
        for index, segment in enumerate(self.segments):
            if segment.segment_id == segment_id:
                return index
        raise ProjectFormatError(f"unknown segment ID: {segment_id}")

    def _reindex(self) -> None:
        for order, segment in enumerate(self.segments, 1):
            segment.order = order
            if segment.audio is not None:
                segment.audio = audio_path_for_order(order)
        self._validate_segments()

    def _validate_segments(self) -> None:
        ids = [segment.segment_id for segment in self.segments]
        if len(ids) != len(set(ids)):
            raise ProjectFormatError("segment IDs must be unique")
        if any(segment.order != order for order, segment in enumerate(self.segments, 1)):
            raise ProjectFormatError("segment orders must be consecutive")
        if not isinstance(self.next_segment_id, int) or self.next_segment_id <= max(ids, default=0):
            raise ProjectFormatError("next_segment_id must exceed every existing segment ID")
