"""Safe on-disk persistence for local Narezchik projects."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path

from narezchik.models.project import Project, ProjectFormatError


PROJECT_FILENAME = "project.json"
BACKUP_FILENAME = "project.json.bak"


@dataclass(frozen=True, slots=True)
class ProjectLoadResult:
    project: Project | None
    issue: str | None = None
    recovered_from_backup: bool = False
    recovery_available: bool = False


class ProjectStore:
    def load(self, project_root: Path, *, use_backup: bool = False) -> ProjectLoadResult:
        project_path = project_root / PROJECT_FILENAME
        try:
            project = self._read(project_path)
        except (OSError, json.JSONDecodeError, ProjectFormatError) as error:
            try:
                project = self._read(project_root / BACKUP_FILENAME)
            except (OSError, json.JSONDecodeError, ProjectFormatError):
                return ProjectLoadResult(None, f"Не удалось открыть project.json: {error}")
            if not use_backup:
                return ProjectLoadResult(
                    None,
                    f"Основной project.json повреждён: {error}. Доступна резервная копия.",
                    recovery_available=True,
                )
            corrupt_copy = None
            if project_path.is_file():
                corrupt_copy = project_root / f"project.json.corrupt-{datetime.now():%Y%m%d-%H%M%S}"
                shutil.copy2(project_path, corrupt_copy)
            project.reconcile_audio_files(project_root)
            project.reconcile_analysis_files(project_root)
            self.save(project_root, project)
            return ProjectLoadResult(
                project,
                "Основной project.json повреждён; восстановлена резервная копия."
                + (f" Повреждённый файл сохранён как {corrupt_copy.name}." if corrupt_copy else ""),
                True,
            )
        project.reconcile_audio_files(project_root)
        if project.reconcile_analysis_files(project_root):
            self.save(project_root, project)
        return ProjectLoadResult(project)

    def save(self, project_root: Path, project: Project) -> None:
        project_root.mkdir(parents=True, exist_ok=True)
        target = project_root / PROJECT_FILENAME
        payload = json.dumps(project.to_dict(), ensure_ascii=False, indent=2) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(prefix=".project-", suffix=".tmp", dir=project_root, text=True)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if target.exists():
                # A repair save must never replace a known-good backup with a
                # damaged primary file before the user has a recoverable copy.
                try:
                    self._read(target)
                except (OSError, json.JSONDecodeError, ProjectFormatError):
                    pass
                else:
                    shutil.copy2(target, project_root / BACKUP_FILENAME)
            os.replace(temporary, target)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _read(path: Path) -> Project:
        with path.open("r", encoding="utf-8") as stream:
            return Project.from_dict(json.load(stream))
