"""Smoke tests for the initial repository layout."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ProjectLayoutTests(unittest.TestCase):
    def test_required_documentation_exists(self) -> None:
        for relative_path in (
            "AGENTS.md",
            "CHECKLIST.md",
            "PRD.md",
            "TASKS.md",
            "docs/ARCHITECTURE.md",
            "docs/PROJECT_FORMAT.md",
            "docs/TTS.md",
            "docs/VIDEO_PIPELINE.md",
            "docs/TESTING.md",
        ):
            self.assertTrue((ROOT / relative_path).is_file(), relative_path)

    def test_application_package_has_expected_boundaries(self) -> None:
        for package in ("ui", "core", "services", "models"):
            self.assertTrue((ROOT / "src" / "narezchik" / package / "__init__.py").is_file())
