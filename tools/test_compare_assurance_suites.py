from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from aerost_tool import validate_json_schema
from assurance_obligations import extract_obligations
from assurance_suite_reducer import reduce_suite
from assurance_test_synthesizer import synthesize
from mutation_aware_suite_reducer import reduce_suite as reduce_mutation_aware_suite
from compare_assurance_suites import (
    ComparisonError,
    canonical_json_bytes,
    combine_manual_suites,
    compare_suites,
)


class AssuranceSuiteComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.air = json.loads(
            (cls.root / "artifacts/latest/power-supervisor.air.json").read_text(
                encoding="utf-8"
            )
        )
        cls.fault_policy = json.loads(
            (cls.root / "examples/power-supervisor/policies/runtime-fault-policy.json").read_text(
                encoding="utf-8"
            )
        )
        cls.traceability = json.loads(
            (cls.root / "artifacts/latest/traceability-report.json").read_text(
                encoding="utf-8"
            )
        )
        cls.domain = json.loads(
            (
                cls.root
                / "tests/input-domains/power-supervisor-input-domain.json"
            ).read_text(encoding="utf-8")
        )
        cls.manual_requirements = json.loads(
            (cls.root / "tests/scenarios.json").read_text(encoding="utf-8")
        )
        cls.manual_closure = json.loads(
            (cls.root / "tests/coverage-closure-scenarios.json").read_text(
                encoding="utf-8"
            )
        )
        import hashlib
        source_air_sha256 = hashlib.sha256(
            (cls.root / "artifacts/latest/power-supervisor.air.json").read_bytes()
        ).hexdigest()
        cls.obligations = extract_obligations(
            air=cls.air,
            source_air_sha256=source_air_sha256,
            fault_policy_document=cls.fault_policy,
            traceability_document=cls.traceability,
        )
        cls.unreduced, cls.synthesis_report = synthesize(
            cls.air,
            cls.obligations,
            cls.domain,
        )
        cls.obligation_reduced, cls.reduction_report = reduce_suite(
            cls.unreduced,
            cls.obligations,
        )
        (
            cls.mutation_aware_reduced,
            cls.mutation_aware_reduction_report,
        ) = reduce_mutation_aware_suite(
            cls.air,
            cls.unreduced,
            cls.obligations,
        )
        cls.report = compare_suites(
            cls.air,
            cls.obligations,
            cls.manual_requirements,
            cls.manual_closure,
            cls.unreduced,
            cls.obligation_reduced,
            cls.mutation_aware_reduced,
        )
        cls.by_name = {item["name"]: item for item in cls.report["suites"]}

    def test_controlled_suite_sizes_are_reported(self) -> None:
        expected = {
            "manual-requirements": (24, 29),
            "manual-closure": (48, 53),
            "automatic-unreduced": (333, 773),
            "automatic-obligation-reduced": (57, 142),
            "automatic-mutation-aware-reduced": (58, 146),
        }
        actual = {
            name: (item["scenario_count"], item["cycle_count"])
            for name, item in self.by_name.items()
        }
        self.assertEqual(actual, expected)

    def test_manual_requirements_suite_exposes_coverage_gap(self) -> None:
        suite = self.by_name["manual-requirements"]
        self.assertEqual(suite["obligations"]["covered"], 240)
        self.assertEqual(suite["obligations"]["uncovered_count"], 87)
        self.assertEqual(suite["mcdc"], {"covered": 6, "total": 6, "complete": True})
        self.assertEqual(suite["mutation"]["killed"], 8)

    def test_manual_closure_and_unreduced_suite_close_all_obligations(self) -> None:
        for name in ("manual-closure", "automatic-unreduced"):
            suite = self.by_name[name]
            self.assertEqual(suite["obligations"]["covered"], 327)
            self.assertEqual(suite["obligations"]["uncovered_count"], 0)
            self.assertEqual(suite["mcdc"]["covered"], 6)
            self.assertEqual(suite["mutation"]["killed"], 8)

    def test_obligation_reduced_suite_preserves_obligations_but_loses_one_mutant(self) -> None:
        suite = self.by_name["automatic-obligation-reduced"]
        self.assertEqual(suite["obligations"]["covered"], 327)
        self.assertEqual(suite["mcdc"]["covered"], 6)
        self.assertEqual(suite["mutation"]["killed"], 7)
        self.assertEqual(suite["mutation"]["surviving"], ["MUT-WEAKEN-RECOVERY"])

    def test_mutation_aware_suite_restores_full_mutation_detection(self) -> None:
        suite = self.by_name["automatic-mutation-aware-reduced"]
        self.assertEqual(suite["obligations"]["covered"], 327)
        self.assertEqual(suite["mcdc"]["covered"], 6)
        self.assertEqual(suite["mutation"]["killed"], 8)
        self.assertEqual(suite["mutation"]["surviving"], [])

    def test_reduction_delta_matches_controlled_result(self) -> None:
        delta = next(
            item
            for item in self.report["comparisons"]
            if item["baseline"] == "automatic-unreduced"
            and item["candidate"] == "automatic-obligation-reduced"
        )
        self.assertEqual(delta["scenario_delta"], -276)
        self.assertEqual(delta["cycle_delta"], -631)
        self.assertEqual(delta["obligation_coverage_delta"], 0)
        self.assertEqual(delta["mcdc_delta"], 0)
        self.assertEqual(delta["mutation_kill_delta"], -1)

    def test_final_mutation_aware_reduction_delta_matches_controlled_result(self) -> None:
        delta = next(
            item
            for item in self.report["comparisons"]
            if item["baseline"] == "automatic-unreduced"
            and item["candidate"] == "automatic-mutation-aware-reduced"
        )
        self.assertEqual(delta["scenario_delta"], -275)
        self.assertEqual(delta["cycle_delta"], -627)
        self.assertEqual(delta["obligation_coverage_delta"], 0)
        self.assertEqual(delta["mcdc_delta"], 0)
        self.assertEqual(delta["mutation_kill_delta"], 0)

    def test_mutation_restoration_cost_matches_controlled_result(self) -> None:
        delta = next(
            item
            for item in self.report["comparisons"]
            if item["baseline"] == "automatic-obligation-reduced"
            and item["candidate"] == "automatic-mutation-aware-reduced"
        )
        self.assertEqual(delta["scenario_delta"], 1)
        self.assertEqual(delta["cycle_delta"], 4)
        self.assertEqual(delta["obligation_coverage_delta"], 0)
        self.assertEqual(delta["mcdc_delta"], 0)
        self.assertEqual(delta["mutation_kill_delta"], 1)

    def test_mutation_aware_delta_against_manual_closure(self) -> None:
        delta = next(
            item
            for item in self.report["comparisons"]
            if item["baseline"] == "manual-closure"
            and item["candidate"] == "automatic-mutation-aware-reduced"
        )
        self.assertEqual(delta["scenario_delta"], 10)
        self.assertEqual(delta["cycle_delta"], 93)
        self.assertEqual(delta["obligation_coverage_delta"], 0)
        self.assertEqual(delta["mcdc_delta"], 0)
        self.assertEqual(delta["mutation_kill_delta"], 0)

    def test_combined_manual_suite_contains_no_duplicate_ids(self) -> None:
        combined = combine_manual_suites(
            self.manual_requirements,
            self.manual_closure,
        )
        ids = [item["id"] for item in combined["scenarios"]]
        self.assertEqual(len(ids), 48)
        self.assertEqual(len(ids), len(set(ids)))

    def test_output_validates_against_declared_schema(self) -> None:
        schema = json.loads(
            (
                self.root
                / "schemas/assurance-suite-comparison.schema.json"
            ).read_text(encoding="utf-8")
        )
        errors = validate_json_schema(self.report, schema)
        self.assertEqual(errors, [])

    def test_comparison_is_byte_identical(self) -> None:
        second = compare_suites(
            self.air,
            self.obligations,
            self.manual_requirements,
            self.manual_closure,
            self.unreduced,
            self.obligation_reduced,
            self.mutation_aware_reduced,
        )
        self.assertEqual(canonical_json_bytes(self.report), canonical_json_bytes(second))

    def test_cli_writes_byte_identical_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            paths = {
                "air": temp / "air.json",
                "obligations": temp / "obligations.json",
                "manual": temp / "manual.json",
                "closure": temp / "closure.json",
                "unreduced": temp / "unreduced.json",
                "obligation_reduced": temp / "obligation-reduced.json",
                "mutation_reduced": temp / "mutation-reduced.json",
            }
            values = {
                "air": self.air,
                "obligations": self.obligations,
                "manual": self.manual_requirements,
                "closure": self.manual_closure,
                "unreduced": self.unreduced,
                "obligation_reduced": self.obligation_reduced,
                "mutation_reduced": self.mutation_aware_reduced,
            }
            for name, path in paths.items():
                path.write_bytes(canonical_json_bytes(values[name]))
            first = temp / "first.json"
            second = temp / "second.json"
            command = [
                sys.executable,
                str(self.root / "tools/compare_assurance_suites.py"),
                "compare",
                "--air",
                str(paths["air"]),
                "--obligations",
                str(paths["obligations"]),
                "--manual-requirements",
                str(paths["manual"]),
                "--manual-closure",
                str(paths["closure"]),
                "--automatic-unreduced",
                str(paths["unreduced"]),
                "--automatic-obligation-reduced",
                str(paths["obligation_reduced"]),
                "--automatic-mutation-aware-reduced",
                str(paths["mutation_reduced"]),
                "--output",
                str(first),
            ]
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(self.root / "tools")
            subprocess.run(command, check=True, capture_output=True, text=True, env=environment)
            command[-1] = str(second)
            subprocess.run(command, check=True, capture_output=True, text=True, env=environment)
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_rejects_program_identity_mismatch(self) -> None:
        altered = copy.deepcopy(self.mutation_aware_reduced)
        altered["program_id"] = "AIR-PROG-WRONG"
        with self.assertRaisesRegex(ComparisonError, "program identities"):
            compare_suites(
                self.air,
                self.obligations,
                self.manual_requirements,
                self.manual_closure,
                self.unreduced,
                self.obligation_reduced,
                altered,
            )

    def test_rejects_tampered_expected_result(self) -> None:
        altered = copy.deepcopy(self.manual_requirements)
        altered["scenarios"][0]["cycles"][0]["expected"][
            "normal_commit_inhibited"
        ] = True
        with self.assertRaisesRegex(ComparisonError, "replay mismatch"):
            compare_suites(
                self.air,
                self.obligations,
                altered,
                self.manual_closure,
                self.unreduced,
                self.obligation_reduced,
                self.mutation_aware_reduced,
            )

    def test_comparison_source_has_no_application_specific_logic(self) -> None:
        source = (
            self.root / "tools/compare_assurance_suites.py"
        ).read_text(encoding="utf-8")
        forbidden = [
            "BatteryAHealthy",
            "BatteryBHealthy",
            "PayloadOvercurrent",
            "PowerSupervisor",
            "CommunicationLost",
            "LinkDegraded",
            "ReturnToHome",
        ]
        for token in forbidden:
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
