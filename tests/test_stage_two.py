"""Stage-two rules use small files and doubles, never real video or models."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch
from unittest.mock import patch

from narezchik.core import ProjectWorkflow
from narezchik.models import Project, ProjectFormatError, SceneState, SubtitleState, VideoSource
from narezchik.main import MainWindow
from narezchik.services.subtitles import SubtitleCue
from narezchik.services.analysis import AnalysisCancelled, AnalysisError, Scene, detect_scenes, transcribe, write_scenes
from narezchik.services.subtitles import parse_subtitles, read_subtitles, write_canonical
from narezchik.services.video import VideoError, VideoMetadata, ensure_current_source, inspect_video


FINGERPRINT_A = "a" * 64
FINGERPRINT_B = "b" * 64


def source(path: Path, fingerprint: str = FINGERPRINT_A) -> VideoSource:
    return VideoSource(str(path.resolve()), fingerprint, path.name, 120.0, 1920, 1080, 24.0)


class VideoStateTests(unittest.TestCase):
    def test_ordered_video_parts_round_trip_as_one_source(self) -> None:
        first = VideoSource(str(Path("C:/part-1.mp4")), "a" * 64, "part-1.mp4", 10, 1920, 1080, 24)
        second = VideoSource(str(Path("C:/part-2.mp4")), "b" * 64, "part-2.mp4", 20, 1280, 720, 30)
        source = VideoSource.combine([first, second])
        project = Project("Составной", video_source=source)
        restored = Project.from_dict(project.to_dict()).video_source
        self.assertEqual([part.file_name for part in restored.source_parts], ["part-1.mp4", "part-2.mp4"])
        self.assertEqual(restored.duration, 30)
        self.assertEqual(restored.locate(12)[0:], (1, restored.source_parts[1], 2))
        self.assertNotEqual(source.fingerprint, VideoSource.combine([second, first]).fingerprint)
    def test_composite_transcription_offsets_each_part(self) -> None:
        first=VideoSource(str(Path('C:/one.mp4')),'a'*64,'one.mp4',10,100,100,24)
        second=VideoSource(str(Path('C:/two.mp4')),'b'*64,'two.mp4',20,100,100,24)
        source=VideoSource.combine([first,second]);fake=object();reports=[]
        with patch('narezchik.main.transcribe',side_effect=[([SubtitleCue(1,2,'one')],'ru'),([SubtitleCue(1,2,'two')],'ru')]):
            cues,language=MainWindow.transcribe_source(fake,source,'small','ru',lambda:False,lambda *value:reports.append(value))
        self.assertEqual([(cue.start,cue.end) for cue in cues],[(1,2),(11,12)])
        self.assertEqual(language,'ru');self.assertTrue(reports)
    def test_missing_active_analysis_is_not_restored_from_backup_as_current(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary); flow = ProjectWorkflow(); project, created = flow.create(root, "Тест")
            film = root / "film.mp4"; film.write_bytes(b"film")
            flow.replace_video_source(project, created, source(film), archive_previous=False)
            project.set_active_subtitles(SubtitleState("analysis/subtitles.srt", "analysis/transcript.json", FINGERPRINT_A, "import"))
            flow.save(project, created)
            reopened = flow.open(created)
            self.assertIsNone(reopened.active_subtitles)

    def test_stage_one_project_migrates_to_current_format_on_save(self) -> None:
        original = Project("Старый")
        stage_one = original.to_dict()
        stage_one["format_version"] = 1
        stage_one.pop("video_source")
        stage_one.pop("active_subtitles")
        stage_one.pop("scene_analysis")
        migrated = Project.from_dict(stage_one)
        self.assertEqual(migrated.format_version, 5)
        self.assertIsNone(migrated.video_source)

    def test_rejects_malformed_video_state_instead_of_crashing_on_project_open(self) -> None:
        with self.assertRaises(ProjectFormatError):
            VideoSource("C:/film.mp4", 123, "film.mp4", "unknown", 1920, 1080, 24)
        with self.assertRaises(ProjectFormatError):
            SubtitleState.from_dict({"subtitles_path": 3, "transcript_path": "analysis/transcript.json",
                                     "source_fingerprint": FINGERPRINT_A, "kind": "import"})
    def test_changed_source_invalidates_active_results_but_same_fingerprint_keeps_them(self) -> None:
        with TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.mp4"; second = Path(temporary) / "second.mp4"
            project = Project("Видео")
            project.set_video_source(source(first))
            project.set_active_subtitles(SubtitleState("analysis/subtitles.srt", "analysis/transcript.json", FINGERPRINT_A, "import"))
            project.set_scene_analysis(SceneState("analysis/scenes.json", FINGERPRINT_A))

            self.assertFalse(project.set_video_source(source(second, FINGERPRINT_A)))
            self.assertIsNotNone(project.active_subtitles)
            self.assertTrue(project.set_video_source(source(second, FINGERPRINT_B)))
            self.assertIsNone(project.active_subtitles)
            self.assertIsNone(project.scene_analysis)

    def test_replacing_a_source_archives_active_analysis(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary); flow = ProjectWorkflow(); project, created = flow.create(root, "Тест")
            one = root / "one.mp4"; two = root / "two.mp4"; one.write_bytes(b"one"); two.write_bytes(b"two")
            flow.replace_video_source(project, created, source(one), archive_previous=False)
            (created / "analysis" / "subtitles.srt").write_text("old", encoding="utf-8")
            (created / "analysis" / "transcript.json").write_text("{}", encoding="utf-8")
            project.set_active_subtitles(SubtitleState("analysis/subtitles.srt", "analysis/transcript.json", FINGERPRINT_A, "import"))
            flow.replace_video_source(project, created, source(two, FINGERPRINT_B), archive_previous=True)

            self.assertIsNone(project.active_subtitles)
            self.assertTrue(list((created / "analysis" / "retired").rglob("subtitles.srt")))

    def test_failed_subtitle_replacement_restores_the_active_pair(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary); flow = ProjectWorkflow(); project, created = flow.create(root, "Тест")
            video = root / "film.mp4"; video.write_bytes(b"film")
            flow.replace_video_source(project, created, source(video), archive_previous=False)
            analysis = created / "analysis"
            (analysis / "subtitles.srt").write_text("old subtitles", encoding="utf-8")
            (analysis / "transcript.json").write_text('{"old": true}', encoding="utf-8")
            old_state = SubtitleState("analysis/subtitles.srt", "analysis/transcript.json", FINGERPRINT_A, "import")
            project.set_active_subtitles(old_state)
            staging = analysis / ".staging"; staging.mkdir()
            staged_srt = staging / "subtitles.srt"; staged_json = staging / "transcript.json"
            staged_srt.write_text("new subtitles", encoding="utf-8"); staged_json.write_text("{}", encoding="utf-8")
            with patch.object(flow, "save", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    flow.replace_subtitles(project, created, old_state, staged_srt, staged_json)
            self.assertEqual((analysis / "subtitles.srt").read_text(encoding="utf-8"), "old subtitles")
            self.assertEqual((analysis / "transcript.json").read_text(encoding="utf-8"), '{"old": true}')
            self.assertEqual(project.active_subtitles, old_state)

    def test_failed_source_save_keeps_old_analysis_and_project_state(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary); flow = ProjectWorkflow(); project, created = flow.create(root, "Тест")
            one = root / "one.mp4"; two = root / "two.mp4"; one.write_bytes(b"one"); two.write_bytes(b"two")
            flow.replace_video_source(project, created, source(one), archive_previous=False)
            (created / "analysis" / "scenes.json").write_text("old scenes", encoding="utf-8")
            old_state = SceneState("analysis/scenes.json", FINGERPRINT_A)
            project.set_scene_analysis(old_state)
            with patch.object(flow, "save", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    flow.replace_video_source(project, created, source(two, FINGERPRINT_B), archive_previous=True)
            self.assertEqual(project.video_source, source(one))
            self.assertEqual(project.scene_analysis, old_state)
            self.assertEqual((created / "analysis" / "scenes.json").read_text(encoding="utf-8"), "old scenes")


class SubtitleTests(unittest.TestCase):
    def test_windows_1251_subtitles_are_supported(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "film.srt"
            path.write_bytes("1\n00:00:00,000 --> 00:00:01,000\nПривет\n".encode("cp1251"))
            self.assertEqual(read_subtitles(path)[0].text, "Привет")
    def test_srt_and_vtt_are_normalized(self) -> None:
        srt = parse_subtitles("1\n00:00:01,250 --> 00:00:03,000\n<i>Привет</i>\n", ".srt")
        vtt = parse_subtitles("WEBVTT\n\nintro\n00:04.000 --> 00:05.500 align:start\nHello\n", ".vtt")
        self.assertEqual(srt[0].text, "Привет")
        self.assertEqual(vtt[0].start, 4.0)
        with TemporaryDirectory() as temporary:
            srt_path, transcript = write_canonical(srt, Path(temporary))
            self.assertIn("00:00:01,250", srt_path.read_text(encoding="utf-8"))
            self.assertIn('"cues"', transcript.read_text(encoding="utf-8"))


class VideoServiceTests(unittest.TestCase):
    def test_changed_source_contents_are_detected_before_analysis(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "film.mp4"; path.write_bytes(b"old")
            fingerprint = hashlib.sha256(b"old").hexdigest()
            path.write_bytes(b"new")
            with self.assertRaises(VideoError):
                ensure_current_source(path, fingerprint)
    def test_inspection_uses_metadata_double_and_content_hash(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "film.mp4"; path.write_bytes(b"film")
            with patch("narezchik.services.video.probe_video", return_value=VideoMetadata(3.5, 640, 360, 30.0)):
                result = inspect_video(path)
            self.assertEqual(result.duration, 3.5)
            self.assertEqual(result.fingerprint, hashlib.sha256(b"film").hexdigest())


class AnalysisStorageTests(unittest.TestCase):
    def test_scene_detection_honours_cancel_before_opening_video(self) -> None:
        with self.assertRaises(AnalysisCancelled):
            detect_scenes(Path("missing.mp4"), 1.0, cancelled=lambda: True)

    def test_scene_write_replaces_a_complete_file_atomically(self) -> None:
        with TemporaryDirectory() as temporary:
            analysis = Path(temporary); destination = analysis / "scenes.json"
            destination.write_text("old scenes", encoding="utf-8")
            with patch("narezchik.services.analysis.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    write_scenes([Scene(1, 0, 2)], analysis, FINGERPRINT_A)
            self.assertEqual(destination.read_text(encoding="utf-8"), "old scenes")


class WhisperFallbackTests(unittest.TestCase):
    def _run_transcription(self, factory, cuda_available=True):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = SimpleNamespace(whisper_models=root / "models")
            with patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=factory)}), \
                 patch("narezchik.services.analysis.app_paths", return_value=paths), \
                 patch("narezchik.services.analysis._cuda_runtime_available", return_value=cuda_available):
                return transcribe(root / "film.mp4")

    def test_cuda_load_failure_retries_once_on_cpu_with_int8(self) -> None:
        calls = []

        def factory(*args, **kwargs):
            calls.append((args, kwargs))
            if kwargs["device"] == "cuda":
                raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
            return SimpleNamespace(transcribe=lambda *_args, **_kwargs: ([SimpleNamespace(start=0, end=1, text="Текст")], SimpleNamespace(language="ru")))

        cues, language = self._run_transcription(factory)

        self.assertEqual([(cue.start, cue.end, cue.text) for cue in cues], [(0.0, 1.0, "Текст")])
        self.assertEqual(language, "ru")
        self.assertEqual([call[1]["device"] for call in calls], ["cuda", "cpu"])
        self.assertTrue(all(call[1]["compute_type"] == "int8" for call in calls))

    def test_successful_cuda_start_reports_the_actual_device(self) -> None:
        reports = []

        def factory(*_args, **_kwargs):
            return SimpleNamespace(transcribe=lambda *_args, **_kwargs: ([SimpleNamespace(start=0, end=1, text="Текст")], SimpleNamespace(language="ru")))

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = SimpleNamespace(whisper_models=root / "models")
            fake_torch = SimpleNamespace(cuda=SimpleNamespace(get_device_name=lambda _index: "NVIDIA GeForce GTX 1660"))
            with patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=factory), "torch": fake_torch}), \
                 patch("narezchik.services.analysis.app_paths", return_value=paths), \
                 patch("narezchik.services.analysis._cuda_runtime_available", return_value=True):
                transcribe(root / "film.mp4", progress=lambda *_value: reports.append(_value))

        self.assertTrue(any("NVIDIA GeForce GTX 1660" in report[2] for report in reports))

    def test_non_cuda_model_error_is_not_retried_as_cpu(self) -> None:
        calls = []

        def factory(*args, **kwargs):
            calls.append((args, kwargs))
            raise RuntimeError("model files are corrupted")

        with self.assertRaisesRegex(AnalysisError, "model files are corrupted"):
            self._run_transcription(factory)
        self.assertEqual([call[1]["device"] for call in calls], ["cuda"])
