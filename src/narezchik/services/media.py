"""Small adapters for ffprobe and safe audio filename moves."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import uuid


class MediaError(RuntimeError):
    pass


def ffprobe_is_available() -> bool:
    """Return whether the required duration tool can be started from PATH."""
    return shutil.which("ffprobe") is not None


def probe_duration(path: Path) -> float:
    try:
        completed = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nk=1:nw=1", str(path)],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError as error:
        raise MediaError(
            "ffprobe не найден. Установите полный комплект FFmpeg и добавьте его в PATH."
        ) from error
    if completed.returncode != 0:
        raise MediaError(completed.stderr.strip() or "ffprobe не смог определить длительность")
    try:
        return float(completed.stdout.strip())
    except ValueError as error:
        raise MediaError("ffprobe вернул некорректную длительность") from error


def rename_audio_files(project_root: Path, changes: dict[str, str]) -> None:
    """Rename colliding numbered audio files in two phases."""
    moves = [(project_root / old, project_root / new) for old, new in changes.items() if old != new and (project_root / old).is_file()]
    temporary: list[tuple[Path, Path]] = []
    for source, destination in moves:
        temp = source.with_name(f".{source.name}.{uuid.uuid4().hex}.tmp")
        source.rename(temp)
        temporary.append((temp, destination))
    for source, destination in temporary:
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.replace(destination)


def archive_retired_audio_files(project_root: Path, retired: dict[int, str]) -> None:
    """Keep MP3 files from removed segments out of numbered active slots."""
    archive = project_root / "audio" / "retired"
    for segment_id, relative_path in retired.items():
        source = project_root / relative_path
        if not source.is_file():
            continue
        archive.mkdir(parents=True, exist_ok=True)
        destination = archive / f"{segment_id}-{source.name}"
        if destination.exists():
            destination = archive / f"{segment_id}-{uuid.uuid4().hex}-{source.name}"
        source.replace(destination)
