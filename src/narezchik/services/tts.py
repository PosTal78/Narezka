"""Edge TTS and a retrying, cancellable-at-boundary generation queue."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
import tempfile
import time
import os
from dataclasses import dataclass

from narezchik.models import Project, SegmentStatus, TTSSettings
from .media import probe_duration


@dataclass(frozen=True, slots=True)
class TTSQueueResult:
    target_count: int
    processed_count: int
    cancelled: bool


class EdgeTTSService:
    def list_voices(self) -> list[dict[str, str]]:
        import edge_tts

        voices = asyncio.run(edge_tts.list_voices())
        return [{"name": voice["Name"], "locale": voice["Locale"], "gender": voice.get("Gender", "") } for voice in voices]

    def synthesize(self, text: str, settings: TTSSettings, destination: Path) -> None:
        import edge_tts

        async def save() -> None:
            communicate = edge_tts.Communicate(text, voice=settings.voice, rate=settings.rate, volume=settings.volume, pitch=settings.pitch)
            await communicate.save(str(destination))
        asyncio.run(save())


class TTSQueue:
    def __init__(self, tts: EdgeTTSService, duration_reader: Callable[[Path], float] = probe_duration) -> None:
        self.tts = tts
        self.duration_reader = duration_reader

    def generate(
        self, project: Project, project_root: Path, *, force: bool = False, only_ids: set[int] | None = None,
        cancelled: Callable[[], bool] = lambda: False,
        progress: Callable[[int, int, str], None] = lambda *_: None,
        saved: Callable[[], None] = lambda: None,
    ) -> TTSQueueResult:
        targets = [segment for segment in project.segments if not segment.excluded_from_tts and (only_ids is None or segment.segment_id in only_ids) and (force or segment.status in {SegmentStatus.NEEDS_TTS, SegmentStatus.STALE, SegmentStatus.FILE_MISSING, SegmentStatus.ERROR})]
        audio_dir = project_root / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        processed_count = 0
        was_cancelled = False
        for number, segment in enumerate(targets, 1):
            if cancelled():
                was_cancelled = True
                break
            progress(number, len(targets), segment.text)
            destination = audio_dir / f"{segment.order:03d}.mp3"
            descriptor, temporary_name = tempfile.mkstemp(suffix=".mp3", dir=audio_dir)
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                for attempt in range(3):
                    try:
                        self.tts.synthesize(segment.text, project.tts_settings, temporary)
                        duration = self.duration_reader(temporary)
                        temporary.replace(destination)
                        segment.mark_ready(duration, project.tts_settings)
                        project.invalidate_segment_matches({segment.segment_id})
                        saved()
                        break
                    except Exception as error:
                        if attempt == 2:
                            segment.status = SegmentStatus.ERROR
                            segment.error = str(error) or "Не удалось создать озвучку."
                            project.invalidate_segment_matches({segment.segment_id})
                            saved()
                        else:
                            time.sleep(0.5 * (attempt + 1))
            finally:
                temporary.unlink(missing_ok=True)
            processed_count += 1
        return TTSQueueResult(len(targets), processed_count, was_cancelled)
