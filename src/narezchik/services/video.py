"""Video probing and content identity services.

The source video deliberately stays outside a project.  Its SHA-256 identity,
not its filename, is used to decide whether cached analysis is still valid.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import subprocess
from typing import Callable

from narezchik.models import VideoSource
from .media import media_tool


class VideoError(RuntimeError):
    pass


class VideoCancelled(VideoError):
    pass


@dataclass(frozen=True, slots=True)
class VideoMetadata:
    duration: float
    width: int
    height: int
    fps: float


def _as_float(value: object) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise VideoError("ffprobe вернул некорректные метаданные видео") from error
    if not math.isfinite(result):
        raise VideoError("ffprobe вернул некорректные метаданные видео")
    return result


def _fps(value: str | None) -> float:
    if not value or value == "0/0":
        return 0.0
    numerator, separator, denominator = value.partition("/")
    if not separator:
        return _as_float(value)
    divisor = _as_float(denominator)
    if divisor == 0:
        raise VideoError("ffprobe вернул некорректную частоту кадров")
    return _as_float(numerator) / divisor


def probe_video(path: Path) -> VideoMetadata:
    if path.suffix.lower() not in {".mp4", ".mkv"}:
        raise VideoError("Поддерживаются только файлы MP4 и MKV.")
    try:
        completed = subprocess.run(
            [media_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height,avg_frame_rate",
             "-of", "json", str(path)], capture_output=True, text=True, check=False,
        )
    except (FileNotFoundError, RuntimeError) as error:
        raise VideoError("ffprobe не найден. Установите полный комплект FFmpeg и добавьте его в PATH.") from error
    if completed.returncode != 0:
        raise VideoError(completed.stderr.strip() or "Не удалось прочитать метаданные видео.")
    try:
        payload = json.loads(completed.stdout)
        stream = next(item for item in payload["streams"] if item.get("codec_type") == "video")
        return VideoMetadata(_as_float(payload["format"]["duration"]), int(stream["width"]), int(stream["height"]), _fps(stream.get("avg_frame_rate")))
    except (KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError) as error:
        raise VideoError("В файле не найдена пригодная видеодорожка.") from error


def has_audio_stream(path: Path) -> bool:
    """A movie without audio remains exportable; the quiet-background option becomes silence."""
    completed = subprocess.run([media_tool("ffprobe"), "-v", "error", "-select_streams", "a",
                                "-show_entries", "stream=codec_type", "-of", "json", str(path)],
                               capture_output=True, text=True, check=False)
    if completed.returncode:
        return False
    try:
        return bool(json.loads(completed.stdout).get("streams"))
    except json.JSONDecodeError:
        return False


def sha256_file(path: Path, *, cancelled: Callable[[], bool] | None = None,
                progress: Callable[[int, int], None] | None = None) -> str:
    total = path.stat().st_size
    processed = 0
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            if cancelled and cancelled():
                raise VideoCancelled("Проверка файла отменена.")
            digest.update(block)
            processed += len(block)
            if progress:
                progress(processed, total)
    return digest.hexdigest()


def inspect_video(path: Path, *, cancelled: Callable[[], bool] | None = None,
                  progress: Callable[[int, int], None] | None = None) -> VideoSource:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise VideoError("Исходный файл не найден.")
    metadata = probe_video(path)
    return VideoSource(str(path), sha256_file(path, cancelled=cancelled, progress=progress), path.name,
                       metadata.duration, metadata.width, metadata.height, metadata.fps)


def inspect_videos(paths: list[Path], *, cancelled: Callable[[], bool] | None = None,
                   progress: Callable[[int, int], None] | None = None) -> VideoSource:
    if not paths:
        raise VideoError("Выберите хотя бы одну часть фильма.")
    resolved = [path.expanduser().resolve() for path in paths]
    if any(not path.is_file() for path in resolved):
        raise VideoError("Одна из частей фильма не найдена.")
    sizes = [path.stat().st_size for path in resolved]
    completed = 0
    parts: list[VideoSource] = []
    for path, size in zip(resolved, sizes):
        part = inspect_video(path, cancelled=cancelled,
                             progress=(lambda current, _total, base=completed:
                                       progress(base + current, sum(sizes))) if progress else None)
        parts.append(part)
        completed += size
    return VideoSource.combine(parts)


def ensure_current_source(path: Path, fingerprint: str, *, cancelled: Callable[[], bool] | None = None,
                          progress: Callable[[int, int], None] | None = None) -> None:
    if not path.is_file():
        raise VideoError("Исходный файл не найден. Выберите его заново.")
    if sha256_file(path, cancelled=cancelled, progress=progress) != fingerprint:
        raise VideoError("Содержимое исходного файла изменилось. Выберите фильм заново, чтобы не использовать неверный анализ.")


def ensure_current_sources(source: VideoSource, *, cancelled: Callable[[], bool] | None = None,
                           progress: Callable[[int, int], None] | None = None) -> None:
    parts = source.source_parts
    sizes = [Path(part.path).stat().st_size if Path(part.path).is_file() else 0 for part in parts]
    completed = 0
    for part, size in zip(parts, sizes):
        ensure_current_source(Path(part.path), part.fingerprint, cancelled=cancelled,
                              progress=(lambda current, _total, base=completed:
                                        progress(base + current, sum(sizes))) if progress else None)
        completed += size
