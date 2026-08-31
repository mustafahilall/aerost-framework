from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import aerost_tool as tool
from application_case_validator import validate_application_case
from assurance_obligations import extract_obligations
from assurance_test_synthesizer import synthesize
from compare_assurance_suites import compare_suites
from multi_case_assurance_pipeline import (
    MultiCasePipelineError,
    _validate_output_directory,
)

ROOT = Path(__file__).resolve().parents[1]


def load(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


class ResearchBaselineHardeningTests(unittest.TestCase):
    def test_canonical_research_suite_identity(self) -> None:
        suite = load("applications/research-suite.json")
        self.assertEqual(suite["suite_id"], "aerost-research-baseline")
        self.assertEqual(list((ROOT / "applications").glob("research-suite-v*.json")), [])

    def test_output_directory_safety_rejects_repository_and_parent(self) -> None:
        with self.assertRaises(MultiCasePipelineError):
            _validate_output_directory(ROOT, ROOT)
        with self.assertRaises(MultiCasePipelineError):
            _validate_output_directory(ROOT, ROOT.parent)
        with tempfile.TemporaryDirectory() as directory:
            safe = Path(directory) / "output"
            self.assertEqual(_validate_output_directory(ROOT, safe), safe.resolve())

    def test_execution_contract_declares_reset_and_recovery_paths(self) -> None:
        for manifest_path, expected_reset, expected_recovery in (
            ("applications/power-supervisor.application.json", 2, 2),
            ("applications/communication-link-supervisor.application.json", 1, 3),
        ):
            application = tool.load_application_manifest(ROOT, Path(manifest_path))
            source = application.source_path.read_text(encoding="utf-8")
            typed = tool.semantic_analyze(tool.parse_source(source))
            contract = tool.load_execution_contract(application.runtime_fault_policy_path, typed)
            metadata = contract["assurance_obligations"]
            self.assertEqual(len(metadata["reset_paths"]), expected_reset)
            self.assertEqual(len(metadata["recovery_paths"]), expected_recovery)

    def test_resource_limit_is_reported_as_incomplete_search(self) -> None:
        application = tool.load_application_manifest(
            ROOT, Path("applications/communication-link-supervisor.application.json")
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "case"
            bundle = tool.build_core_bundle(
                ROOT, output, compile_backend=False, application=application
            )
            obligations = extract_obligations(
                air=bundle["air"],
                source_air_sha256=tool.sha256_file(output / application.air_artifact_name),
                fault_policy_document=load("examples/communication-link-supervisor/policies/runtime-fault-policy.json"),
                traceability_document=tool.load_json(output / "traceability-report.json"),
            )
            domain = load("tests/input-domains/communication-link-input-domain.json")
            truncated = copy.deepcopy(domain)
            truncated["search_bounds"]["maximum_reachable_states"] = 1
            truncated["search_bounds"]["maximum_transition_evaluations"] = 512
            _, report = synthesize(bundle["air"], obligations, truncated)
            self.assertFalse(report["search_summary"]["search_complete"])
            self.assertTrue(report["search_summary"]["transition_limit_reached"])
            self.assertEqual(
                report["search_summary"]["termination_reason"],
                "transition-limit-reached",
            )
            self.assertFalse(report["passed"])
            self.assertGreater(
                report["obligation_summary"]["unresolved_due_to_resource_limit"], 0
            )

    def test_comparison_pass_requires_final_closure_and_honors_manual_gate(self) -> None:
        application = tool.load_application_manifest(
            ROOT, Path("applications/communication-link-supervisor.application.json")
        )
        with tempfile.TemporaryDirectory() as directory:
            summary = validate_application_case(
                ROOT, application.manifest_path, Path(directory) / "case", compile_backend=False
            )
            comparison = summary["assurance_suite_comparison"]
            self.assertTrue(comparison["replay_passed"])
            self.assertTrue(comparison["final_automatic_complete"])
            self.assertFalse(comparison["manual_suite_gate"])
            self.assertTrue(comparison["manual_suite_gate_passed"])
            self.assertTrue(comparison["acceptance_passed"])
            self.assertTrue(comparison["passed"])

    def test_application_case_summary_has_own_valid_schema_record(self) -> None:
        application = tool.load_application_manifest(
            ROOT, Path("applications/communication-link-supervisor.application.json")
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "case"
            summary = validate_application_case(
                ROOT, application.manifest_path, output, compile_backend=False
            )
            records = summary["schema_validation"]["records"]
            record = next(
                item for item in records
                if item["artifact"] == "application-case-validation-summary.json"
            )
            self.assertTrue(record["valid"])
            self.assertEqual(record["errors"], [])


if __name__ == "__main__":
    unittest.main()
