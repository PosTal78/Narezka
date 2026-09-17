"""Portable project state and pure segment rules for the first MVP stage."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import math
from numbers import Real
from pathlib import Path, PurePath
from typing import Any, Iterable


PROJECT_FORMAT_VERSION = 5


class ProjectFormatError(ValueError):
    """The project file does not conform to a supported Narezchik format."""


class SegmentStatus(StrEnum):
    READY = "ready"
    NEEDS_TTS = "needs_tts"
    STALE = "stale"
    EXCLUDED = "excluded"
    FILE_MISSING = "file_missing"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class VideoSource:
    """An external source-file reference and the facts verified at import time."""

    path: str
    fingerprint: str
    file_name: str
    duration: float
    width: int
    height: int
    fps: float
    parts: tuple["VideoSource", ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path or not Path(self.path).is_absolute():
            raise ProjectFormatError("video source path must be an absolute external path")
        if Path(self.path).suffix.lower() not in {".mp4", ".mkv"}:
            raise ProjectFormatError("video source must reference an MP4 or MKV file")
        _validate_fingerprint(self.fingerprint, "video source fingerprint")
        if not isinstance(self.file_name, str) or not self.file_name:
            raise ProjectFormatError("video source metadata is invalid")
        for name, value in (("duration", self.duration), ("fps", self.fps)):
            if (not isinstance(value, Real) or isinstance(value, bool)
                    or not math.isfinite(float(value)) or value <= 0):
                raise ProjectFormatError(f"video source {name} must be a positive finite number")
        for name, value in (("width", self.width), ("height", self.height)):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ProjectFormatError(f"video source {name} must be a positive integer")
        if self.parts:
            if any(part.parts for part in self.parts):
                raise ProjectFormatError("video source parts cannot be nested")
            if abs(sum(part.duration for part in self.parts) - self.duration) > 0.05:
                raise ProjectFormatError("video source duration must equal the duration of all parts")
            if self.path != self.parts[0].path:
                raise ProjectFormatError("video source path must point to its first part")

    def to_dict(self) -> dict[str, Any]:
        def part_dict(part: "VideoSource") -> dict[str, Any]:
            return {"path": part.path, "fingerprint": part.fingerprint, "file_name": part.file_name,
                    "duration": part.duration, "width": part.width, "height": part.height, "fps": part.fps}
        return {**part_dict(self), "parts": [part_dict(part) for part in self.source_parts]}

    @classmethod
    def from_dict(cls, data: Any) -> "VideoSource":
        if not isinstance(data, dict):
            raise ProjectFormatError("video_source must be an object")
        try:
            values = {key: data[key] for key in ("path", "fingerprint", "file_name", "duration", "width", "height", "fps")}
            raw_parts = data.get("parts")
            if raw_parts:
                parts = tuple(cls.from_dict({**part, "parts": []}) for part in raw_parts)
                return cls(**values, parts=parts)
            return cls(**values)
        except KeyError as error:
            raise ProjectFormatError(f"video_source is missing {error.args[0]}") from error

    @property
    def source_parts(self) -> tuple["VideoSource", ...]:
        return self.parts or (self,)

    @property
    def offsets(self) -> tuple[float, ...]:
        result: list[float] = []
        position = 0.0
        for part in self.source_parts:
            result.append(position)
            position += part.duration
        return tuple(result)

    def locate(self, position: float) -> tuple[int, "VideoSource", float]:
        value = min(max(0.0, float(position)), self.duration)
        for index, (part, offset) in enumerate(zip(self.source_parts, self.offsets)):
            if value < offset + part.duration or index == len(self.source_parts) - 1:
                return index, part, min(part.duration, max(0.0, value - offset))
        raise ProjectFormatError("video position is outside the source")

    @classmethod
    def combine(cls, parts: Iterable["VideoSource"]) -> "VideoSource":
        values = tuple(parts)
        if not values:
            raise ProjectFormatError("at least one video source part is required")
        if len(values) == 1:
            part = values[0]
            return cls(part.path, part.fingerprint, part.file_name, part.duration,
                       part.width, part.height, part.fps, (part,))
        digest = hashlib.sha256()
        for part in values:
            digest.update(bytes.fromhex(part.fingerprint))
        first = values[0]
        return cls(first.path, digest.hexdigest(), f"{len(values)} частей", sum(part.duration for part in values),
                   first.width, first.height, first.fps, values)


@dataclass(frozen=True, slots=True)
class SubtitleState:
    subtitles_path: str
    transcript_path: str
    source_fingerprint: str
    kind: str
    language: str | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        _validate_relative_path(self.subtitles_path, "subtitles path")
        _validate_relative_path(self.transcript_path, "transcript path")
        _validate_fingerprint(self.source_fingerprint, "subtitle source fingerprint")
        if not isinstance(self.kind, str) or self.kind not in {"import", "whisper"}:
            raise ProjectFormatError("subtitle kind must be import or whisper")

    def to_dict(self) -> dict[str, Any]:
        return {"subtitles_path": self.subtitles_path, "transcript_path": self.transcript_path,
                "source_fingerprint": self.source_fingerprint, "kind": self.kind,
                "language": self.language, "model": self.model}

    @classmethod
    def from_dict(cls, data: Any) -> "SubtitleState":
        if not isinstance(data, dict):
            raise ProjectFormatError("active_subtitles must be an object")
        try:
            return cls(data["subtitles_path"], data["transcript_path"], data["source_fingerprint"], data["kind"],
                       data.get("language"), data.get("model"))
        except KeyError as error:
            raise ProjectFormatError(f"active_subtitles is missing {error.args[0]}") from error


@dataclass(frozen=True, slots=True)
class SceneState:
    scenes_path: str
    source_fingerprint: str
    detector: str = "content-default"

    def __post_init__(self) -> None:
        _validate_relative_path(self.scenes_path, "scenes path")
        _validate_fingerprint(self.source_fingerprint, "scene source fingerprint")

    def to_dict(self) -> dict[str, Any]:
        return {"scenes_path": self.scenes_path, "source_fingerprint": self.source_fingerprint,
                "detector": self.detector}

    @classmethod
    def from_dict(cls, data: Any) -> "SceneState":
        if not isinstance(data, dict):
            raise ProjectFormatError("scene_analysis must be an object")
        try:
            return cls(data["scenes_path"], data["source_fingerprint"], data.get("detector", "content-default"))
        except KeyError as error:
            raise ProjectFormatError(f"scene_analysis is missing {error.args[0]}") from error


@dataclass(frozen=True, slots=True)
class VideoIndexState:
    """A reusable local index belonging to one exact source movie."""
    index_path: str
    source_fingerprint: str
    model_version: str

    def __post_init__(self) -> None:
        _validate_relative_path(self.index_path, "video index path")
        _validate_fingerprint(self.source_fingerprint, "video index source fingerprint")
        if not isinstance(self.model_version, str) or not self.model_version.strip():
            raise ProjectFormatError("video index model version must be a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        return {"index_path": self.index_path, "source_fingerprint": self.source_fingerprint,
                "model_version": self.model_version}

    @classmethod
    def from_dict(cls, data: Any) -> "VideoIndexState":
        if not isinstance(data, dict):
            raise ProjectFormatError("video_index must be an object")
        try:
            return cls(data["index_path"], data["source_fingerprint"], data["model_version"])
        except KeyError as error:
            raise ProjectFormatError(f"video_index is missing {error.args[0]}") from error


@dataclass(frozen=True, slots=True)
class VideoFragment:
    start: float
    end: float
    scene_id: int | None = None
    thumbnail_path: str | None = None
    source_index: int = 0

    def __post_init__(self) -> None:
        values = (self.start, self.end)
        if (not all(isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(float(value)) for value in values)
                or self.start < 0 or self.end <= self.start):
            raise ProjectFormatError("video fragment has invalid time range")
        if self.scene_id is not None and (not isinstance(self.scene_id, int) or isinstance(self.scene_id, bool) or self.scene_id < 1):
            raise ProjectFormatError("video fragment scene ID must be positive")
        _validate_relative_path(self.thumbnail_path, "fragment thumbnail path")
        if not isinstance(self.source_index, int) or isinstance(self.source_index, bool) or self.source_index < 0:
            raise ProjectFormatError("video fragment source index must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "scene_id": self.scene_id,
                "thumbnail_path": self.thumbnail_path, "source_index": self.source_index}

    @classmethod
    def from_dict(cls, data: Any) -> "VideoFragment":
        if not isinstance(data, dict):
            raise ProjectFormatError("video fragment must be an object")
        return cls(data["start"], data["end"], data.get("scene_id"), data.get("thumbnail_path"),
                   int(data.get("source_index", 0)))


@dataclass(frozen=True, slots=True)
class TimelineState:
    """Pointer to the editable stage-four montage for one source movie."""
    timeline_path: str
    source_fingerprint: str

    def __post_init__(self) -> None:
        _validate_relative_path(self.timeline_path, "timeline path")
        _validate_fingerprint(self.source_fingerprint, "timeline source fingerprint")

    def to_dict(self) -> dict[str, Any]:
        return {"timeline_path": self.timeline_path, "source_fingerprint": self.source_fingerprint}

    @classmethod
    def from_dict(cls, data: Any) -> "TimelineState":
        if not isinstance(data, dict):
            raise ProjectFormatError("timeline_state must be an object")
        return cls(data["timeline_path"], data["source_fingerprint"])


@dataclass(slots=True)
class SegmentMatch:
    segment_id: int
    fragments: list[VideoFragment] = field(default_factory=list)
    candidates: list[list[VideoFragment]] = field(default_factory=list)
    confidence: float | None = None
    needs_review: bool = True
    reason: str | None = None
    manual: bool = False
    context_used: bool = False
    confirmation: str = "automatic"

    def __post_init__(self) -> None:
        if not isinstance(self.segment_id, int) or isinstance(self.segment_id, bool) or self.segment_id < 1:
            raise ProjectFormatError("match segment ID must be positive")
        if self.confidence is not None and (not isinstance(self.confidence, Real) or isinstance(self.confidence, bool)
                                            or not math.isfinite(float(self.confidence)) or not 0 <= self.confidence <= 1):
            raise ProjectFormatError("match confidence must be between zero and one")
        if self.manual and not self.fragments:
            raise ProjectFormatError("a manual match requires fragments")
        if any(not isinstance(value, bool) for value in (self.needs_review, self.manual, self.context_used)):
            raise ProjectFormatError("match flags must be booleans")
        if self.reason is not None and not isinstance(self.reason, str):
            raise ProjectFormatError("match reason must be text")
        if len(self.candidates) > 3:
            raise ProjectFormatError("a match can contain at most three candidates")
        if self.confirmation not in {"automatic", "manual", "bulk"}:
            raise ProjectFormatError("match confirmation must be automatic, manual or bulk")

    def to_dict(self) -> dict[str, Any]:
        return {"segment_id": self.segment_id, "fragments": [item.to_dict() for item in self.fragments],
                "candidates": [[item.to_dict() for item in candidate] for candidate in self.candidates],
                "confidence": self.confidence, "needs_review": self.needs_review,
                "reason": self.reason, "manual": self.manual, "context_used": self.context_used,
                "confirmation": self.confirmation}

    @classmethod
    def from_dict(cls, data: Any) -> "SegmentMatch":
        if not isinstance(data, dict):
            raise ProjectFormatError("segment match must be an object")
        return cls(data["segment_id"], [VideoFragment.from_dict(item) for item in data.get("fragments", [])],
                   [[VideoFragment.from_dict(item) for item in candidate] for candidate in data.get("candidates", [])],
                   data.get("confidence"), data.get("needs_review", True), data.get("reason"),
                   data.get("manual", False), data.get("context_used", False),
                   data.get("confirmation", "manual" if data.get("manual", False) else "automatic"))


def normalize_comparison_text(text: str) -> str:
    """Ignore only non-visible whitespace changes when matching old audio."""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def audio_path_for_order(order: int) -> str:
    return f"audio/{order:03d}.mp3"


def _validate_relative_path(path: str | None, field_name: str) -> None:
    if path is None:
        return
    if not isinstance(path, str):
        raise ProjectFormatError(f"{field_name} must be a project-relative path")
    candidate = PurePath(path.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise ProjectFormatError(f"{field_name} must be a project-relative path")


def _validate_fingerprint(value: str, field_name: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ProjectFormatError(f"{field_name} must be a SHA-256 hash")


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
    audio_revision: int = 0

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
            if not math.isfinite(self.duration) or self.duration <= 0:
                raise ProjectFormatError("segment duration must be a positive finite number")
        if not isinstance(self.audio_revision, int) or self.audio_revision < 0:
            raise ProjectFormatError("audio revision must be a non-negative integer")
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
        if (not isinstance(duration, (int, float)) or isinstance(duration, bool)
                or not math.isfinite(duration) or duration <= 0):
            raise ProjectFormatError("segment duration must be a positive finite number")
        self.excluded_from_tts = False
        self.status = SegmentStatus.READY
        self.audio = audio_path_for_order(self.order)
        self.duration = duration
        self.tts_settings = settings
        self.error = None
        self.status_before_exclusion = None
        self.audio_revision += 1

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
                "status_before_exclusion": self.status_before_exclusion.value if self.status_before_exclusion else None,
                "audio_revision": self.audio_revision}

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
                error=data.get("error"), status_before_exclusion=SegmentStatus(prior) if prior else None,
                audio_revision=int(data.get("audio_revision", 0)))
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
    video_source: VideoSource | None = None
    active_subtitles: SubtitleState | None = None
    scene_analysis: SceneState | None = None
    video_index: VideoIndexState | None = None
    segment_matches: list[SegmentMatch] = field(default_factory=list)
    timeline_state: TimelineState | None = None
    format_version: int = PROJECT_FORMAT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ProjectFormatError("project name must be a non-empty string")
        if self.format_version != PROJECT_FORMAT_VERSION:
            raise ProjectFormatError(f"unsupported project format version: {self.format_version}")
        self._validate_segments()
        self._validate_video_state()

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
        self.invalidate_segment_matches({segment_id})
        return removed

    def edit_segment(self, segment_id: int, text: str) -> None:
        self.segments[self._index_for_id(segment_id)].edit_text(text)
        self.invalidate_segment_matches({segment_id})

    def set_segment_excluded(self, segment_id: int, excluded: bool) -> None:
        (self.segments[self._index_for_id(segment_id)].exclude() if excluded
         else self.segments[self._index_for_id(segment_id)].include())
        self.invalidate_segment_matches({segment_id})

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
        self.invalidate_segment_matches({item.segment_id for item in created})
        return created

    def merge_with_next(self, segment_id: int) -> Segment:
        index = self._index_for_id(segment_id)
        if index == len(self.segments) - 1:
            raise ProjectFormatError("the last segment cannot be merged with a next segment")
        primary = self.segments[index]
        following = self.segments.pop(index + 1)
        primary.edit_text(f"{primary.text}\n{following.text}")
        self._reindex()
        self.invalidate_segment_matches({primary.segment_id, following.segment_id})
        return primary

    def set_tts_settings(self, settings: TTSSettings) -> None:
        if settings == self.tts_settings:
            return
        self.tts_settings = settings
        for segment in self.segments:
            if segment.tts_settings != settings:
                segment.mark_stale()
        self.invalidate_segment_matches({segment.segment_id for segment in self.segments})

    def replace_segments(self, texts: Iterable[str]) -> None:
        values = list(texts)
        if not all(isinstance(text, str) for text in values):
            raise ProjectFormatError("segment text must be strings")
        reusable: dict[str, deque[Segment]] = defaultdict(deque)
        for segment in self.segments:
            if segment.tts_settings == self.tts_settings:
                reusable[normalize_comparison_text(segment.text)].append(segment)
        replacement: list[Segment] = []
        retained_ids: set[int] = set()
        for text in values:
            matches = reusable[normalize_comparison_text(text)]
            if matches:
                segment = matches.popleft()
                segment.text = text
                retained_ids.add(segment.segment_id)
            else:
                segment = Segment(self.next_segment_id, 1, text)
                self.next_segment_id += 1
            replacement.append(segment)
        self.segments = replacement
        self._reindex()
        self.invalidate_segment_matches({item.segment_id for item in self.segment_matches if item.segment_id not in retained_ids})

    def invalidate_segment_matches(self, segment_ids: Iterable[int] | None = None) -> None:
        """Drop only choices made invalid by a changed script or audio segment."""
        if segment_ids is None:
            self.segment_matches = []
            return
        stale = set(segment_ids)
        self.segment_matches = [item for item in self.segment_matches if item.segment_id not in stale]

    def reconcile_audio_files(self, project_root: Path) -> None:
        missing: set[int] = set()
        for segment in self.segments:
            if segment.audio and not (project_root / segment.audio).is_file():
                segment.mark_file_missing()
                missing.add(segment.segment_id)
        self.invalidate_segment_matches(missing)

    def reconcile_analysis_files(self, project_root: Path) -> bool:
        """Drop active references whose files are no longer present on disk."""
        changed = False
        if self.active_subtitles and (not (project_root / self.active_subtitles.subtitles_path).is_file()
                                      or not (project_root / self.active_subtitles.transcript_path).is_file()):
            self.active_subtitles = None
            self.video_index = None
            self.segment_matches = []
            changed = True
        if self.scene_analysis and not (project_root / self.scene_analysis.scenes_path).is_file():
            self.scene_analysis = None
            self.video_index = None
            self.segment_matches = []
            changed = True
        if self.video_index and not (project_root / self.video_index.index_path).is_file():
            self.video_index = None
            self.segment_matches = []
            changed = True
        return changed

    @property
    def video_is_available(self) -> bool:
        return bool(self.video_source and all(Path(part.path).is_file() for part in self.video_source.source_parts))

    def set_video_source(self, source: VideoSource) -> bool:
        """Set a source and return whether it invalidated video-derived state."""
        changed = self.video_source is not None and self.video_source.fingerprint != source.fingerprint
        self.video_source = source
        if changed:
            self.active_subtitles = None
            self.scene_analysis = None
            self.video_index = None
            self.segment_matches = []
            self.timeline_state = None
        return changed

    def set_active_subtitles(self, state: SubtitleState) -> None:
        if not self.video_source or state.source_fingerprint != self.video_source.fingerprint:
            raise ProjectFormatError("subtitles do not belong to the current video source")
        self.active_subtitles = state
        self.video_index = None
        self.segment_matches = []

    def set_scene_analysis(self, state: SceneState) -> None:
        if not self.video_source or state.source_fingerprint != self.video_source.fingerprint:
            raise ProjectFormatError("scenes do not belong to the current video source")
        self.scene_analysis = state
        self.video_index = None
        self.segment_matches = []

    def set_video_index(self, state: VideoIndexState) -> None:
        if not self.video_source or state.source_fingerprint != self.video_source.fingerprint:
            raise ProjectFormatError("video index does not belong to the current video source")
        self.video_index = state

    def replace_segment_matches(self, matches: Iterable[SegmentMatch]) -> None:
        values = list(matches)
        known = {segment.segment_id for segment in self.segments}
        if any(item.segment_id not in known for item in values) or len({item.segment_id for item in values}) != len(values):
            raise ProjectFormatError("matches must belong to unique current segments")
        current = {segment.segment_id for segment in self.segments if segment.has_current_audio}
        manual = {item.segment_id: item for item in self.segment_matches if item.manual and item.segment_id in current}
        self.segment_matches = [manual.get(item.segment_id, item) for item in values]

    def to_dict(self) -> dict[str, Any]:
        self._validate_segments()
        self._validate_video_state()
        return {"format_version": self.format_version, "name": self.name,
                "tts_settings": self.tts_settings.to_dict(), "script_text": self.script_text,
                "next_segment_id": self.next_segment_id,
                "segments": [segment.to_dict() for segment in self.segments],
                "video_source": self.video_source.to_dict() if self.video_source else None,
                "active_subtitles": self.active_subtitles.to_dict() if self.active_subtitles else None,
                "scene_analysis": self.scene_analysis.to_dict() if self.scene_analysis else None,
                "video_index": self.video_index.to_dict() if self.video_index else None,
                "segment_matches": [item.to_dict() for item in self.segment_matches],
                "timeline_state": self.timeline_state.to_dict() if self.timeline_state else None}

    @classmethod
    def from_dict(cls, data: Any) -> "Project":
        if not isinstance(data, dict):
            raise ProjectFormatError("project.json must contain an object")
        try:
            version = data["format_version"]
            if version in {1, 2, 3, 4}:
                # Stage-one projects are upgraded in memory and gain video state on next save.
                version = PROJECT_FORMAT_VERSION
            return cls(name=data["name"], format_version=version,
                tts_settings=TTSSettings.from_dict(data["tts_settings"]),
                next_segment_id=data["next_segment_id"],
                segments=[Segment.from_dict(value) for value in data["segments"]],
                video_source=VideoSource.from_dict(data["video_source"]) if data.get("video_source") else None,
                active_subtitles=SubtitleState.from_dict(data["active_subtitles"]) if data.get("active_subtitles") else None,
                scene_analysis=SceneState.from_dict(data["scene_analysis"]) if data.get("scene_analysis") else None,
                video_index=VideoIndexState.from_dict(data["video_index"]) if data.get("video_index") else None,
                segment_matches=[SegmentMatch.from_dict(item) for item in data.get("segment_matches", [])],
                timeline_state=TimelineState.from_dict(data["timeline_state"]) if data.get("timeline_state") else None)
        except KeyError as error:
            raise ProjectFormatError(f"project is missing {error.args[0]}") from error
        except ProjectFormatError:
            raise
        except (TypeError, ValueError) as error:
            raise ProjectFormatError(f"project contains invalid data: {error}") from error

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

    def _validate_video_state(self) -> None:
        if self.active_subtitles and (not self.video_source or self.active_subtitles.source_fingerprint != self.video_source.fingerprint):
            raise ProjectFormatError("active subtitles must belong to the current video source")
        if self.scene_analysis and (not self.video_source or self.scene_analysis.source_fingerprint != self.video_source.fingerprint):
            raise ProjectFormatError("scene analysis must belong to the current video source")
        if self.video_index and (not self.video_source or self.video_index.source_fingerprint != self.video_source.fingerprint):
            raise ProjectFormatError("video index must belong to the current video source")
        if self.timeline_state and (not self.video_source or self.timeline_state.source_fingerprint != self.video_source.fingerprint):
            raise ProjectFormatError("timeline must belong to the current video source")
        if len({item.segment_id for item in self.segment_matches}) != len(self.segment_matches):
            raise ProjectFormatError("matches must have unique segment IDs")
        known_ids = {segment.segment_id for segment in self.segments}
        if any(item.segment_id not in known_ids for item in self.segment_matches):
            raise ProjectFormatError("matches must belong to current segments")
        if any(len(item.candidates) > 3 for item in self.segment_matches):
            raise ProjectFormatError("a match can contain at most three candidates")
        if self.segment_matches and not self.video_source:
            raise ProjectFormatError("matches require a current video source")
        current_audio_ids = {segment.segment_id for segment in self.segments if segment.has_current_audio}
        if any(item.manual and item.segment_id not in current_audio_ids for item in self.segment_matches):
            raise ProjectFormatError("manual matches require current narration audio")
        if self.video_source:
            fragments = (fragment for item in self.segment_matches
                         for group in ([item.fragments] + item.candidates) for fragment in group)
            if any(fragment.end > self.video_source.duration or fragment.source_index >= len(self.video_source.source_parts)
                   for fragment in fragments):
                raise ProjectFormatError("match fragment exceeds the current video source")
