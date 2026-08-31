from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

from aerost_tool import validate_json_schema
from assurance_obligations import extract_obligations
from assurance_test_synthesizer import (
    SynthesisError,
    canonical_json_bytes,
    synthesize,
)
from external_air_executor import AirExecutor


ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class AssuranceTestSynthesizerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.air_path = ROOT / "artifacts/latest/power-supervisor.air.json"
        cls.air = load_json(cls.air_path)
        cls.fault_policy = load_json(
            ROOT / "examples/power-supervisor/policies/runtime-fault-policy.json"
        )
        cls.traceability = load_json(
            ROOT / "artifacts/latest/traceability-report.json"
        )
        cls.domain = load_json(
            ROOT
            / "tests/input-domains/power-supervisor-input-domain.json"
        )
        cls.obligations = extract_obligations(
            air=cls.air,
            source_air_sha256=hashlib.sha256(
                cls.air_path.read_bytes()
            ).hexdigest(),
            fault_policy_document=cls.fault_policy,
            traceability_document=cls.traceability,
        )
        cls.scenarios, cls.report = synthesize(
            cls.air, cls.obligations, cls.domain
        )

    def test_power_supervisor_closes_every_declared_obligation(self) -> None:
        summary = self.report["obligation_summary"]
        self.assertEqual(summary["total"], 327)
        self.assertEqual(summary["covered"], 327)
        self.assertEqual(summary["uncovered"], 0)
        self.assertTrue(self.report["passed"])

    def test_search_metrics_are_deterministic_for_controlled_domain(self) -> None:
        self.assertEqual(
            self.report["search_summary"],
            {
                "states_explored": 12,
                "transitions_explored": 196620,
                "candidate_sequences": 333,
                "search_complete": True,
                "termination_reason": "reachable-frontier-exhausted",
                "state_limit_reached": False,
                "transition_limit_reached": False,
                "depth_frontier_remaining": False,
            },
        )
        self.assertEqual(
            self.report["suite_summary"],
            {
                "selected_scenarios": 333,
                "selected_cycles": 773,
                "covered_obligations": 327,
            },
        )

    def test_outputs_validate_against_declared_schemas(self) -> None:
        scenario_schema = load_json(
            ROOT / "schemas/synthesized-scenarios.schema.json"
        )
        report_schema = load_json(
            ROOT / "schemas/synthesis-report.schema.json"
        )
        self.assertEqual(
            validate_json_schema(self.scenarios, scenario_schema), []
        )
        self.assertEqual(
            validate_json_schema(self.report, report_schema), []
        )

    def test_every_generated_cycle_replays_to_expected_result(self) -> None:
        executor = AirExecutor(self.air)
        for scenario in self.scenarios["scenarios"]:
            state = copy.deepcopy(scenario["initial_state"])
            for cycle_index, cycle in enumerate(scenario["cycles"], start=1):
                result = executor.execute_cycle(
                    state,
                    cycle["inputs"],
                    scenario["id"],
                    cycle_index,
                    cycle.get("blocking_runtime_fault", False),
                )
                self.assertEqual(
                    result["committed_state"],
                    cycle["expected"]["committed_state"],
                )
                self.assertEqual(
                    result["outputs"], cycle["expected"]["outputs"]
                )
                self.assertEqual(
                    result["normal_commit_inhibited"],
                    cycle["expected"]["normal_commit_inhibited"],
                )
                state = copy.deepcopy(result["committed_state"])

    def test_every_obligation_has_a_witness_scenario(self) -> None:
        targets = {
            target
            for scenario in self.scenarios["scenarios"]
            for target in scenario["target_obligations"]
        }
        expected = {
            item["id"] for item in self.obligations["obligations"]
        }
        self.assertEqual(targets, expected)

    def test_selected_mcdc_obligations_emit_paired_witnesses(self) -> None:
        mcdc_ids = {
            item["id"]
            for item in self.obligations["obligations"]
            if item["kind"] == "MCDC_PAIR"
        }
        counts = {obligation_id: 0 for obligation_id in mcdc_ids}
        for scenario in self.scenarios["scenarios"]:
            for target in scenario["target_obligations"]:
                if target in counts:
                    counts[target] += 1
        self.assertEqual(set(counts.values()), {2})

    def test_runtime_fault_obligations_are_synthesized(self) -> None:
        fault_kinds = {
            "FAULT_POLICY_ACTIVATED",
            "NORMAL_COMMIT_INHIBITED",
            "CONSERVATIVE_OUTPUT_APPLIED",
        }
        fault_ids = {
            item["id"]
            for item in self.obligations["obligations"]
            if item["kind"] in fault_kinds
        }
        witnesses = [
            scenario
            for scenario in self.scenarios["scenarios"]
            if fault_ids.intersection(scenario["target_obligations"])
        ]
        self.assertEqual(len(witnesses), 3)
        self.assertTrue(
            all(
                any(
                    cycle.get("blocking_runtime_fault") is True
                    for cycle in scenario["cycles"]
                )
                for scenario in witnesses
            )
        )

    def test_canonical_serialization_is_byte_identical(self) -> None:
        first = canonical_json_bytes((self.scenarios, self.report))
        second = canonical_json_bytes((self.scenarios, self.report))
        self.assertEqual(first, second)
        self.assertEqual(
            self.report["synthesis_run_id"],
            "SYNTH-RUN-56B9D26ABBC1",
        )

    def test_transition_evaluation_bound_is_enforced(self) -> None:
        source = (
            ROOT / "tools/assurance_test_synthesizer.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "transitions_explored >= maximum_transitions", source
        )
        self.assertIn(
            'bounds["maximum_transition_evaluations"]', source
        )

    def test_rejects_mismatched_program_identity(self) -> None:
        obligations = copy.deepcopy(self.obligations)
        obligations["program_id"] = "AIR-PROG-MISMATCH"
        with self.assertRaisesRegex(
            SynthesisError, "program identities do not match"
        ):
            synthesize(self.air, obligations, self.domain)

    def test_synthesizer_has_no_application_specific_logic(self) -> None:
        source = (
            ROOT / "tools/assurance_test_synthesizer.py"
        ).read_text(encoding="utf-8")
        forbidden = (
            "BatteryAHealthy",
            "BatteryBHealthy",
            "PayloadOvercurrent",
            "PowerSupervisor",
            "CommunicationLost",
            "ReturnToHome",
        )
        for name in forbidden:
            self.assertNotIn(name, source)


if __name__ == "__main__":
    unittest.main()
