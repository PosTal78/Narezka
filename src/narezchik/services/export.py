"""FFmpeg command construction and atomic export for a validated timeline."""
from __future__ import annotations

from pathlib import Path
import math
import subprocess
import uuid
from typing import Callable

from narezchik.models import Project
from .media import media_tool, probe_duration
from .timeline import Timeline, validate
from .video import ensure_current_sources, has_audio_stream


class ExportError(RuntimeError): pass
class ExportCancelled(ExportError): pass


def _output_size(project: Project, resolution: str) -> tuple[int, int]:
    source = project.video_source
    assert source
    if resolution == "1080p": return (1920, 1080)
    if resolution == "720p": return (1280, 720)
    if resolution != "Original":
        raise ExportError("Неизвестное разрешение экспорта.")
    if source.width <= 0 or source.height <= 0:
        raise ExportError("У исходного фильма некорректное разрешение.")
    return source.width, source.height


def _output_fps(project: Project, fps: str) -> float:
    if fps == "Original":
        rate = project.video_source.fps if project.video_source else 0
    elif fps in {"30", "60"}:
        rate = float(fps)
    else:
        raise ExportError("Неизвестная частота кадров экспорта.")
    if not math.isfinite(rate) or rate <= 0:
        raise ExportError("У исходного фильма некорректная частота кадров.")
    return rate


def _verify_audio_durations(project: Project, root: Path,
                            progress: Callable[[str], None] | None = None) -> None:
    segments = [segment for segment in project.segments if segment.has_current_audio]
    for index, segment in enumerate(segments, 1):
        if progress:
            progress(f"Проверяю озвучку {index}/{len(segments)}…")
        try:
            actual = probe_duration(root / segment.audio)
        except Exception as error:
            raise ExportError(f"Реплика {segment.segment_id}: не удалось проверить аудиофайл: {error}") from error
        expected = segment.duration or 0
        if not math.isfinite(actual) or abs(actual - expected) > 0.05:
            raise ExportError(
                f"Реплика {segment.segment_id}: аудиофайл изменился "
                f"({actual:.2f} с вместо {expected:.2f} с). Перегенерируйте озвучку."
            )


def build_export_command(project: Project, timeline: Timeline, root: Path, temporary: Path,
                         *, resolution: str = "Original", fps: str = "Original", source_audio: bool = False) -> list[str]:
    source = project.video_source
    if not source: raise ExportError("Исходный фильм не выбран.")
    width, height = _output_size(project, resolution)
    rate = _output_fps(project, fps)
    by_segment = {entry.segment_id: entry for entry in timeline.entries}
    # Validation guarantees that every current narration has one complete row.
    # Deriving this list from the project prevents stale or excluded rows from
    # leaking old narration into a new export.
    entries = [by_segment[segment.segment_id] for segment in project.segments if segment.has_current_audio]
    # The worker reads stderr only after completion.  Restricting FFmpeg to
    # errors avoids filling the pipe and freezing a long export.
    parts=source.source_parts
    part_has_audio=[has_audio_stream(Path(part.path)) for part in parts] if source_audio else [False]*len(parts)
    command = [media_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y"]
    for part in parts:command += ["-i", part.path]
    for entry in entries:
        segment = next(item for item in project.segments if item.segment_id == entry.segment_id)
        command += ["-i", str(root / segment.audio)]
    filters: list[str] = []
    video_labels: list[str] = []; background_labels: list[str] = []; narration_labels: list[str] = []
    number = 0
    for audio_index, entry in enumerate(entries, 1):
        for fragment in entry.fragments:
            for part_index,(part,offset) in enumerate(zip(parts,source.offsets)):
                global_start=max(fragment.start,offset);global_end=min(fragment.end,offset+part.duration)
                if global_end<=global_start:continue
                local_start=global_start-offset;local_end=global_end-offset
                label = f"v{number}"; number += 1
                filters.append(f"[{part_index}:v]trim=start={local_start:.6f}:end={local_end:.6f},setpts=PTS-STARTPTS,"
                               f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                               f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2[{label}]")
                video_labels.append(f"[{label}]")
                if source_audio:
                    alabel = f"b{number}";duration=local_end-local_start
                    if part_has_audio[part_index]:
                        filters.append(f"[{part_index}:a]atrim=start={local_start:.6f}:end={local_end:.6f},asetpts=PTS-STARTPTS,"
                                       f"afade=t=in:st=0:d=0.08,afade=t=out:st={max(0,duration-.08):.6f}:d=0.08[{alabel}]")
                    else:
                        filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={duration:.6f}[{alabel}]")
                    background_labels.append(f"[{alabel}]")
        nlabel = f"n{entry.segment_id}"
        filters.append(f"[{len(parts)+audio_index-1}:a]asetpts=PTS-STARTPTS,atrim=duration={entry.audio_duration:.6f}[{nlabel}]")
        narration_labels.append(f"[{nlabel}]")
    total_duration = sum(entry.audio_duration for entry in entries)
    filters.append("".join(video_labels) + f"concat=n={len(video_labels)}:v=1:a=0[assembled]")
    filters.append(f"[assembled]tpad=stop_mode=clone:stop_duration=1,trim=duration={total_duration:.6f},"
                   f"setpts=PTS-STARTPTS,fps={rate:.6f}[vout]")
    filters.append("".join(narration_labels) + f"concat=n={len(narration_labels)}:v=0:a=1[narration]")
    if source_audio and background_labels:
        filters.append("".join(background_labels) + f"concat=n={len(background_labels)}:v=0:a=1[background]")
        filters.append("[background]volume=0.1[quiet]")
        filters.append("[narration][quiet]amix=inputs=2:duration=first:normalize=0[aout]")
    else: filters.append("[narration]anull[aout]")
    script = temporary.with_suffix(".filters.txt"); script.write_text(";\n".join(filters), encoding="utf-8")
    return command + ["-filter_complex_script", str(script), "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(temporary)]


def export(project: Project, timeline: Timeline, root: Path, *, resolution: str = "Original", fps: str = "Original",
           source_audio: bool = False, cancelled: Callable[[], bool] | None = None,
           progress: Callable[[str], None] | None = None) -> Path:
    source = project.video_source
    if not source:
        raise ExportError("Исходный фильм не выбран.")
    if progress:
        progress("Проверяю исходный фильм…")
    try:
        ensure_current_sources(
            source,
            cancelled=cancelled,
            progress=(lambda _current, _total: progress("Проверяю исходный фильм…")) if progress else None,
        )
    except Exception as error:
        if cancelled and cancelled():
            raise ExportCancelled("Экспорт отменён.") from error
        raise ExportError(str(error)) from error
    issues = validate(project, timeline, root)
    if issues: raise ExportError("\n".join(issues))
    _verify_audio_durations(project, root, progress)
    destination = root / "export" / "final.mp4"; destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".final-{uuid.uuid4().hex}.mp4")
    command = build_export_command(project, timeline, root, temporary, resolution=resolution, fps=fps,
                                   source_audio=source_audio and any(has_audio_stream(Path(part.path)) for part in source.source_parts))
    script = temporary.with_suffix(".filters.txt")
    try:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        while process.poll() is None:
            if cancelled and cancelled():
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
                raise ExportCancelled("Экспорт отменён.")
            if progress: progress("FFmpeg собирает ролик…")
            import time; time.sleep(.15)
        stderr = process.stderr.read() if process.stderr else ""
        if process.returncode or not temporary.is_file(): raise ExportError(stderr.strip() or "FFmpeg не создал итоговый файл.")
        expected_duration = sum(entry.audio_duration for entry in timeline.entries
                                if any(segment.segment_id == entry.segment_id and segment.has_current_audio
                                       for segment in project.segments))
        actual_duration = probe_duration(temporary)
        if not math.isfinite(actual_duration) or abs(actual_duration - expected_duration) > max(0.25, 2 / _output_fps(project, fps)):
            raise ExportError(
                f"Проверка итогового файла не пройдена: ожидалось {expected_duration:.2f} с, "
                f"получено {actual_duration:.2f} с. Прежний final.mp4 сохранён."
            )
        temporary.replace(destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True); script.unlink(missing_ok=True)
