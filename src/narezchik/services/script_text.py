"""TXT import and deterministic script segmentation."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
import re


class SegmentationMode(StrEnum):
    LINES = "lines"
    SENTENCES = "sentences"
    PARAGRAPHS = "paragraphs"


_ABBREVIATIONS = {"г.", "ул.", "д.", "стр.", "т.", "т. д.", "т. п.", "им.", "рис.", "см.", "др."}


def read_text_file(path: Path, encoding: str | None = None) -> tuple[str, str]:
    """Read a user TXT using a small, transparent Windows-friendly fallback set."""
    candidates = [encoding] if encoding else ["utf-8-sig", "utf-8", "cp1251", "cp866"]
    for candidate in candidates:
        try:
            return path.read_text(encoding=candidate), candidate
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("text", b"", 0, 1, "Не удалось определить кодировку TXT")


def segment_text(text: str, mode: SegmentationMode) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if mode is SegmentationMode.LINES:
        return [line.strip() for line in normalized.split("\n") if line.strip()]
    if mode is SegmentationMode.PARAGRAPHS:
        return [part.strip() for part in re.split(r"\n[ \t]*\n+", normalized) if part.strip()]
    return _sentences(normalized)


def _sentences(text: str) -> list[str]:
    result: list[str] = []
    start = 0
    for match in re.finditer(r"[.!?]+(?:[»\"']+)?(?=\s+|$)", text):
        candidate = text[start:match.end()].strip()
        if candidate.lower() in _ABBREVIATIONS or re.search(r"\b[А-ЯA-Z]\.$", candidate):
            continue
        if candidate:
            result.append(candidate)
        start = match.end()
    tail = text[start:].strip()
    if tail:
        result.append(tail)
    return result
