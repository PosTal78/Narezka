"""Unit and isolated filesystem tests for the stage-one project contract."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from narezchik.models import Project, ProjectFormatError, SegmentStatus, TTSSettings
from narezchik.services import ProjectStore


class ProjectModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = TTSSettings("ru-RU", "ru-RU-SvetlanaNeural", "+10%", "-5%", "+2Hz")

    def _ready_project(self) -> Project:
        project = Project("Фильм", tts_settings=self.settings)
        first = project.add_segment("Первая реплика.")
        second = project.add_segment("Вторая реплика.")
        first.mark_ready(2.75, self.settings)
        second.mark_ready(3.5, self.settings)
        return project

    def test_round_trip_preserves_unicode_and_full_segment_metadata(self) -> None:
        project = self._ready_project()
        project.segments[1].error = "Предыдущее предупреждение"

        restored = Project.from_dict(project.to_dict())

        self.assertEqual(restored.name, "Фильм")
        self.assertEqual(restored.tts_settings, self.settings)
        self.assertEqual(restored.script_text, "Первая реплика.\nВторая реплика.")
        self.assertEqual(restored.segments[0].audio, "audio/001.mp3")
        self.assertEqual(restored.segments[1].duration, 3.5)
        self.assertEqual(restored.segments[1].error, "Предыдущее предупреждение")

    def test_ids_are_never_reused_and_orders_and_audio_names_are_reindexed(self) -> None:
        project = self._ready_project()
        first_id, second_id = [segment.segment_id for segment in project.segments]

        project.remove_segment(first_id)
        replacement = project.add_segment("Третья реплика.", index=0)

        self.assertEqual(replacement.segment_id, 3)
        self.assertEqual([segment.segment_id for segment in project.segments], [3, second_id])
        self.assertEqual([segment.order for segment in project.segments], [1, 2])
        self.assertEqual(project.segments[1].audio, "audio/002.mp3")

    def test_split_and_merge_keep_one_stable_id_and_invalidate_changed_audio(self) -> None:
        project = self._ready_project()
        first_id = project.segments[0].segment_id

        pieces = project.split_segment(first_id, ["Первая", "реплика."])

        self.assertEqual([piece.segment_id for piece in pieces], [first_id, 3])
        self.assertEqual(pieces[0].status, SegmentStatus.STALE)
        self.assertFalse(pieces[0].can_play)
        self.assertEqual(pieces[0].audio, "audio/001.mp3")
        merged = project.merge_with_next(first_id)
        self.assertEqual(merged.segment_id, first_id)
        self.assertEqual(merged.status, SegmentStatus.STALE)
        self.assertEqual(project.script_text, "Первая\nреплика.\nВторая реплика.")

    def test_edit_or_settings_change_marks_existing_audio_stale_without_deleting_it(self) -> None:
        project = self._ready_project()
        project.edit_segment(project.segments[0].segment_id, "Исправленная реплика.")

        changed = project.segments[0]
        self.assertEqual(changed.status, SegmentStatus.STALE)
        self.assertEqual(changed.audio, "audio/001.mp3")
        self.assertFalse(changed.can_play)

        project.set_tts_settings(TTSSettings(voice="ru-RU-DmitryNeural"))
        self.assertEqual(project.segments[1].status, SegmentStatus.STALE)
        self.assertFalse(project.segments[1].can_play)

    def test_excluded_segment_keeps_and_restores_ready_audio(self) -> None:
        project = self._ready_project()
        segment_id = project.segments[0].segment_id

        project.set_segment_excluded(segment_id, True)
        segment = project.segments[0]
        self.assertEqual(segment.status, SegmentStatus.EXCLUDED)
        self.assertEqual(segment.audio, "audio/001.mp3")
        self.assertFalse(segment.can_play)

        project.set_segment_excluded(segment_id, False)
        self.assertEqual(segment.status, SegmentStatus.READY)
        self.assertTrue(segment.can_play)

    def test_resegmentation_preserves_audio_only_for_normalized_exact_text_and_current_settings(self) -> None:
        project = self._ready_project()
        first_id = project.segments[0].segment_id

        project.replace_segments(["  Первая реплика.\r\n", "Вторая реплика!"])

        preserved, new = project.segments
        self.assertEqual(preserved.segment_id, first_id)
        self.assertEqual(preserved.status, SegmentStatus.READY)
        self.assertTrue(preserved.can_play)
        self.assertNotEqual(new.segment_id, 2)
        self.assertEqual(new.status, SegmentStatus.NEEDS_TTS)

    def test_empty_project_has_no_segments_and_cannot_generate_tts(self) -> None:
        project = Project("Пустой")

        self.assertEqual(project.script_text, "")
        self.assertFalse(project.can_generate_tts)
        self.assertEqual(project.to_dict()["segments"], [])

    def test_rejects_unknown_version_and_absolute_audio_path(self) -> None:
        with self.assertRaisesRegex(ProjectFormatError, "unsupported"):
            Project.from_dict({
                "format_version": 99, "name": "X", "tts_settings": self.settings.to_dict(),
                "next_segment_id": 1, "segments": [],
            })
        with self.assertRaisesRegex(ProjectFormatError, "relative"):
            Project.from_dict({
                "format_version": 1, "name": "X", "tts_settings": self.settings.to_dict(),
                "next_segment_id": 2,
                "segments": [{"segment_id": 1, "order": 1, "text": "X", "excluded_from_tts": False,
                    "status": "ready", "audio": "C:\\outside.mp3", "duration": 1,
                    "tts_settings": self.settings.to_dict(), "error": None, "status_before_exclusion": None}],
            })

    def test_rejects_empty_segment_text(self) -> None:
        with self.assertRaisesRegex(ProjectFormatError, "must not be empty"):
            Project("X").add_segment("   ")

        project = Project("X")
        segment = project.add_segment("Текст")
        with self.assertRaisesRegex(ProjectFormatError, "must not be empty"):
            project.edit_segment(segment.segment_id, "")


class ProjectStoreTests(unittest.TestCase):
    def _project(self) -> Project:
        project = Project("Тест")
        segment = project.add_segment("Реплика")
        segment.mark_ready(1.2, project.tts_settings)
        return project

    def test_save_is_readable_and_keeps_previous_copy_as_backup(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ProjectStore()
            project = self._project()
            store.save(root, project)
            project.name = "Новое имя"
            store.save(root, project)

            self.assertTrue((root / "project.json.bak").is_file())
            self.assertEqual(json.loads((root / "project.json.bak").read_text(encoding="utf-8"))["name"], "Тест")
            self.assertEqual(store.load(root).project.name, "Новое имя")

    def test_missing_audio_is_safe_and_reported_without_changing_file(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ProjectStore()
            store.save(root, self._project())

            result = store.load(root)

            self.assertIsNotNone(result.project)
            self.assertEqual(result.project.segments[0].status, SegmentStatus.FILE_MISSING)
            self.assertFalse(result.project.segments[0].can_play)
            self.assertTrue((root / "project.json").is_file())

    def test_corrupt_primary_requires_explicit_backup_recovery(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ProjectStore()
            initial = self._project()
            store.save(root, initial)
            initial.name = "Последняя версия"
            store.save(root, initial)
            (root / "project.json").write_text("{ broken", encoding="utf-8")

            result = store.load(root)

            self.assertIsNone(result.project)
            self.assertTrue(result.recovery_available)
            self.assertIn("повреждён", result.issue)
            self.assertEqual((root / "project.json").read_text(encoding="utf-8"), "{ broken")

            recovered = store.load(root, use_backup=True)
            self.assertTrue(recovered.recovered_from_backup)
            self.assertEqual(recovered.project.name, "Тест")
            self.assertEqual(store.load(root).project.name, "Тест")
            self.assertEqual(len(list(root.glob("project.json.corrupt-*"))), 1)

    def test_unrecoverable_corrupt_project_returns_an_issue(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "project.json").write_text("not json", encoding="utf-8")

            result = ProjectStore().load(root)

            self.assertIsNone(result.project)
            self.assertFalse(result.recovered_from_backup)
            self.assertIn("Не удалось", result.issue)

    def test_missing_primary_can_be_recovered_from_a_backup(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ProjectStore()
            store.save(root, self._project())
            (root / "project.json.bak").write_text((root / "project.json").read_text(encoding="utf-8"), encoding="utf-8")
            (root / "project.json").unlink()

            recovered = store.load(root, use_backup=True)

            self.assertTrue(recovered.recovered_from_backup)
            self.assertEqual(store.load(root).project.name, "Тест")
