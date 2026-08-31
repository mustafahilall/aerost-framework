from __future__ import annotations

import ast
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import aerost_tool as tool


ROOT = Path(__file__).resolve().parents[1]
EXECUTOR = ROOT / "tools" / "external_air_executor.py"
AIR = ROOT / "artifacts" / "latest" / "power-supervisor.air.json"
PROTOCOL = ROOT / "artifacts" / "latest" / "controlled-protocol.txt"
REFERENCE = ROOT / "artifacts" / "latest" / "reference-traces.json"
BACKEND = ROOT / "artifacts" / "latest" / "backend-traces.json"
ALLOWED_IMPORT_ROOTS = {
    "argparse",
    "copy",
    "dataclasses",
    "json",
    "pathlib",
    "sys",
    "typing",
    "__future__",
}
OBSERVABLE_KEYS = {
    "scenario_id",
    "cycle",
    "committed_state",
    "outputs",
    "normal_commit_inhibited",
    "statements",
    "decisions",
    "case_arms",
    "diagnostics",
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def run_executor(air: Path, protocol: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(EXECUTOR),
            "--air",
            str(air),
            "--protocol",
            str(protocol),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def mutate_first_binary_operator(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("kind") == "binary" and "operator" in value:
            value["operator"] = "UNSUPPORTED_TEST_OPERATOR"
            return True
        return any(mutate_first_binary_operator(item) for item in value.values())
    if isinstance(value, list):
        return any(mutate_first_binary_operator(item) for item in value)
    return False


class ExternalAirExecutorTests(unittest.TestCase):
    def test_executor_uses_only_declared_standard_library_imports(self) -> None:
        source = EXECUTOR.read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported_roots.add((node.module or "").split(".", 1)[0])
        self.assertLessEqual(imported_roots, ALLOWED_IMPORT_ROOTS)
        self.assertNotIn("aerost_tool", source)
        self.assertNotIn("execute_cycle_air", source)

    def test_matches_reference_and_generated_rust_for_all_controlled_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "external-traces.json"
            completed = run_executor(AIR, PROTOCOL, output)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            external = load_json(output)

        reference = load_json(REFERENCE)
        backend = load_json(BACKEND)
        self.assertEqual(external["cycles"], reference["cycles"])
        external_observable = [
            {key: cycle[key] for key in OBSERVABLE_KEYS}
            for cycle in external["cycles"]
        ]
        self.assertEqual(external_observable, backend["cycles"])
        self.assertEqual(len(external["cycles"]), 53)


    def test_core_bundle_emits_authoritative_three_way_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "bundle"
            bundle = tool.build_core_bundle(ROOT, output, compile_backend=False)
            self.assertTrue(bundle["three_way"]["passed"])
            self.assertEqual(bundle["three_way"]["execution_path_count"], 3)
            self.assertEqual(bundle["three_way"]["mismatch_count"], 0)
            self.assertTrue(bundle["external_executor_independence"]["passed"])
            self.assertTrue((output / "external-executor-traces.json").exists())
            self.assertTrue((output / "three-way-differential-results.json").exists())
            self.assertTrue((output / "external-executor-independence-report.json").exists())

    def test_rejects_an_unsupported_air_operator(self) -> None:
        air = copy.deepcopy(load_json(AIR))
        self.assertTrue(mutate_first_binary_operator(air))
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            mutated_air = directory / "mutated.air.json"
            output = directory / "external-traces.json"
            mutated_air.write_text(
                json.dumps(air, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            completed = run_executor(mutated_air, PROTOCOL, output)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("unsupported AIR binary operator", completed.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
