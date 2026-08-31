from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import aerost_tool as tool

ROOT = Path(__file__).resolve().parents[1]


class AuthoritativeAssurancePipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.output = Path(cls.temporary_directory.name) / "bundle"
        cls.application = tool.load_application_manifest(ROOT)
        tool.build_core_bundle(
            ROOT, cls.output, compile_backend=False, application=cls.application
        )
        cls.assurance = tool.run_assurance_evidence_pipeline(
            ROOT, cls.output, application=cls.application
        )
        cls.summary = tool.assurance_results_summary(cls.assurance)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    def test_application_manifest_is_emitted(self) -> None:
        self.assertTrue((self.output / "application-manifest.json").is_file())
        self.assertEqual(self.application.application_id, "power-supervisor")

    def test_authoritative_assurance_artifacts_are_emitted(self) -> None:
        for relative in tool.ASSURANCE_ARTIFACTS:
            self.assertTrue((self.output / relative).is_file(), relative)

    def test_controlled_synthesis_and_reduction_results(self) -> None:
        synthesis = self.summary["automatic_test_synthesis"]
        self.assertEqual(synthesis["obligation_summary"]["covered"], 327)
        self.assertEqual(synthesis["obligation_summary"]["uncovered"], 0)
        self.assertEqual(synthesis["search_summary"]["states_explored"], 12)
        self.assertEqual(synthesis["search_summary"]["transitions_explored"], 196620)
        self.assertEqual(synthesis["suite_summary"]["selected_scenarios"], 333)
        self.assertEqual(synthesis["suite_summary"]["selected_cycles"], 773)

    def test_mutation_aware_final_suite_closes_all_targets(self) -> None:
        reduction = self.summary["mutation_aware_suite_reduction"]
        self.assertEqual(reduction["output_summary"]["selected_scenarios"], 58)
        self.assertEqual(reduction["output_summary"]["selected_cycles"], 146)
        self.assertEqual(reduction["mutation_summary"]["killed"], 8)
        self.assertEqual(reduction["mutation_summary"]["total"], 8)
        self.assertEqual(reduction["mutation_summary"]["surviving"], [])

    def test_five_suite_comparison_is_authoritative(self) -> None:
        comparison = self.summary["assurance_suite_comparison"]
        self.assertTrue(comparison["passed"])
        self.assertEqual(comparison["suite_count"], 5)
        by_name = {suite["name"]: suite for suite in comparison["suites"]}
        final_suite = by_name["automatic-mutation-aware-reduced"]
        self.assertEqual(final_suite["obligations"]["covered"], 327)
        self.assertEqual(final_suite["obligations"]["total"], 327)
        self.assertEqual(final_suite["mcdc"]["covered"], 6)
        self.assertEqual(final_suite["mutation"]["killed"], 8)

    def test_new_artifacts_validate_against_declared_schemas(self) -> None:
        pairs = {
            "application-manifest.json": "application-manifest.schema.json",
            "mutation-profile.json": "mutation-profile.schema.json",
            "controlled-input-domain.json": "input-domain.schema.json",
            "input-domain-validation.json": "input-domain-validation.schema.json",
            "assurance-obligations.json": "assurance-obligations.schema.json",
            "synthesized-scenarios.json": "synthesized-scenarios.schema.json",
            "synthesis-report.json": "synthesis-report.schema.json",
            "obligation-reduced-scenarios.json": "synthesized-scenarios.schema.json",
            "suite-reduction-report.json": "suite-reduction-report.schema.json",
            "mutation-aware-reduced-scenarios.json": "synthesized-scenarios.schema.json",
            "mutation-aware-suite-reduction-report.json": "mutation-aware-suite-reduction-report.schema.json",
            "assurance-suite-comparison.json": "assurance-suite-comparison.schema.json",
        }
        for artifact, schema in pairs.items():
            document = json.loads((self.output / artifact).read_text(encoding="utf-8"))
            schema_document = json.loads((ROOT / "schemas" / schema).read_text(encoding="utf-8"))
            errors = tool.validate_json_schema(document, schema_document)
            self.assertEqual(errors, [], f"{artifact}: {errors}")


if __name__ == "__main__":
    unittest.main()
