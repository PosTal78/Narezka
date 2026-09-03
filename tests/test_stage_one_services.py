from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from narezchik.core import ProjectWorkflow, WorkflowError
from narezchik.models import Project, SegmentStatus
from narezchik.services.script_text import SegmentationMode, segment_text
from narezchik.services.tts import TTSQueue
from narezchik.services.media import MediaError, probe_duration
from narezchik.main import MainWindow


class SegmentationTests(unittest.TestCase):
    def test_lines_paragraphs_and_conservative_sentences(self):
        self.assertEqual(segment_text("A\n\nB", SegmentationMode.LINES), ["A", "B"])
        self.assertEqual(segment_text("A\n\nB", SegmentationMode.PARAGRAPHS), ["A", "B"])
        self.assertEqual(segment_text("г. Москва. Готово!", SegmentationMode.SENTENCES), ["г. Москва.", "Готово!"])


class WorkflowTests(unittest.TestCase):
    def test_new_project_must_not_reuse_existing_folder(self):
        with TemporaryDirectory() as temp:
            flow = ProjectWorkflow(); project, root = flow.create(Path(temp), "Новый")
            self.assertTrue((root / "audio").is_dir())
            with self.assertRaises(WorkflowError): flow.create(Path(temp), "Новый")

    def test_resegmentation_renames_existing_audio(self):
        with TemporaryDirectory() as temp:
            flow = ProjectWorkflow(); project, root = flow.create(Path(temp), "Новый")
            project.add_segment("Первый").mark_ready(1, project.tts_settings)
            project.add_segment("Второй").mark_ready(1, project.tts_settings)
            (root / "audio" / "001.mp3").write_bytes(b"one")
            (root / "audio" / "002.mp3").write_bytes(b"two")
            flow.replace_script(project, root, "Второй", SegmentationMode.LINES)
            self.assertEqual((root / "audio" / "001.mp3").read_bytes(), b"two")

    def test_removing_segment_archives_its_audio_before_renumbering(self):
        with TemporaryDirectory() as temp:
            flow = ProjectWorkflow(); project, root = flow.create(Path(temp), "Новый")
            first = project.add_segment("Первый")
            second = project.add_segment("Второй")
            first.mark_ready(1, project.tts_settings)
            second.mark_ready(1, project.tts_settings)
            (root / "audio" / "001.mp3").write_bytes(b"one")
            (root / "audio" / "002.mp3").write_bytes(b"two")

            before = {segment.segment_id: segment.audio for segment in project.segments if segment.audio}
            project.remove_segment(first.segment_id)
            flow.after_segment_change(project, root, before)

            self.assertEqual((root / "audio" / "001.mp3").read_bytes(), b"two")
            self.assertEqual((root / "audio" / "retired" / "1-001.mp3").read_bytes(), b"one")

    def test_clearing_script_archives_all_audio_before_new_segments_can_use_numbers(self):
        with TemporaryDirectory() as temp:
            flow = ProjectWorkflow(); project, root = flow.create(Path(temp), "Новый")
            first = project.add_segment("Первый")
            second = project.add_segment("Второй")
            first.mark_ready(1, project.tts_settings)
            second.mark_ready(1, project.tts_settings)
            (root / "audio" / "001.mp3").write_bytes(b"one")
            (root / "audio" / "002.mp3").write_bytes(b"two")

            flow.clear_script(project, root)

            self.assertEqual(project.segments, [])
            self.assertFalse((root / "audio" / "001.mp3").exists())
            self.assertFalse((root / "audio" / "002.mp3").exists())
            self.assertEqual((root / "audio" / "retired" / "1-001.mp3").read_bytes(), b"one")
            self.assertEqual((root / "audio" / "retired" / "2-002.mp3").read_bytes(), b"two")

    def test_open_requires_explicit_backup_recovery(self):
        with TemporaryDirectory() as temp:
            flow = ProjectWorkflow(); project, root = flow.create(Path(temp), "Новый")
            flow.save(project, root)
            (root / "project.json").write_text("{ broken", encoding="utf-8")

            with self.assertRaises(WorkflowError):
                flow.open(root)
            self.assertTrue(flow.recovery_available)
            self.assertEqual(flow.open(root, recover_from_backup=True).name, "Новый")


class FakeTTS:
    def __init__(self): self.calls = []
    def synthesize(self, text, settings, destination):
        self.calls.append(text)
        if text == "Ошибка": raise RuntimeError("сбой")
        destination.write_bytes(b"mp3")


class QueueTests(unittest.TestCase):
    def test_queue_continues_after_error_and_retries_twice(self):
        with TemporaryDirectory() as temp:
            project = Project("Тест"); project.add_segment("Ошибка"); project.add_segment("Готово")
            fake = FakeTTS(); saved = []
            TTSQueue(fake, lambda _: 1.5).generate(project, Path(temp), saved=lambda: saved.append(True))
            self.assertEqual(fake.calls.count("Ошибка"), 3)
            self.assertEqual(project.segments[0].status, SegmentStatus.ERROR)
            self.assertEqual(project.segments[1].status, SegmentStatus.READY)
            self.assertTrue((Path(temp) / "audio" / "002.mp3").is_file())

    def test_queue_regenerates_only_requested_segment(self):
        with TemporaryDirectory() as temp:
            project = Project("Тест")
            first = project.add_segment("Первый")
            second = project.add_segment("Второй")
            fake = FakeTTS()

            TTSQueue(fake, lambda _: 1.5).generate(project, Path(temp), only_ids={second.segment_id})

            self.assertEqual(fake.calls, ["Второй"])
            self.assertEqual(first.status, SegmentStatus.NEEDS_TTS)
            self.assertEqual(second.status, SegmentStatus.READY)

    def test_queue_reports_unprocessed_segments_after_cancellation(self):
        with TemporaryDirectory() as temp:
            project = Project("Тест")
            project.add_segment("Первый")
            project.add_segment("Второй")
            fake = FakeTTS()

            result = TTSQueue(fake, lambda _: 1.5).generate(
                project, Path(temp), cancelled=lambda: len(fake.calls) == 1,
            )

            self.assertTrue(result.cancelled)
            self.assertEqual(result.target_count, 2)
            self.assertEqual(result.processed_count, 1)
            self.assertEqual(project.segments[1].status, SegmentStatus.NEEDS_TTS)


class MediaTests(unittest.TestCase):
    def test_missing_ffprobe_has_actionable_error(self):
        with patch("narezchik.services.media.subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(MediaError, "ffprobe не найден"):
                probe_duration(Path("audio.mp3"))


class WindowRegressionTests(unittest.TestCase):
    def test_new_does_not_create_a_second_project_after_saving_a_draft(self):
        class DraftWindow:
            worker = None
            project = None

            def protect_draft(self):
                self.project = Project("Сохранённый черновик")
                return True

        window = DraftWindow()
        with patch("narezchik.main.QFileDialog.getExistingDirectory") as choose_folder:
            MainWindow.new(window)

        self.assertEqual(window.project.name, "Сохранённый черновик")
        choose_folder.assert_not_called()

    def test_editing_a_segment_to_blank_text_keeps_it_unchanged(self):
        project = Project("Тест")
        segment = project.add_segment("Исходный текст")

        class EditWindow:
            def __init__(self, project): self.project = project
            def selected(self): return segment
            def changed(self, _before): self.changed_called = True

        window = EditWindow(project)
        with patch("narezchik.main.QInputDialog.getMultiLineText", return_value=("   ", True)), patch("narezchik.main.QMessageBox.warning") as warning:
            MainWindow.edit(window)

        self.assertEqual(segment.text, "Исходный текст")
        self.assertFalse(hasattr(window, "changed_called"))
        warning.assert_called_once()
