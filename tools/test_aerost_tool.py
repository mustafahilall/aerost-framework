from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import aerost_tool as tool


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "examples" / "power-supervisor" / "source" / "power-supervisor.ascp"


class AerostCompilerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source_text = SOURCE.read_text(encoding="utf-8")
        cls.program = tool.parse_source(cls.source_text)
        cls.typed = tool.semantic_analyze(cls.program)
        cls.policy_path = ROOT / "examples" / "power-supervisor" / "policies" / "runtime-fault-policy.json"
        cls.execution_contract = tool.load_execution_contract(cls.policy_path, cls.typed)
        cls.air = tool.lower_to_air(cls.typed, cls.execution_contract)
        cls.scenarios = tool.load_controlled_scenarios(ROOT)
        cls.results = tool.run_reference_scenarios(cls.air, cls.scenarios)

    def test_real_executable_ast_is_created(self) -> None:
        self.assertGreater(len(self.program.transition), 0)
        self.assertGreater(len(self.program.output), 0)
        self.assertTrue(any(stmt.kind == "case" for stmt in self.program.transition[0].branches[0][1]))

    def test_semantic_analyzer_resolves_all_names_and_types(self) -> None:
        self.assertEqual(len(self.typed.symbols), 25)
        for symbol in self.typed.symbols.values():
            self.assertIn(symbol.type_name, {"BOOL", "U16", "I32", "SupervisorState"})

    def test_semantic_analyzer_rejects_undefined_identifier(self) -> None:
        broken = self.source_text.replace("PayloadPermit := FALSE;", "UnknownOutput := FALSE;", 1)
        with self.assertRaises(tool.AerostError):
            tool.semantic_analyze(tool.parse_source(broken))

    def test_semantic_analyzer_rejects_non_exhaustive_case(self) -> None:
        broken = self.source_text.replace(
            "    LOCKOUT:\n        BlockingPowerFault := TRUE;\n        ImmediateLandingRequest := TRUE;\n",
            "",
            1,
        )
        with self.assertRaises(tool.AerostError):
            tool.semantic_analyze(tool.parse_source(broken))

    def test_air_is_derived_from_source(self) -> None:
        self.assertEqual(self.air["program"]["name"], "PowerSupervisor")
        nodes = tool.all_air_nodes(self.air)
        self.assertGreater(len(nodes), 100)
        self.assertTrue(all(node.get("source_id") for node in nodes))
        self.assertTrue(all((node.get("id") or node.get("decision_id")) for node in nodes))

    def test_generator_emits_independent_safe_rust(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "generated"
            source_map = tool.generate_rust_crate(self.air, output)
            lib = (output / "src" / "lib.rs").read_text(encoding="utf-8")
            cargo = (output / "Cargo.toml").read_text(encoding="utf-8")
            self.assertIn("[workspace]", cargo)
            self.assertIn("pub fn step", lib)
            self.assertNotIn("aerost_reference", lib)
            self.assertNotIn("execute_cycle", lib)
            self.assertTrue(source_map["mappings"])
            self.assertTrue(all(item.get("source_id") for item in source_map["mappings"]))

    def test_all_accepted_programs_generate_and_compile(self) -> None:
        if shutil.which("cargo") is None:
            self.skipTest("cargo is not available")
        accepted = sorted((ROOT / "conformance" / "accepted").glob("*.ascp"))
        self.assertGreater(len(accepted), 0)
        with tempfile.TemporaryDirectory() as tmp:
            for path in accepted:
                typed = tool.semantic_analyze(tool.parse_source(path.read_text(encoding="utf-8")))
                air = tool.lower_to_air(typed)
                generated = Path(tmp) / path.stem
                tool.generate_rust_crate(air, generated)
                binary = tool.compile_generated(generated)
                self.assertTrue(binary.exists(), path.name)

    def test_runtime_fault_policy_is_metadata_driven(self) -> None:
        source = """TYPE Mode : (IDLE, ACTIVE);
END_TYPE
PROGRAM NeutralPolicyDemo
VAR_INPUT
    Enable : BOOL;
END_VAR
VAR_OUTPUT
    ConservativeRequest : BOOL;
    ModeEcho : Mode;
END_VAR
VAR_STATE
    ModeMemory : Mode := IDLE;
    FaultMemory : BOOL := FALSE;
END_VAR
TRANSITION_STAGE
IF Enable THEN
    ModeMemory := ACTIVE;
END_IF;
OUTPUT_STAGE
ConservativeRequest := FALSE;
ModeEcho := ModeMemory;
END_PROGRAM
"""
        typed = tool.semantic_analyze(tool.parse_source(source))
        policy = {
            "schema_version": tool.SCHEMA_VERSION,
            "program": "NeutralPolicyDemo",
            "runtime_fault_policy": {
                "normal_commit_inhibited": True,
                "retained_assignments": [{"target": "FaultMemory", "value": True}],
                "output_assignments": [
                    {"target": "ConservativeRequest", "value": True},
                    {"target": "ModeEcho", "from_state": "ModeMemory"},
                ],
                "diagnostic": "DEMO-RUNTIME-FAULT",
            },
            "diagnostic_policy": {
                "state_transition": "DEMO-STATE-TRANSITION",
                "latched_flags": [
                    {
                        "state_variable": "FaultMemory",
                        "active_value": True,
                        "diagnostic": "DEMO-FAULT-LATCHED",
                    }
                ],
            },
        }
        air = tool.lower_to_air(typed, policy)
        result = tool.execute_cycle_air(
            air,
            tool.initial_state_from_air(air),
            {"Enable": True},
            "TEST-METADATA-POLICY",
            0,
            True,
        )
        self.assertTrue(result.normal_commit_inhibited)
        self.assertTrue(result.committed_state["FaultMemory"])
        self.assertTrue(result.outputs["ConservativeRequest"])
        self.assertEqual(result.outputs["ModeEcho"], "ACTIVE")
        self.assertIn("DEMO-RUNTIME-FAULT", result.trace.diagnostics)
        self.assertIn("DEMO-FAULT-LATCHED", result.trace.diagnostics)
        with tempfile.TemporaryDirectory() as tmp:
            generated = Path(tmp) / "generated"
            tool.generate_rust_crate(air, generated)
            lib = (generated / "src" / "lib.rs").read_text(encoding="utf-8")
            cargo = (generated / "Cargo.toml").read_text(encoding="utf-8")
            self.assertNotIn("BlockingFaultLatched", lib)
            self.assertNotIn("ShedNonessentialLoads", lib)
            self.assertIn("state.fault_memory = true;", lib)
            self.assertIn("outputs.conservative_request = true;", lib)
            self.assertIn('name = "aerost-generated-neutral-policy-demo"', cargo)
            if shutil.which("cargo") is not None:
                self.assertTrue(tool.compile_generated(generated).exists())

    def test_runtime_fault_policy_rejects_unknown_target(self) -> None:
        broken = tool.load_json(self.policy_path)
        broken["runtime_fault_policy"]["retained_assignments"][0]["target"] = "UnknownLatch"
        with self.assertRaises(tool.AerostError):
            tool.validate_execution_contract(broken, self.typed)

    def test_different_programs_generate_different_rust(self) -> None:
        accepted = sorted((ROOT / "conformance" / "accepted").glob("*.ascp"))
        hashes: set[str] = set()
        with tempfile.TemporaryDirectory() as tmp:
            for index, path in enumerate(accepted):
                typed = tool.semantic_analyze(tool.parse_source(path.read_text(encoding="utf-8")))
                air = tool.lower_to_air(typed)
                out = Path(tmp) / str(index)
                tool.generate_rust_crate(air, out)
                hashes.add(tool.sha256_file(out / "src" / "lib.rs"))
        self.assertEqual(len(hashes), len(accepted))

    def test_reference_matches_independent_frozen_oracle(self) -> None:
        report = tool.oracle_comparison_report(self.results, self.scenarios)
        self.assertTrue(report["passed"])
        self.assertEqual(report["mismatch_count"], 0)

    def test_claim_critical_coverage_is_closed(self) -> None:
        report = tool.coverage_report(self.air, self.results)
        self.assertTrue(report["claim_critical_closed"])
        self.assertEqual(report["claim_critical_open_count"], 0)


    def test_controlled_scenario_ids_are_unique(self) -> None:
        ids = [scenario["id"] for scenario in self.scenarios]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 48)

    def test_full_structural_coverage_is_closed(self) -> None:
        coverage = tool.coverage_report(self.air, self.results)
        self.assertTrue(coverage["complete"])
        self.assertEqual(coverage["uncovered_count"], 0)
        self.assertEqual(coverage["statement"]["reached"], coverage["statement"]["total"])
        self.assertEqual(coverage["decision"]["both_outcomes"], coverage["decision"]["total"])
        self.assertEqual(coverage["condition"]["both_outcomes"], coverage["condition"]["total"])

    def test_host_timing_parser_reports_every_path(self) -> None:
        sample = "\n".join([
            "BENCHCASE|TEST-A|0|100|10|20|21.50|30|40|50",
            "BENCHCASE|TEST-B|1|100|11|22|23.50|31|41|60",
            "BENCHTOTAL|2|100|1000|200|10|21|22.50|31|41|60|TEST-B|1|60",
        ])
        report = tool.parse_host_timing_output(sample)
        self.assertEqual(report["controlled_paths"], 2)
        self.assertEqual(report["samples_per_path"], 100)
        self.assertEqual(report["total_samples"], 200)
        self.assertEqual(len(report["paths"]), 2)
        self.assertEqual(report["worst_observed_path"]["scenario_id"], "TEST-B")
        self.assertEqual(report["aggregate"]["maximum_ns"], 60)

    def test_selected_mcdc_is_calculated_from_events(self) -> None:
        report = tool.mcdc_report(self.air, self.results)
        self.assertTrue(report["complete"])
        self.assertEqual(report["independence_pairs_found"], report["independence_pairs_required"])
        self.assertGreater(report["independence_pairs_found"], 0)

    def test_air_mutations_are_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = tool.mutation_report(self.air, self.scenarios, Path(tmp), False)
        self.assertEqual(report["detected"], len(tool.MUTATION_TAGS))
        self.assertEqual(report["surviving"], [])

    def test_traceability_boundaries_are_separate_and_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            generated = Path(tmp) / "generated"
            source_map = tool.generate_rust_crate(self.air, generated)
            req = tool.load_json(ROOT / "examples" / "power-supervisor" / "requirements" / "requirements.json")
            report = tool.traceability_report(self.air, source_map, req, self.scenarios, self.results)
        self.assertTrue(report["complete"])
        for boundary in (
            "requirement_to_source",
            "source_to_air",
            "air_to_backend",
            "requirement_to_test",
            "test_to_result",
        ):
            self.assertEqual(report[boundary]["percent"], 100.0)

    def test_schema_validation_rejects_missing_required_field(self) -> None:
        schema = {"type": "object", "required": ["x"], "properties": {"x": {"type": "integer"}}, "additionalProperties": False}
        self.assertTrue(tool.validate_json_schema({}, schema))
        self.assertEqual(tool.validate_json_schema({"x": 1}, schema), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
