from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import assurance_obligations as obligations

ROOT = Path(__file__).resolve().parents[1]
AIR_PATH = ROOT / "artifacts" / "latest" / "power-supervisor.air.json"
FAULT_PATH = ROOT / "examples" / "power-supervisor" / "policies" / "runtime-fault-policy.json"
TRACEABILITY_PATH = ROOT / "artifacts" / "latest" / "traceability-report.json"
SCHEMA_PATH = ROOT / "schemas" / "assurance-obligations.schema.json"


def require_artifacts(test_case: unittest.TestCase) -> None:
    for path in (AIR_PATH, FAULT_PATH, TRACEABILITY_PATH, SCHEMA_PATH):
        if not path.exists():
            test_case.skipTest(f"required controlled artifact is absent: {path}")


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class AssuranceObligationTests(unittest.TestCase):
    def setUp(self) -> None:
        require_artifacts(self)
        self.air = load(AIR_PATH)
        self.fault = load(FAULT_PATH)
        self.traceability = load(TRACEABILITY_PATH)
        self.result = obligations.extract_obligations(
            air=self.air,
            source_air_sha256=obligations.sha256_file(AIR_PATH),
            fault_policy_document=self.fault,
            traceability_document=self.traceability,
        )
        self.by_id = {item["id"]: item for item in self.result["obligations"]}

    def test_emits_stable_obligation_ids(self) -> None:
        ids = [item["id"] for item in self.result["obligations"]]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), len(set(ids)))
        for obligation_id in ids:
            self.assertRegex(obligation_id, r"^OBL-[A-Za-z0-9._-]+$")

    def test_extracts_every_air_statement(self) -> None:
        expected = {
            f"OBL-STMT-{record['id']}"
            for record in obligations.collect_statement_records(self.air["program"])
        }
        actual = {
            item["id"]
            for item in self.result["obligations"]
            if item["kind"] == "STATEMENT_REACHED"
        }
        self.assertEqual(actual, expected)
        self.assertGreater(len(actual), 0)

    def test_extracts_both_outcomes_for_every_decision_and_condition(self) -> None:
        decisions = obligations.collect_decision_records(self.air["program"])
        for decision in decisions:
            decision_id = decision["decision_id"]
            self.assertIn(f"OBL-DEC-{decision_id}-TRUE", self.by_id)
            self.assertIn(f"OBL-DEC-{decision_id}-FALSE", self.by_id)
            for condition_id in decision["condition_ids"]:
                self.assertIn(f"OBL-COND-{condition_id}-TRUE", self.by_id)
                self.assertIn(f"OBL-COND-{condition_id}-FALSE", self.by_id)

    def test_selected_mcdc_obligations_follow_air_metadata(self) -> None:
        expected = {
            f"OBL-MCDC-{decision['decision_id']}-{condition_id}"
            for decision in obligations.collect_decision_records(self.air["program"])
            if decision["mcdc"]
            for condition_id in decision["condition_ids"]
        }
        actual = {
            item["id"]
            for item in self.result["obligations"]
            if item["kind"] == "MCDC_PAIR"
        }
        self.assertEqual(actual, expected)
        self.assertGreater(len(actual), 0)

    def test_case_arm_event_mapping_is_exported(self) -> None:
        case_obligations = [
            item for item in self.result["obligations"]
            if item["kind"] == "CASE_ARM_REACHED"
        ]
        self.assertGreater(len(case_obligations), 0)
        for item in case_obligations:
            self.assertIn("semantic_event_id", item["attributes"])
            self.assertIn("::", item["attributes"]["semantic_event_id"])

    def test_fault_policy_obligations_are_metadata_driven(self) -> None:
        kinds = {item["kind"] for item in self.result["obligations"]}
        self.assertIn("FAULT_POLICY_ACTIVATED", kinds)
        self.assertIn("NORMAL_COMMIT_INHIBITED", kinds)
        self.assertIn("CONSERVATIVE_OUTPUT_APPLIED", kinds)

        mutated_fault = copy.deepcopy(self.fault)
        mutated_fault["runtime_fault_policy"]["diagnostic"] = "DIFFERENT-DIAGNOSTIC"
        with self.assertRaises(obligations.ObligationExtractionError):
            obligations.extract_obligations(
                air=self.air,
                source_air_sha256=obligations.sha256_file(AIR_PATH),
                fault_policy_document=mutated_fault,
                traceability_document=self.traceability,
            )

    def test_explicit_reset_and_recovery_obligations_are_emitted(self) -> None:
        reset = [
            item for item in self.result["obligations"]
            if item["kind"] == "RESET_PATH_EXECUTED"
        ]
        recovery = [
            item for item in self.result["obligations"]
            if item["kind"] == "RECOVERY_PATH_EXECUTED"
        ]
        self.assertEqual(len(reset), 2)
        self.assertEqual(len(recovery), 2)
        for item in reset + recovery:
            self.assertIn("state_variable", item["attributes"])
            self.assertIn("from_values", item["attributes"])
            self.assertIn("to_value", item["attributes"])

    def test_requirement_obligations_match_traceability_roots(self) -> None:
        expected = {
            f"OBL-REQ-{value}"
            for value in self.traceability["application_requirements"]
        }
        actual = {
            item["id"]
            for item in self.result["obligations"]
            if item["kind"] == "REQUIREMENT_EXERCISED"
        }
        self.assertEqual(actual, expected)

    def test_repeated_extraction_is_byte_identical(self) -> None:
        first = obligations.canonical_json(self.result)
        second_result = obligations.extract_obligations(
            air=load(AIR_PATH),
            source_air_sha256=obligations.sha256_file(AIR_PATH),
            fault_policy_document=load(FAULT_PATH),
            traceability_document=load(TRACEABILITY_PATH),
        )
        second = obligations.canonical_json(second_result)
        self.assertEqual(first, second)

    def test_extractor_has_no_application_specific_logic(self) -> None:
        source = (ROOT / "tools" / "assurance_obligations.py").read_text(
            encoding="utf-8"
        )
        forbidden = (
            "BatteryAHealthy",
            "BatteryBHealthy",
            "PowerSupervisor",
            "LinkDegraded",
            "CommunicationLost",
            "ReturnToHome",
        )
        for token in forbidden:
            self.assertNotIn(token, source)

    def test_cli_writes_schema_compatible_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "assurance-obligations.json"
            process = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools" / "assurance_obligations.py"),
                    "extract",
                    "--air",
                    str(AIR_PATH),
                    "--fault-policy",
                    str(FAULT_PATH),
                    "--traceability",
                    str(TRACEABILITY_PATH),
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertTrue(output.exists())
            document = load(output)
            schema = load(SCHEMA_PATH)

            sys.path.insert(0, str(ROOT / "tools"))
            try:
                from aerost_tool import validate_json_schema
            except ImportError:
                self.skipTest("aerost_tool schema validator is unavailable")
            errors = validate_json_schema(document, schema)
            self.assertEqual(errors, [])

    def test_rejects_invalid_semantic_identity(self) -> None:
        invalid_air = copy.deepcopy(self.air)
        invalid_air["program"]["transition_stage"][0]["id"] = "AIR STMT INVALID"
        with self.assertRaises(obligations.ObligationExtractionError):
            obligations.extract_obligations(
                air=invalid_air,
                source_air_sha256="0" * 64,
                fault_policy_document=None,
                traceability_document=None,
            )


if __name__ == "__main__":
    unittest.main()
