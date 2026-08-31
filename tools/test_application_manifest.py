from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import aerost_tool as tool

ROOT = Path(__file__).resolve().parents[1]


class ApplicationManifestTests(unittest.TestCase):
    def test_default_manifest_resolves_controlled_paths(self) -> None:
        application = tool.load_application_manifest(ROOT)
        self.assertEqual(application.application_id, "power-supervisor")
        self.assertEqual(application.profile_version, tool.PROFILE_VERSION)
        self.assertEqual(application.air_artifact_name, "power-supervisor.air.json")
        self.assertTrue(application.mutation_profile_path.is_file())
        self.assertEqual(len(application.controlled_scenario_paths), 2)
        self.assertTrue(all(path.is_file() for path in application.controlled_scenario_paths))

    def test_default_manifest_validates_against_schema(self) -> None:
        manifest = json.loads(
            (ROOT / "applications/power-supervisor.application.json").read_text(encoding="utf-8")
        )
        schema = json.loads(
            (ROOT / "schemas/application-manifest.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(tool.validate_json_schema(manifest, schema), [])

    def test_manifest_rejects_repository_escape(self) -> None:
        original = json.loads(
            (ROOT / "applications/power-supervisor.application.json").read_text(encoding="utf-8")
        )
        modified = copy.deepcopy(original)
        modified["source"] = "../outside.ascp"
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", dir=ROOT / "applications", delete=False, encoding="utf-8"
        ) as stream:
            json.dump(modified, stream)
            path = Path(stream.name)
        try:
            with self.assertRaisesRegex(tool.AerostError, "escapes repository root"):
                tool.load_application_manifest(ROOT, path)
        finally:
            path.unlink(missing_ok=True)

    def test_manifest_rejects_duplicate_scenario_paths(self) -> None:
        original = json.loads(
            (ROOT / "applications/power-supervisor.application.json").read_text(encoding="utf-8")
        )
        modified = copy.deepcopy(original)
        modified["controlled_scenarios"] = [
            modified["manual_requirements_scenarios"],
            modified["manual_requirements_scenarios"],
        ]
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", dir=ROOT / "applications", delete=False, encoding="utf-8"
        ) as stream:
            json.dump(modified, stream)
            path = Path(stream.name)
        try:
            with self.assertRaisesRegex(tool.AerostError, "duplicate file paths"):
                tool.load_application_manifest(ROOT, path)
        finally:
            path.unlink(missing_ok=True)

    def test_explicit_manifest_preserves_power_results(self) -> None:
        application = tool.load_application_manifest(ROOT)
        scenarios = tool.load_controlled_scenarios(ROOT, application)
        self.assertEqual(len(scenarios), 48)
        self.assertEqual(sum(len(item["cycles"]) for item in scenarios), 53)


if __name__ == "__main__":
    unittest.main()
