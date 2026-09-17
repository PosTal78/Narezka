"""Parsing and canonical storage for external and locally generated subtitles."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from html import unescape
import json
from pathlib import Path
import re
from typing import Iterable


class SubtitleError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SubtitleCue:
    start: float
    end: float
    text: str

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start or not self.text.strip():
            raise SubtitleError("Некорректный таймкод или пустая реплика в субтитрах.")


_TIMECODE = re.compile(r"^(?:(?P<h>\d{1,2}):)?(?P<m>[0-5]\d):(?P<s>[0-5]\d)[,.](?P<ms>\d{1,3})$")


def parse_time(value: str) -> float:
    match = _TIMECODE.match(value.strip())
    if not match:
        raise SubtitleError(f"Некорректный таймкод: {value}")
    parts = match.groupdict()
    return int(parts["h"] or 0) * 3600 + int(parts["m"]) * 60 + int(parts["s"]) + int(parts["ms"].ljust(3, "0")) / 1000


def _clean_text(lines: Iterable[str]) -> str:
    text = "\n".join(line.strip() for line in lines).strip()
    return unescape(re.sub(r"<[^>]+>", "", text))


def parse_subtitles(text: str, suffix: str) -> list[SubtitleCue]:
    if suffix.lower() not in {".srt", ".vtt"}:
        raise SubtitleError("Поддерживаются только субтитры SRT и VTT.")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if suffix.lower() == ".vtt" and lines and lines[0].lstrip("\ufeff").startswith("WEBVTT"):
        lines = lines[1:]
    cues: list[SubtitleCue] = []
    index = 0
    while index < len(lines):
        while index < len(lines) and not lines[index].strip():
            index += 1
        if index >= len(lines):
            break
        if "-->" not in lines[index]:
            index += 1  # SRT ordinal or a VTT cue identifier
        if index >= len(lines) or "-->" not in lines[index]:
            continue
        start_text, end_text = (part.strip().split()[0] for part in lines[index].split("-->", 1))
        index += 1
        content: list[str] = []
        while index < len(lines) and lines[index].strip():
            content.append(lines[index])
            index += 1
        cues.append(SubtitleCue(parse_time(start_text), parse_time(end_text), _clean_text(content)))
    if not cues:
        raise SubtitleError("В файле не найдено ни одной реплики.")
    return cues


def read_subtitles(path: Path) -> list[SubtitleCue]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise SubtitleError(f"Не удалось прочитать субтитры: {error}") from error
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return parse_subtitles(raw.decode(encoding), path.suffix)
        except UnicodeDecodeError:
            continue
    raise SubtitleError("Не удалось определить кодировку субтитров. Сохраните файл в UTF-8 или Windows-1251.")


def _format_time(value: float) -> str:
    milliseconds = round(value * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def write_canonical(cues: Iterable[SubtitleCue], analysis_dir: Path, *, language: str | None = None,
                    kind: str = "import", model: str | None = None) -> tuple[Path, Path]:
    values = list(cues)
    if not values:
        raise SubtitleError("Нельзя сохранить пустые субтитры.")
    analysis_dir.mkdir(parents=True, exist_ok=True)
    srt = analysis_dir / "subtitles.srt"
    transcript = analysis_dir / "transcript.json"
    srt.write_text("\n\n".join(f"{index}\n{_format_time(cue.start)} --> {_format_time(cue.end)}\n{cue.text}"
                                for index, cue in enumerate(values, 1)) + "\n", encoding="utf-8")
    transcript.write_text(json.dumps({"kind": kind, "language": language, "model": model,
                                      "cues": [asdict(cue) for cue in values]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return srt, transcript
