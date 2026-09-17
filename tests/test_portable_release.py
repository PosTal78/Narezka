"""Portable-release paths must never fall back to a Windows profile."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from narezchik.app_paths import app_paths
from narezchik.services.media import media_tool


class PortablePathTests(unittest.TestCase):
    def test_explicit_portable_root_owns_every_data_directory(self) -> None:
        with TemporaryDirectory() as temporary, patch.dict(os.environ, {"NAREZCHIK_HOME": temporary}):
            paths = app_paths()
            paths.ensure_data_directories()

            self.assertEqual(paths.data, Path(temporary) / "data")
            self.assertTrue(paths.projects.is_dir())
            self.assertTrue(paths.whisper_models.is_dir())

    def test_bundled_ffprobe_beats_path(self) -> None:
        with TemporaryDirectory() as temporary, patch.dict(os.environ, {"NAREZCHIK_HOME": temporary}):
            executable = Path(temporary) / "tools" / "ffmpeg" / "ffprobe.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"portable")
            with patch("narezchik.services.media.shutil.which", return_value="C:/other/ffprobe.exe"):
                self.assertEqual(media_tool("ffprobe"), str(executable))

    def test_build_script_keeps_user_data_outside_pyinstaller_destination(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "scripts" / "build_portable.ps1").read_text(encoding="utf-8")

        self.assertIn("--distpath $stageRoot", script)
        self.assertIn("$portable\\data contains user projects", script)
        self.assertNotIn("--distpath $portable", script)

    def test_build_script_collects_whisper_assets_and_verifies_them(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "scripts" / "build_portable.ps1").read_text(encoding="utf-8")

        self.assertIn("--collect-all faster_whisper", script)
        self.assertIn("--collect-all sentencepiece", script)
        self.assertIn("_internal\\sentencepiece\\_sentencepiece.cp312-win_amd64.pyd", script)
        self.assertIn("--collect-all ctranslate2", script)
        self.assertIn("_internal\\faster_whisper\\assets\\silero_vad_v6.onnx", script)
        self.assertIn("_internal\\torch\\lib\\cudnn64_9.dll", script)
