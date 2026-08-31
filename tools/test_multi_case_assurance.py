from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from multi_case_assurance_pipeline import _reproducibility_report, run_multi_case_pipeline


ROOT = Path(__file__).resolve().parents[1]
SUITE = Path("applications/research-suite.json")


class MultiCaseAssuranceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.output = Path(cls.temporary_directory.name) / "multi-case"
        cls.summary = run_multi_case_pipeline(
            ROOT,
            SUITE,
            cls.output,
            compile_backend=False,
            check_reproducibility=False,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    def test_two_application_results_are_aggregated(self) -> None:
        self.assertEqual(self.summary["application_count"], 2)
        self.assertEqual(
            {item["id"] for item in self.summary["applications"]},
            {"power-supervisor", "communication-link-supervisor"},
        )

    def test_aggregate_execution_and_assurance_targets_close(self) -> None:
        aggregate = self.summary["aggregate"]
        self.assertEqual(aggregate["controlled_scenarios"], 95)
        self.assertEqual(aggregate["executed_cycles"], 132)
        self.assertEqual(aggregate["equivalent_cycles"], 132)
        self.assertEqual(aggregate["assurance_obligations_covered"], 538)
        self.assertEqual(aggregate["assurance_obligations_total"], 538)
        self.assertEqual(aggregate["selected_mcdc_covered"], 13)
        self.assertEqual(aggregate["selected_mcdc_total"], 13)
        self.assertEqual(aggregate["controlled_mutants_killed"], 16)
        self.assertEqual(aggregate["controlled_mutants_total"], 16)

    def test_aggregate_final_automatic_suite_size(self) -> None:
        aggregate = self.summary["aggregate"]
        self.assertEqual(aggregate["final_automatic_scenarios"], 90)
        self.assertEqual(aggregate["final_automatic_cycles"], 213)


    def test_root_artifacts_validate_against_schemas(self) -> None:
        report = json.loads(
            (self.output / "multi-case-schema-validation.json").read_text(encoding="utf-8")
        )
        self.assertTrue(report["all_valid"])

    def test_reproducibility_comparator_detects_identity_and_change(self) -> None:
        with tempfile.TemporaryDirectory() as left_dir, tempfile.TemporaryDirectory() as right_dir:
            left = Path(left_dir)
            right = Path(right_dir)
            (left / "a.json").write_text("{}\n", encoding="utf-8")
            (right / "a.json").write_text("{}\n", encoding="utf-8")
            same = _reproducibility_report(left, right, enabled=True)
            self.assertTrue(same["byte_identical"])
            self.assertEqual(same["files_identical"], 1)
            (right / "a.json").write_text("{\"changed\":true}\n", encoding="utf-8")
            changed = _reproducibility_report(left, right, enabled=True)
            self.assertFalse(changed["byte_identical"])
            self.assertEqual(len(changed["mismatches"]), 1)

    def test_stable_baseline_identity_is_reported(self) -> None:
        self.assertEqual(self.summary["suite_id"], "aerost-research-baseline")
        self.assertEqual(self.summary["tool_version"], "AEROST-TOOL-RESEARCH-BASELINE")

    def test_assembly_mode_is_not_authoritative(self) -> None:
        self.assertFalse(self.summary["authoritative_full_pipeline"])
        self.assertTrue(self.summary["assembly_validation_mode"])
        self.assertTrue(self.summary["passed"])


if __name__ == "__main__":
    unittest.main()
