from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from aerost_tool import MUTATION_TAGS, mutate_air, validate_json_schema
from assurance_obligations import extract_obligations
from assurance_suite_reducer import reduce_suite as reduce_obligation_only
from assurance_test_synthesizer import synthesize
from external_air_executor import AirExecutor
from mutation_aware_suite_reducer import (
    MutationAwareReductionError,
    canonical_json_bytes,
    main,
    reduce_suite,
    scenario_detects_mutant,
)


ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class MutationAwareSuiteReducerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.air_path = ROOT / "artifacts/latest/power-supervisor.air.json"
        cls.air = load_json(cls.air_path)
        fault_policy = load_json(
            ROOT / "examples/power-supervisor/policies/runtime-fault-policy.json"
        )
        traceability = load_json(
            ROOT / "artifacts/latest/traceability-report.json"
        )
        domain = load_json(
            ROOT / "tests/input-domains/power-supervisor-input-domain.json"
        )
        cls.obligations = extract_obligations(
            air=cls.air,
            source_air_sha256=hashlib.sha256(
                cls.air_path.read_bytes()
            ).hexdigest(),
            fault_policy_document=fault_policy,
            traceability_document=traceability,
        )
        cls.unreduced, _ = synthesize(cls.air, cls.obligations, domain)
        cls.obligation_only, cls.obligation_only_report = (
            reduce_obligation_only(cls.unreduced, cls.obligations)
        )
        cls.reduced, cls.report = reduce_suite(
            cls.air,
            cls.unreduced,
            cls.obligations,
        )

    def test_controlled_mutation_aware_result(self) -> None:
        self.assertEqual(
            self.report["input_summary"],
            {
                "scenarios": 333,
                "cycles": 773,
                "obligations": 327,
                "required_obligation_witness_slots": 333,
                "controlled_mutants": 8,
                "total_required_slots": 341,
            },
        )
        self.assertEqual(
            self.report["output_summary"],
            {
                "selected_scenarios": 58,
                "selected_cycles": 146,
                "covered_obligations": 327,
                "covered_obligation_witness_slots": 333,
                "killed_mutants": 8,
                "scenario_reduction_percent": 82.582583,
                "cycle_reduction_percent": 81.112549,
            },
        )
        self.assertEqual(len(self.reduced["scenarios"]), 58)
        self.assertTrue(self.report["passed"])

    def test_adds_only_recovery_detecting_scenario_to_obligation_suite(self) -> None:
        obligation_ids = {
            item["id"] for item in self.obligation_only["scenarios"]
        }
        mutation_ids = {item["id"] for item in self.reduced["scenarios"]}
        self.assertEqual(
            mutation_ids - obligation_ids,
            {"SYN-247575E69D0B"},
        )
        self.assertEqual(obligation_ids - mutation_ids, set())
        added = next(
            item
            for item in self.reduced["scenarios"]
            if item["id"] == "SYN-247575E69D0B"
        )
        self.assertEqual(len(added["cycles"]), 4)

    def test_all_controlled_mutants_are_detected(self) -> None:
        summary = self.report["mutation_summary"]
        self.assertEqual(summary["killed"], 8)
        self.assertEqual(summary["total"], 8)
        self.assertEqual(summary["score_percent"], 100.0)
        self.assertEqual(summary["surviving"], [])
        self.assertEqual(
            set(summary["selected_detection_cases"]),
            {f"MUT-{tag.upper()}" for tag in MUTATION_TAGS},
        )
        self.assertTrue(
            all(summary["selected_detection_cases"].values())
        )

    def test_weaken_recovery_has_declared_detection_case(self) -> None:
        detections = self.report["mutation_summary"][
            "selected_detection_cases"
        ]["MUT-WEAKEN-RECOVERY"]
        self.assertEqual(
            detections,
            [{"scenario_id": "SYN-247575E69D0B", "cycle": 3}],
        )

    def test_obligation_and_mcdc_requirements_are_preserved(self) -> None:
        output = self.report["output_summary"]
        self.assertEqual(output["covered_obligations"], 327)
        self.assertEqual(output["covered_obligation_witness_slots"], 333)
        self.assertEqual(self.report["uncovered_obligations"], [])
        mcdc = self.report["mcdc_summary"]
        self.assertEqual(mcdc["obligations"], 6)
        self.assertEqual(set(mcdc["witness_counts"].values()), {2})

    def test_every_selected_cycle_replays(self) -> None:
        executor = AirExecutor(self.air)
        replayed = 0
        for scenario in self.reduced["scenarios"]:
            retained = copy.deepcopy(scenario["initial_state"])
            for cycle_index, cycle in enumerate(scenario["cycles"]):
                result = executor.execute_cycle(
                    retained,
                    cycle["inputs"],
                    scenario["id"],
                    cycle_index,
                    cycle.get("blocking_runtime_fault", False),
                )
                expected = cycle["expected"]
                self.assertEqual(
                    result["committed_state"], expected["committed_state"]
                )
                self.assertEqual(result["outputs"], expected["outputs"])
                self.assertEqual(
                    result["normal_commit_inhibited"],
                    expected["normal_commit_inhibited"],
                )
                retained = copy.deepcopy(result["committed_state"])
                replayed += 1
        self.assertEqual(replayed, 146)

    def test_mutation_detection_recomputes_from_selected_suite(self) -> None:
        scenarios = self.reduced["scenarios"]
        killed: set[str] = set()
        for tag in MUTATION_TAGS:
            mutant_id = f"MUT-{tag.upper()}"
            mutated = mutate_air(copy.deepcopy(self.air), tag)
            if any(
                scenario_detects_mutant(mutated, scenario)[0]
                for scenario in scenarios
            ):
                killed.add(mutant_id)
        self.assertEqual(
            killed,
            {f"MUT-{tag.upper()}" for tag in MUTATION_TAGS},
        )

    def test_outputs_validate_against_schemas(self) -> None:
        scenario_schema = load_json(
            ROOT / "schemas/synthesized-scenarios.schema.json"
        )
        report_schema = load_json(
            ROOT
            / "schemas/mutation-aware-suite-reduction-report.schema.json"
        )
        self.assertEqual(
            validate_json_schema(self.reduced, scenario_schema), []
        )
        self.assertEqual(
            validate_json_schema(self.report, report_schema), []
        )

    def test_reduction_is_byte_identical(self) -> None:
        second_reduced, second_report = reduce_suite(
            self.air,
            self.unreduced,
            self.obligations,
        )
        self.assertEqual(
            canonical_json_bytes(self.reduced),
            canonical_json_bytes(second_reduced),
        )
        self.assertEqual(
            canonical_json_bytes(self.report),
            canonical_json_bytes(second_report),
        )
        self.assertEqual(
            self.report["reduction_run_id"],
            "MUTATION-REDUCE-RUN-0D7BDA40E431",
        )

    def test_rejects_suite_that_cannot_kill_recovery_mutant(self) -> None:
        broken = copy.deepcopy(self.unreduced)
        broken["scenarios"] = [
            scenario
            for scenario in broken["scenarios"]
            if scenario["id"] != "SYN-247575E69D0B"
        ]
        with self.assertRaisesRegex(
            MutationAwareReductionError,
            "cannot kill controlled mutants: MUT-WEAKEN-RECOVERY",
        ):
            reduce_suite(self.air, broken, self.obligations)

    def test_rejects_program_identity_mismatch(self) -> None:
        broken = copy.deepcopy(self.obligations)
        broken["program_id"] = "AIR-PROG-MISMATCH"
        with self.assertRaisesRegex(
            MutationAwareReductionError,
            "AIR and obligation identities do not match",
        ):
            reduce_suite(self.air, self.unreduced, broken)

    def test_cli_writes_byte_identical_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            air_path = temp / "air.json"
            scenarios_path = temp / "scenarios.json"
            obligations_path = temp / "obligations.json"
            air_path.write_bytes(canonical_json_bytes(self.air))
            scenarios_path.write_bytes(canonical_json_bytes(self.unreduced))
            obligations_path.write_bytes(canonical_json_bytes(self.obligations))
            outputs = []
            for prefix in ("first", "second"):
                reduced_path = temp / f"{prefix}-reduced.json"
                report_path = temp / f"{prefix}-report.json"
                code = main(
                    [
                        "reduce",
                        "--air",
                        str(air_path),
                        "--scenarios",
                        str(scenarios_path),
                        "--obligations",
                        str(obligations_path),
                        "--output-scenarios",
                        str(reduced_path),
                        "--output-report",
                        str(report_path),
                    ]
                )
                self.assertEqual(code, 0)
                outputs.append((reduced_path.read_bytes(), report_path.read_bytes()))
            self.assertEqual(outputs[0], outputs[1])

    def test_source_has_no_application_specific_logic(self) -> None:
        source = (
            ROOT / "tools/mutation_aware_suite_reducer.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "BatteryAHealthy",
            "BatteryBHealthy",
            "PayloadOvercurrent",
            "PowerSupervisor",
            "CommunicationLost",
            "ReturnToHome",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
