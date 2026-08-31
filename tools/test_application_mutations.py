from __future__ import annotations

import json
import unittest
from pathlib import Path

import aerost_tool as tool
from application_mutations import build_mutated_airs, load_mutation_profile


ROOT = Path(__file__).resolve().parents[1]


class ApplicationMutationProfileTests(unittest.TestCase):
    def _air_and_profile(self, manifest: str):
        application = tool.load_application_manifest(ROOT, Path(manifest))
        source = application.source_path.read_text(encoding="utf-8")
        typed = tool.semantic_analyze(tool.parse_source(source))
        contract = tool.load_execution_contract(application.runtime_fault_policy_path, typed)
        air = tool.lower_to_air(typed, contract)
        profile = load_mutation_profile(application.mutation_profile_path)
        return application, air, profile

    def test_power_profile_builds_eight_distinct_mutants(self) -> None:
        application, air, profile = self._air_and_profile(
            "applications/power-supervisor.application.json"
        )
        self.assertEqual(profile["application_id"], application.application_id)
        mutants = build_mutated_airs(air, profile)
        self.assertEqual(len(mutants), 8)
        self.assertEqual(len({tool.canonical_json(item) for item in mutants.values()}), 8)

    def test_communication_profile_builds_eight_distinct_mutants(self) -> None:
        application, air, profile = self._air_and_profile(
            "applications/communication-link-supervisor.application.json"
        )
        self.assertEqual(profile["application_id"], application.application_id)
        mutants = build_mutated_airs(air, profile)
        self.assertEqual(len(mutants), 8)
        self.assertEqual(len({tool.canonical_json(item) for item in mutants.values()}), 8)

    def test_profiles_validate_against_schema(self) -> None:
        schema = json.loads(
            (ROOT / "schemas/mutation-profile.schema.json").read_text(encoding="utf-8")
        )
        for relative in (
            "examples/power-supervisor/mutations/mutation-profile.json",
            "examples/communication-link-supervisor/mutations/mutation-profile.json",
        ):
            document = json.loads((ROOT / relative).read_text(encoding="utf-8"))
            self.assertEqual(tool.validate_json_schema(document, schema), [], relative)


if __name__ == "__main__":
    unittest.main()
