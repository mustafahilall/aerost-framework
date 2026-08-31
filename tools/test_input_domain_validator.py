from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "input_domain_validator.py"
SPEC = importlib.util.spec_from_file_location("input_domain_validator", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

AIR_PATH = ROOT / "artifacts" / "latest" / "power-supervisor.air.json"
DOMAIN_PATH = (
    ROOT
    / "tests"
    / "input-domains"
    / "power-supervisor-input-domain.json"
)


class InputDomainValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.air = json.loads(AIR_PATH.read_text(encoding="utf-8"))
        cls.domain = json.loads(DOMAIN_PATH.read_text(encoding="utf-8"))

    def test_power_domain_matches_every_air_input_in_order(self) -> None:
        summary = MODULE.validate_domain(self.air, self.domain)
        self.assertEqual(summary.input_count, 14)
        expected_names = [item["name"] for item in self.air["program"]["inputs"]]
        actual_names = [item["name"] for item in self.domain["inputs"]]
        self.assertEqual(actual_names, expected_names)

    def test_cartesian_domain_has_16384_vectors(self) -> None:
        summary = MODULE.validate_domain(self.air, self.domain)
        self.assertEqual(summary.input_vector_count, 16384)

    def test_vector_enumeration_is_deterministic(self) -> None:
        first = list(MODULE.enumerate_input_vectors(self.domain))
        second = list(MODULE.enumerate_input_vectors(self.domain))
        self.assertEqual(first, second)
        self.assertEqual(len(first), 16384)
        self.assertTrue(all(value is False for value in first[0].values()))
        self.assertTrue(all(value is True for value in first[-1].values()))

    def test_rejects_missing_air_input(self) -> None:
        domain = copy.deepcopy(self.domain)
        domain["inputs"].pop()
        with self.assertRaisesRegex(MODULE.InputDomainError, "AIR declares"):
            MODULE.validate_domain(self.air, domain)

    def test_rejects_reordered_input(self) -> None:
        domain = copy.deepcopy(self.domain)
        domain["inputs"][0], domain["inputs"][1] = (
            domain["inputs"][1],
            domain["inputs"][0],
        )
        with self.assertRaisesRegex(MODULE.InputDomainError, "order/name mismatch"):
            MODULE.validate_domain(self.air, domain)

    def test_rejects_non_boolean_value_for_bool_input(self) -> None:
        domain = copy.deepcopy(self.domain)
        domain["inputs"][0]["values"] = [False, 1]
        with self.assertRaisesRegex(MODULE.InputDomainError, "BOOL input value"):
            MODULE.validate_domain(self.air, domain)

    def test_rejects_noncanonical_boolean_order(self) -> None:
        domain = copy.deepcopy(self.domain)
        domain["inputs"][0]["values"] = [True, False]
        with self.assertRaisesRegex(MODULE.InputDomainError, "canonical"):
            MODULE.validate_domain(self.air, domain)

    def test_rejects_domain_larger_than_declared_limit(self) -> None:
        domain = copy.deepcopy(self.domain)
        domain["enumeration"]["maximum_input_vectors"] = 100
        with self.assertRaisesRegex(MODULE.InputDomainError, "exceeding"):
            MODULE.validate_domain(self.air, domain)

    def test_rejects_transition_bound_below_cartesian_state_estimate(self) -> None:
        domain = copy.deepcopy(self.domain)
        domain["search_bounds"]["maximum_transition_evaluations"] = 1000
        with self.assertRaisesRegex(MODULE.InputDomainError, "smaller"):
            MODULE.validate_domain(self.air, domain)

    def test_validator_has_no_application_specific_logic(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        forbidden = (
            "BatteryAHealthy",
            "BatteryBHealthy",
            "PowerSupervisor",
            "LinkDegraded",
            "CommunicationLost",
            "ReturnToHome",
        )
        for name in forbidden:
            self.assertNotIn(name, source)

    def test_cli_emits_byte_identical_validation_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.json"
            second = Path(temporary) / "second.json"
            command = [
                sys.executable,
                str(MODULE_PATH),
                "validate",
                "--air",
                str(AIR_PATH),
                "--domain",
                str(DOMAIN_PATH),
            ]
            subprocess.run(command + ["--output", str(first)], check=True)
            subprocess.run(command + ["--output", str(second)], check=True)
            self.assertEqual(first.read_bytes(), second.read_bytes())


if __name__ == "__main__":
    unittest.main()
