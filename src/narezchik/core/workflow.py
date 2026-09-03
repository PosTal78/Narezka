"""Application workflows: project folders, script edits, and persistence."""

from __future__ import annotations

from pathlib import Path
import shutil

from narezchik.models import Project, ProjectFormatError, TTSSettings
from narezchik.services.media import archive_retired_audio_files, rename_audio_files
from narezchik.services.project_store import ProjectStore
from narezchik.services.script_text import SegmentationMode, segment_text


class WorkflowError(ValueError):
    pass


class ProjectWorkflow:
    def __init__(self, store: ProjectStore | None = None) -> None:
        self.store = store or ProjectStore()
        self.recovery_available = False

    def create(self, parent: Path, name: str) -> tuple[Project, Path]:
        clean_name = name.strip()
        if not clean_name or any(char in clean_name for char in '<>:"/\\|?*'):
            raise WorkflowError("Введите название без символов \\ / : * ? \" < > |.")
        root = parent / clean_name
        if root.exists():
            raise WorkflowError("Папка проекта уже существует. Выберите другое название.")
        root.mkdir()
        for folder in ("script", "audio", "source", "analysis", "clips", "timeline", "export"):
            (root / folder).mkdir()
        project = Project(clean_name)
        self.store.save(root, project)
        return project, root

    def open(self, root: Path, *, recover_from_backup: bool = False) -> Project:
        result = self.store.load(root, use_backup=recover_from_backup)
        self.recovery_available = result.recovery_available
        if not result.project:
            raise WorkflowError(result.issue or "Не удалось открыть проект.")
        return result.project

    def save(self, project: Project, root: Path) -> None:
        self.store.save(root, project)

    def replace_script(self, project: Project, root: Path, text: str, mode: SegmentationMode) -> None:
        before = {segment.segment_id: segment.audio for segment in project.segments if segment.audio}
        project.replace_segments(segment_text(text, mode))
        self._move_renumbered_audio(root, before, project)
        (root / "script" / "script.txt").write_text(project.script_text, encoding="utf-8")
        self.save(project, root)

    def clear_script(self, project: Project, root: Path) -> None:
        """Remove all script segments while preserving their audio as retired files."""
        before = {segment.segment_id: segment.audio for segment in project.segments if segment.audio}
        project.replace_segments([])
        self.after_segment_change(project, root, before)

    def after_segment_change(self, project: Project, root: Path, before: dict[int, str]) -> None:
        self._move_renumbered_audio(root, before, project)
        (root / "script" / "script.txt").write_text(project.script_text, encoding="utf-8")
        self.save(project, root)

    @staticmethod
    def _move_renumbered_audio(root: Path, before: dict[int, str], project: Project) -> None:
        active_ids = {segment.segment_id for segment in project.segments}
        archive_retired_audio_files(root, {
            segment_id: path for segment_id, path in before.items() if segment_id not in active_ids
        })
        changes = {old: segment.audio for segment in project.segments if (old := before.get(segment.segment_id)) and segment.audio}
        rename_audio_files(root, changes)
