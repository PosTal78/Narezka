"""Persistent, source-safe montage rules used by the editor and exporter."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Iterable
import uuid

from narezchik.models import Project, VideoFragment


class TimelineError(RuntimeError):
    pass


EPSILON = 0.02


@dataclass(slots=True)
class TimelineEntry:
    segment_id: int
    fragments: list[VideoFragment] = field(default_factory=list)
    audio_duration: float = 0.0
    audio_revision: int = 0
    needs_review: bool = True
    reason: str | None = None
    edited_by_user: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.segment_id, int) or isinstance(self.segment_id, bool) or self.segment_id < 1:
            raise TimelineError("Строка монтажа содержит некорректный ID реплики.")
        if (not isinstance(self.audio_duration, (int, float)) or isinstance(self.audio_duration, bool)
                or not math.isfinite(self.audio_duration) or self.audio_duration < 0):
            raise TimelineError("Строка монтажа содержит некорректную длительность озвучки.")
        if not isinstance(self.audio_revision, int) or isinstance(self.audio_revision, bool) or self.audio_revision < 0:
            raise TimelineError("Строка монтажа содержит некорректную ревизию озвучки.")
        if not isinstance(self.needs_review, bool):
            raise TimelineError("Строка монтажа содержит некорректный статус проверки.")
        if self.reason is not None and not isinstance(self.reason, str):
            raise TimelineError("Строка монтажа содержит некорректную причину проверки.")
        if not isinstance(self.edited_by_user, bool):
            raise TimelineError("Строка монтажа содержит некорректный признак ручной правки.")

    @property
    def video_duration(self) -> float:
        return sum(item.end - item.start for item in self.fragments)

    def to_dict(self) -> dict:
        return {"segment_id": self.segment_id, "fragments": [item.to_dict() for item in self.fragments],
                "audio_duration": self.audio_duration, "audio_revision": self.audio_revision,
                "needs_review": self.needs_review, "reason": self.reason,
                "edited_by_user": self.edited_by_user}

    @classmethod
    def from_dict(cls, value: dict) -> "TimelineEntry":
        return cls(int(value["segment_id"]), [VideoFragment.from_dict(item) for item in value.get("fragments", [])],
                   float(value.get("audio_duration", 0)), int(value.get("audio_revision", 0)),
                   bool(value.get("needs_review", True)), value.get("reason"),
                   bool(value.get("edited_by_user", False)))


@dataclass(slots=True)
class Timeline:
    source_fingerprint: str
    entries: list[TimelineEntry] = field(default_factory=list)

    def __post_init__(self) -> None:
        if (not isinstance(self.source_fingerprint, str) or len(self.source_fingerprint) != 64
                or any(char not in "0123456789abcdef" for char in self.source_fingerprint)):
            raise TimelineError("Монтаж содержит некорректный отпечаток фильма.")

    def to_dict(self) -> dict:
        return {"source_fingerprint": self.source_fingerprint, "entries": [item.to_dict() for item in self.entries]}

    @classmethod
    def from_dict(cls, value: dict) -> "Timeline":
        return cls(value["source_fingerprint"], [TimelineEntry.from_dict(item) for item in value.get("entries", [])])


def create_from_matches(project: Project) -> Timeline:
    if not project.video_source:
        raise TimelineError("Сначала выберите исходный фильм.")
    matches = {item.segment_id: item for item in project.segment_matches}
    entries: list[TimelineEntry] = []
    for segment in project.segments:
        if not segment.has_current_audio:
            continue
        match = matches.get(segment.segment_id)
        fragments = list(match.fragments) if match and not match.needs_review else []
        entry = TimelineEntry(segment.segment_id, fragments, segment.duration or 0, segment.audio_revision,
                              not bool(fragments), match.reason if match and match.needs_review else None)
        trim_to_audio(entry)
        entries.append(entry)
    return Timeline(project.video_source.fingerprint, entries)


def trim_to_audio(entry: TimelineEntry) -> None:
    """Keep the user's sequence but make its final frame land on narration end."""
    remaining = entry.audio_duration
    trimmed: list[VideoFragment] = []
    for fragment in entry.fragments:
        if remaining <= EPSILON:
            break
        end = min(fragment.end, fragment.start + remaining)
        if end - fragment.start > EPSILON:
            trimmed.append(VideoFragment(fragment.start, end, fragment.scene_id, fragment.thumbnail_path,
                                         fragment.source_index))
            remaining -= end - fragment.start
    entry.fragments = trimmed


def add_fragment(entry: TimelineEntry, fragment: VideoFragment) -> None:
    entry.edited_by_user = True
    entry.fragments.append(fragment)
    trim_to_audio(entry)
    _mark_coverage(entry)


def replace_fragment(entry: TimelineEntry, index: int, fragment: VideoFragment) -> None:
    if not 0 <= index < len(entry.fragments):
        raise TimelineError("Выберите существующий фрагмент монтажа.")
    entry.edited_by_user = True
    entry.fragments[index] = fragment
    trim_to_audio(entry)
    _mark_coverage(entry)


def remove_fragment(entry: TimelineEntry, index: int) -> None:
    if not 0 <= index < len(entry.fragments):
        raise TimelineError("Выберите существующий фрагмент монтажа.")
    entry.edited_by_user = True
    entry.fragments.pop(index)
    _mark_coverage(entry)


def _mark_coverage(entry: TimelineEntry) -> None:
    entry.needs_review = abs(entry.video_duration - entry.audio_duration) > EPSILON
    entry.reason = "Кадры ещё не покрывают всю озвучку." if entry.needs_review else None


def autofill(entry: TimelineEntry, *, source_duration: float, scenes: Iterable[object] = ()) -> bool:
    """Extend from the end of the last user-selected piece, never silently."""
    if not entry.fragments:
        entry.needs_review = True; entry.reason = "Сначала добавьте первый фрагмент."
        return False
    missing = entry.audio_duration - entry.video_duration
    if missing <= EPSILON:
        trim_to_audio(entry); entry.needs_review = False; entry.reason = None
        return True
    position = entry.fragments[-1].end
    candidates = list(scenes)
    for scene in candidates:
        start, end = max(position, float(scene.start)), min(float(scene.end), source_duration)
        if end <= start + EPSILON:
            continue
        entry.fragments.append(VideoFragment(start, min(end, start + missing), getattr(scene, "scene_id", None),
                                             None, getattr(scene, "source_index", 0)))
        missing = entry.audio_duration - entry.video_duration
        position = end
        if missing <= EPSILON:
            break
    if missing > EPSILON and position < source_duration:
        entry.fragments.append(VideoFragment(position, min(source_duration, position + missing)))
    trim_to_audio(entry)
    entry.needs_review = entry.video_duration + EPSILON < entry.audio_duration
    entry.reason = "До конца фильма не хватило кадров." if entry.needs_review else None
    return not entry.needs_review


def synchronize(project: Project, timeline: Timeline, *, scenes: Iterable[object] = ()) -> bool:
    """Reconcile project rows without discarding still-relevant manual edits."""
    if not project.video_source or timeline.source_fingerprint != project.video_source.fingerprint:
        raise TimelineError("Монтаж создан для другого фильма.")
    existing: dict[int, TimelineEntry] = {}
    for entry in timeline.entries:
        if entry.segment_id in existing:
            raise TimelineError(f"Реплика {entry.segment_id}: строка монтажа продублирована.")
        existing[entry.segment_id] = entry
    matches = {item.segment_id: item for item in project.segment_matches}
    entries: list[TimelineEntry] = []
    changed = False
    for segment in project.segments:
        entry = existing.get(segment.segment_id)
        if segment.excluded_from_tts:
            # Keep a reusable edit while the exclusion is reversible, but it is
            # ignored by validation and export.
            if entry:
                entries.append(entry)
            continue
        if not segment.has_current_audio:
            if entry:
                if not entry.needs_review or entry.reason != "Сначала подготовьте актуальную озвучку реплики.":
                    changed = True
                entry.needs_review = True
                entry.reason = "Сначала подготовьте актуальную озвучку реплики."
                entries.append(entry)
            continue
        if entry is None:
            match = matches.get(segment.segment_id)
            fragments = list(match.fragments) if match and not match.needs_review else []
            entry = TimelineEntry(
                segment.segment_id,
                fragments,
                segment.duration or 0,
                segment.audio_revision,
                not bool(fragments),
                match.reason if match and match.needs_review else None,
            )
            trim_to_audio(entry)
            changed = True
        elif not entry.edited_by_user:
            match = matches.get(segment.segment_id)
            incoming = list(match.fragments) if match and not match.needs_review else []
            if incoming and entry.fragments != incoming:
                entry.fragments = incoming
                entry.audio_duration = segment.duration or 0
                entry.audio_revision = segment.audio_revision
                entry.needs_review = False
                entry.reason = None
                trim_to_audio(entry)
                changed = True
        elif (entry.audio_revision != segment.audio_revision
              or abs(entry.audio_duration - (segment.duration or 0)) > EPSILON):
            entry.audio_duration = segment.duration or 0
            entry.audio_revision = segment.audio_revision
            trim_to_audio(entry)
            entry.needs_review = True
            entry.reason = "Озвучка изменилась — проверьте кадры."
            changed = True
        entries.append(entry)
        if entry.fragments and not entry.edited_by_user and entry.video_duration + EPSILON < entry.audio_duration:
            if autofill(entry, source_duration=project.video_source.duration, scenes=scenes):
                changed = True
    if [entry.segment_id for entry in entries] != [entry.segment_id for entry in timeline.entries]:
        changed = True
    timeline.entries = entries
    return changed


def validate(project: Project, timeline: Timeline, root: Path) -> list[str]:
    issues: list[str] = []
    source = project.video_source
    if not source or not all(Path(part.path).is_file() for part in source.source_parts):
        return ["Исходный фильм недоступен."]
    if timeline.source_fingerprint != source.fingerprint:
        return ["Монтаж создан для другого фильма."]
    known_ids = {segment.segment_id for segment in project.segments}
    entries: dict[int, TimelineEntry] = {}
    for entry in timeline.entries:
        if entry.segment_id not in known_ids:
            issues.append(f"Монтаж содержит удалённую реплику {entry.segment_id}.")
            continue
        if entry.segment_id in entries:
            issues.append(f"Реплика {entry.segment_id}: строка монтажа продублирована.")
            continue
        entries[entry.segment_id] = entry
    for segment in project.segments:
        if segment.excluded_from_tts:
            continue
        if not segment.has_current_audio:
            issues.append(f"Реплика {segment.segment_id}: актуальная озвучка не готова.")
            continue
        entry = entries.get(segment.segment_id)
        if not entry:
            issues.append(f"Реплика {segment.segment_id}: нет строки монтажа."); continue
        if entry.audio_revision != segment.audio_revision or abs(entry.audio_duration - (segment.duration or 0)) > EPSILON:
            issues.append(f"Реплика {segment.segment_id}: озвучка изменилась, проверьте кадры."); continue
        if not segment.audio or not (root / segment.audio).is_file():
            issues.append(f"Реплика {segment.segment_id}: аудиофайл недоступен.")
        if entry.needs_review or abs(entry.video_duration - entry.audio_duration) > EPSILON:
            issues.append(f"Реплика {segment.segment_id}: кадры не готовы.")
        for fragment in entry.fragments:
            if (fragment.start >= source.duration or fragment.end > source.duration + EPSILON
                    or fragment.source_index >= len(source.source_parts)):
                issues.append(f"Реплика {segment.segment_id}: фрагмент выходит за пределы фильма.")
    if not any(segment.has_current_audio for segment in project.segments):
        issues.append("Нет готовых озвученных реплик для экспорта.")
    return issues


def load(root: Path, expected_fingerprint: str) -> Timeline | None:
    path = root / "timeline" / "timeline.json"
    if not path.is_file(): return None
    try: value = Timeline.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, KeyError) as error: raise TimelineError("Файл монтажа повреждён.") from error
    if value.source_fingerprint != expected_fingerprint: return None
    return value


def save(root: Path, timeline: Timeline) -> Path:
    directory = root / "timeline"; directory.mkdir(parents=True, exist_ok=True); destination = directory / "timeline.json"
    descriptor, name = tempfile.mkstemp(prefix=".timeline-", suffix=".tmp", dir=directory, text=True)
    temporary = Path(name)
    final = root / "export" / "final.mp4"
    archived: Path | None = None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(timeline.to_dict(), stream, ensure_ascii=False, indent=2); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        if final.is_file():
            archive = root / "export" / "retired"
            archive.mkdir(parents=True, exist_ok=True)
            archived = archive / f"final-{uuid.uuid4().hex}.mp4"
            final.replace(archived)
        os.replace(temporary, destination)
    except OSError:
        temporary.unlink(missing_ok=True)
        if archived and archived.is_file() and not final.exists():
            archived.replace(final)
        raise
    return destination
