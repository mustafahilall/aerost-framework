from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import aerost_tool as tool
from application_case_validator import validate_application_case


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path("applications/communication-link-supervisor.application.json")


class CommunicationLinkCaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.output = Path(cls.temporary_directory.name) / "communication-link"
        cls.summary = validate_application_case(
            ROOT,
            MANIFEST,
            cls.output,
            compile_backend=False,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    def test_manifest_resolves_second_application(self) -> None:
        application = tool.load_application_manifest(ROOT, MANIFEST)
        self.assertEqual(application.application_id, "communication-link-supervisor")
        self.assertEqual(
            application.air_artifact_name,
            "communication-link-supervisor.air.json",
        )

    def test_independent_manual_oracles_replay_without_mismatch(self) -> None:
        oracle = json.loads(
            (self.output / "independent-oracle-comparison.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(oracle["passed"])
        self.assertEqual(oracle["mismatch_count"], 0)
        self.assertEqual(oracle["equivalent_cycles"], 79)

    def test_three_execution_paths_are_equivalent(self) -> None:
        core = self.summary["core"]
        self.assertTrue(core["passed"])
        self.assertEqual(core["controlled_scenarios"], 47)
        self.assertEqual(core["executed_cycles"], 79)
        self.assertEqual(core["three_way_equivalent_cycles"], 79)
        self.assertEqual(core["three_way_mismatch_count"], 0)

    def test_structural_mcdc_traceability_and_expressive_gates_close(self) -> None:
        core = self.summary["core"]
        self.assertTrue(core["coverage_complete"])
        self.assertTrue(core["mcdc_complete"])
        self.assertTrue(core["traceability_complete"])
        self.assertEqual(core["expressive_adequacy_percent"], 100.0)

    def test_bounded_domain_and_synthesis_close_declared_obligations(self) -> None:
        domain = self.summary["bounded_input_domain"]
        synthesis = self.summary["automatic_test_synthesis"]
        self.assertTrue(domain["passed"])
        self.assertEqual(domain["input_vector_count"], 512)
        self.assertTrue(synthesis["passed"])
        self.assertEqual(synthesis["obligation_summary"]["covered"], 211)
        self.assertEqual(synthesis["obligation_summary"]["total"], 211)
        self.assertEqual(synthesis["search_summary"]["states_explored"], 10)
        self.assertEqual(synthesis["search_summary"]["transitions_explored"], 5130)

    def test_obligation_only_reduction_is_deterministic_and_complete(self) -> None:
        reduction = self.summary["obligation_only_reduction"]
        self.assertTrue(reduction["passed"])
        self.assertEqual(reduction["input_summary"]["scenarios"], 218)
        self.assertEqual(reduction["input_summary"]["cycles"], 422)
        self.assertEqual(reduction["output_summary"]["selected_scenarios"], 29)
        self.assertEqual(reduction["output_summary"]["selected_cycles"], 60)
        self.assertEqual(reduction["mcdc_summary"]["missing_witnesses"], [])


    def test_mutation_aware_reduction_preserves_all_targets(self) -> None:
        reduction = self.summary["mutation_aware_reduction"]
        final_suite = self.summary["final_automatic_suite"]
        self.assertTrue(reduction["passed"])
        self.assertEqual(reduction["mutation_summary"]["killed"], 8)
        self.assertEqual(reduction["mutation_summary"]["total"], 8)
        self.assertEqual(reduction["mutation_summary"]["surviving"], [])
        self.assertEqual(final_suite["scenario_count"], 32)
        self.assertEqual(final_suite["cycle_count"], 67)
        self.assertEqual(final_suite["obligations"]["covered"], 211)
        self.assertEqual(final_suite["mcdc"]["covered"], 7)
        self.assertEqual(final_suite["mutation"]["killed"], 8)

    def test_case_artifacts_validate_against_schemas(self) -> None:
        self.assertTrue(self.summary["schema_validation"]["all_valid"])
        self.assertTrue(self.summary["passed"])


if __name__ == "__main__":
    unittest.main()
