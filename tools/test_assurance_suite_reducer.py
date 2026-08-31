from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from aerost_tool import validate_json_schema
from assurance_obligations import extract_obligations
from assurance_suite_reducer import (
    ReductionError,
    canonical_json_bytes,
    main,
    reduce_suite,
)
from assurance_test_synthesizer import synthesize
from external_air_executor import AirExecutor


ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class AssuranceSuiteReducerTests(unittest.TestCase):
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
        cls.unreduced, cls.synthesis_report = synthesize(
            cls.air, cls.obligations, cls.domain
        )
        cls.reduced, cls.reduction_report = reduce_suite(
            cls.unreduced, cls.obligations
        )

    def test_power_suite_reduces_deterministically(self) -> None:
        self.assertEqual(
            self.reduction_report["input_summary"],
            {
                "scenarios": 333,
                "cycles": 773,
                "obligations": 327,
                "required_witness_slots": 333,
            },
        )
        self.assertEqual(
            self.reduction_report["output_summary"],
            {
                "selected_scenarios": 57,
                "selected_cycles": 142,
                "covered_obligations": 327,
                "covered_witness_slots": 333,
                "scenario_reduction_percent": 82.882883,
                "cycle_reduction_percent": 81.630013,
            },
        )
        self.assertEqual(len(self.reduced["scenarios"]), 57)
        self.assertTrue(self.reduction_report["passed"])

    def test_reduced_suite_preserves_all_obligation_witness_slots(self) -> None:
        self.assertEqual(
            self.reduction_report["output_summary"][
                "covered_witness_slots"
            ],
            self.reduction_report["input_summary"][
                "required_witness_slots"
            ],
        )
        self.assertEqual(
            self.reduction_report["uncovered_obligations"], []
        )

    def test_reduced_suite_preserves_two_witnesses_for_every_mcdc_pair(self) -> None:
        summary = self.reduction_report["mcdc_summary"]
        self.assertEqual(summary["obligations"], 6)
        self.assertEqual(summary["required_witnesses_per_obligation"], 2)
        self.assertEqual(summary["missing_witnesses"], [])
        self.assertEqual(set(summary["witness_counts"].values()), {2})

    def test_every_reduced_cycle_replays_to_expected_result(self) -> None:
        executor = AirExecutor(self.air)
        replayed_cycles = 0
        for scenario in self.reduced["scenarios"]:
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
                replayed_cycles += 1
        self.assertEqual(replayed_cycles, 142)

    def test_outputs_validate_against_declared_schemas(self) -> None:
        scenario_schema = load_json(
            ROOT / "schemas/synthesized-scenarios.schema.json"
        )
        report_schema = load_json(
            ROOT / "schemas/suite-reduction-report.schema.json"
        )
        self.assertEqual(
            validate_json_schema(self.reduced, scenario_schema), []
        )
        self.assertEqual(
            validate_json_schema(self.reduction_report, report_schema), []
        )

    def test_reduction_is_byte_identical(self) -> None:
        second_reduced, second_report = reduce_suite(
            self.unreduced, self.obligations
        )
        self.assertEqual(
            canonical_json_bytes(self.reduced),
            canonical_json_bytes(second_reduced),
        )
        self.assertEqual(
            canonical_json_bytes(self.reduction_report),
            canonical_json_bytes(second_report),
        )
        self.assertEqual(
            self.reduction_report["reduction_run_id"],
            "REDUCE-RUN-0DF9AD8FFDF5",
        )

    def test_selection_trace_uses_declared_tie_breaking(self) -> None:
        first = self.reduction_report["selection_trace"][0]
        self.assertEqual(first["scenario_id"], "SYN-63DF634F15D4")
        self.assertEqual(first["cycle_count"], 4)
        self.assertEqual(first["gain"], 103)
        source = (
            ROOT / "tools/assurance_suite_reducer.py"
        ).read_text(encoding="utf-8")
        self.assertIn("maximum remaining witness-slot gain", source)
        self.assertIn("candidate.cycle_count", source)
        self.assertIn("candidate.scenario_id", source)

    def test_rejects_program_identity_mismatch(self) -> None:
        obligations = copy.deepcopy(self.obligations)
        obligations["program_id"] = "AIR-PROG-MISMATCH"
        with self.assertRaisesRegex(
            ReductionError, "program identities do not match"
        ):
            reduce_suite(self.unreduced, obligations)

    def test_rejects_suite_without_second_mcdc_witness(self) -> None:
        broken = copy.deepcopy(self.unreduced)
        mcdc_id = next(
            item["id"]
            for item in self.obligations["obligations"]
            if item["kind"] == "MCDC_PAIR"
        )
        matching = [
            scenario
            for scenario in broken["scenarios"]
            if mcdc_id in scenario["target_obligations"]
        ]
        self.assertEqual(len(matching), 2)
        removed_id = matching[0]["id"]
        broken["scenarios"] = [
            scenario
            for scenario in broken["scenarios"]
            if scenario["id"] != removed_id
        ]
        with self.assertRaisesRegex(
            ReductionError,
            "cannot satisfy required witness multiplicity",
        ):
            reduce_suite(broken, self.obligations)

    def test_cli_writes_byte_identical_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            scenarios_path = temp / "unreduced.json"
            obligations_path = temp / "obligations.json"
            first_reduced = temp / "first-reduced.json"
            first_report = temp / "first-report.json"
            second_reduced = temp / "second-reduced.json"
            second_report = temp / "second-report.json"
            scenarios_path.write_bytes(canonical_json_bytes(self.unreduced))
            obligations_path.write_bytes(canonical_json_bytes(self.obligations))
            first_code = main(
                [
                    "reduce",
                    "--scenarios",
                    str(scenarios_path),
                    "--obligations",
                    str(obligations_path),
                    "--output-scenarios",
                    str(first_reduced),
                    "--output-report",
                    str(first_report),
                ]
            )
            second_code = main(
                [
                    "reduce",
                    "--scenarios",
                    str(scenarios_path),
                    "--obligations",
                    str(obligations_path),
                    "--output-scenarios",
                    str(second_reduced),
                    "--output-report",
                    str(second_report),
                ]
            )
            self.assertEqual(first_code, 0)
            self.assertEqual(second_code, 0)
            self.assertEqual(first_reduced.read_bytes(), second_reduced.read_bytes())
            self.assertEqual(first_report.read_bytes(), second_report.read_bytes())

    def test_reducer_has_no_application_specific_logic(self) -> None:
        source = (
            ROOT / "tools/assurance_suite_reducer.py"
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
