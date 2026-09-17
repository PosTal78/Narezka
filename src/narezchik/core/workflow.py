"""Application workflows: project folders, script edits, and persistence."""

from __future__ import annotations

from pathlib import Path
import shutil
from datetime import datetime
import uuid

from narezchik.models import Project, ProjectFormatError, SceneState, SubtitleState, TTSSettings, VideoSource
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

    def replace_video_source(self, project: Project, root: Path, source: VideoSource, *, archive_previous: bool) -> bool:
        """Apply an already inspected source.  Caller confirms replacement first."""
        is_different = project.video_source is not None and project.video_source.fingerprint != source.fingerprint
        previous_source = project.video_source
        previous_subtitles = project.active_subtitles
        previous_scenes = project.scene_analysis
        previous_index = project.video_index
        previous_matches = project.segment_matches
        previous_timeline = project.timeline_state
        archived_analysis: Path | None = None
        try:
            if is_different and previous_source and previous_index:
                from narezchik.services.matching import seed_part_cache
                seed_part_cache(root, previous_source, root / previous_index.index_path)
            project.set_video_source(source)
            # Persist the invalidation first.  If this fails, old files and
            # their project references are both still available.
            self.save(project, root)
            if is_different and archive_previous:
                archived_analysis = self._archive_analysis(root, "source-replaced")
                self._archive_montage_results(root, "source-replaced")
        except Exception:
            project.video_source = previous_source
            project.active_subtitles = previous_subtitles
            project.scene_analysis = previous_scenes
            project.video_index = previous_index
            project.segment_matches = previous_matches
            project.timeline_state = previous_timeline
            if archived_analysis:
                self._restore_analysis_archive(root, archived_analysis)
            # The first save may have already persisted the invalidated state.
            # Restore it before reporting an archival failure to the caller.
            self.save(project, root)
            raise
        return is_different

    def replace_subtitles(self, project: Project, root: Path, state: SubtitleState,
                          staged_subtitles: Path, staged_transcript: Path) -> None:
        """Promote a fully written subtitle pair without losing the active pair on failure."""
        analysis = root / "analysis"
        destination_subtitles = analysis / "subtitles.srt"
        destination_transcript = analysis / "transcript.json"
        previous_state = project.active_subtitles
        previous_index = project.video_index
        previous_matches = project.segment_matches
        promoted: list[Path] = []
        archive: Path | None = None
        try:
            archive = self._archive_files(
                root,
                ["subtitles.srt", "transcript.json"] if project.active_subtitles else [],
                "subtitles-replaced",
            )
            shutil.move(str(staged_subtitles), str(destination_subtitles))
            promoted.append(destination_subtitles)
            shutil.move(str(staged_transcript), str(destination_transcript))
            promoted.append(destination_transcript)
            project.set_active_subtitles(state)
            self.save(project, root)
        except Exception:
            project.active_subtitles = previous_state
            project.video_index = previous_index
            project.segment_matches = previous_matches
            for destination in promoted:
                destination.unlink(missing_ok=True)
            if archive:
                for name in ("subtitles.srt", "transcript.json"):
                    archived = archive / name
                    if archived.exists():
                        shutil.move(str(archived), str(analysis / name))
            raise
        finally:
            staged_subtitles.parent.rmdir() if staged_subtitles.parent.exists() and not any(staged_subtitles.parent.iterdir()) else None

    def activate_scenes(self, project: Project, root: Path, state: SceneState) -> None:
        previous_state = project.scene_analysis
        previous_index = project.video_index
        previous_matches = project.segment_matches
        try:
            project.set_scene_analysis(state)
            self.save(project, root)
        except Exception:
            project.scene_analysis = previous_state
            project.video_index = previous_index
            project.segment_matches = previous_matches
            raise

    @staticmethod
    def _archive_files(root: Path, names: list[str], reason: str) -> Path | None:
        analysis = root / "analysis"
        existing = [analysis / name for name in names if (analysis / name).exists()]
        if not existing:
            return None
        archive = analysis / "retired" / f"{datetime.now():%Y%m%d-%H%M%S}-{reason}-{uuid.uuid4().hex[:8]}"
        archive.mkdir(parents=True, exist_ok=True)
        moved: list[Path] = []
        try:
            for item in existing:
                shutil.move(str(item), str(archive / item.name))
                moved.append(item)
        except Exception:
            for item in moved:
                archived = archive / item.name
                if archived.exists():
                    shutil.move(str(archived), str(item))
            archive.rmdir()
            raise
        return archive

    @classmethod
    def _archive_analysis(cls, root: Path, reason: str) -> Path | None:
        analysis = root / "analysis"
        if not analysis.is_dir():
            return None
        return cls._archive_files(root, [item.name for item in analysis.iterdir()
                                         if item.name not in {"retired", "part-cache"}], reason)

    @staticmethod
    def _restore_analysis_archive(root: Path, archive: Path) -> None:
        analysis = root / "analysis"
        for item in archive.iterdir():
            destination = analysis / item.name
            if destination.exists():
                continue
            shutil.move(str(item), str(destination))
        if archive.exists() and not any(archive.iterdir()):
            archive.rmdir()

    @staticmethod
    def _archive_montage_results(root: Path, reason: str) -> Path | None:
        """Archive the active montage and export together, restoring on failure."""
        sources = [root / "timeline" / "timeline.json", root / "export" / "final.mp4"]
        sources = [path for path in sources if path.is_file()]
        if not sources:
            return None
        archive = root / "timeline" / "retired" / f"{datetime.now():%Y%m%d-%H%M%S}-{reason}-{uuid.uuid4().hex[:8]}"
        archive.mkdir(parents=True, exist_ok=True)
        moved: list[tuple[Path, Path]] = []
        try:
            for source in sources:
                destination = archive / source.name
                shutil.move(str(source), str(destination))
                moved.append((source, destination))
        except Exception:
            for source, destination in reversed(moved):
                if destination.exists():
                    shutil.move(str(destination), str(source))
            if not any(archive.iterdir()):
                archive.rmdir()
            raise
        return archive

    @staticmethod
    def _move_renumbered_audio(root: Path, before: dict[int, str], project: Project) -> None:
        active_ids = {segment.segment_id for segment in project.segments}
        archive_retired_audio_files(root, {
            segment_id: path for segment_id, path in before.items() if segment_id not in active_ids
        })
        changes = {old: segment.audio for segment in project.segments if (old := before.get(segment.segment_id)) and segment.audio}
        rename_audio_files(root, changes)
