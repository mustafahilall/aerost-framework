from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "AEROST-INPUT-DOMAIN-0.1"
VALIDATION_VERSION = "AEROST-INPUT-DOMAIN-VALIDATION-0.1"


class InputDomainError(ValueError):
    """Raised when an input-domain document is invalid for its AIR program."""


@dataclass(frozen=True)
class ValidationSummary:
    program_id: str
    domain_id: str
    input_count: int
    input_vector_count: int
    maximum_sequence_depth: int
    maximum_reachable_states: int
    maximum_transition_evaluations: int
    estimated_full_state_transition_evaluations: int

    def to_document(self, *, air_sha256: str, domain_sha256: str) -> dict[str, Any]:
        return {
            "schema_version": VALIDATION_VERSION,
            "program_id": self.program_id,
            "domain_id": self.domain_id,
            "air_sha256": air_sha256,
            "domain_sha256": domain_sha256,
            "input_count": self.input_count,
            "input_vector_count": self.input_vector_count,
            "search_bounds": {
                "maximum_sequence_depth": self.maximum_sequence_depth,
                "maximum_reachable_states": self.maximum_reachable_states,
                "maximum_transition_evaluations": self.maximum_transition_evaluations,
            },
            "estimated_full_state_transition_evaluations": (
                self.estimated_full_state_transition_evaluations
            ),
            "passed": True,
        }


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputDomainError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InputDomainError(f"expected JSON object in {path}")
    return value


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _air_program(air: dict[str, Any]) -> dict[str, Any]:
    program = air.get("program")
    if not isinstance(program, dict):
        raise InputDomainError("AIR document is missing program object")
    return program


def _enum_members(program: dict[str, Any]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for item in program.get("types", []):
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        members = item.get("members")
        if isinstance(name, str) and isinstance(members, list):
            result[name] = {member for member in members if isinstance(member, str)}
    return result


def _validate_value(input_type: str, value: Any, enums: dict[str, set[str]]) -> None:
    if input_type == "BOOL":
        if not isinstance(value, bool):
            raise InputDomainError(
                f"BOOL input value must be true or false, got {value!r}"
            )
        return
    if input_type in enums:
        if not isinstance(value, str) or value not in enums[input_type]:
            raise InputDomainError(
                f"value {value!r} is not a member of enum {input_type}"
            )
        return
    if input_type in {"SINT", "INT", "DINT", "LINT", "USINT", "UINT", "UDINT", "ULINT"}:
        if isinstance(value, bool) or not isinstance(value, int):
            raise InputDomainError(
                f"integer input {input_type} requires integer values, got {value!r}"
            )
        return
    raise InputDomainError(f"unsupported AIR input type: {input_type}")


def validate_domain(air: dict[str, Any], domain: dict[str, Any]) -> ValidationSummary:
    if domain.get("schema_version") != SCHEMA_VERSION:
        raise InputDomainError(
            f"schema_version must be {SCHEMA_VERSION}"
        )
    if domain.get("profile_version") != air.get("profile_version"):
        raise InputDomainError("domain profile_version does not match AIR")

    program = _air_program(air)
    program_id = program.get("id")
    if not isinstance(program_id, str) or not program_id:
        raise InputDomainError("AIR program id is missing")
    if domain.get("program_id") != program_id:
        raise InputDomainError("domain program_id does not match AIR program id")

    domain_id = domain.get("domain_id")
    if not isinstance(domain_id, str) or not domain_id.startswith("AEROST-DOMAIN-"):
        raise InputDomainError("domain_id must start with AEROST-DOMAIN-")

    enumeration = domain.get("enumeration")
    if not isinstance(enumeration, dict):
        raise InputDomainError("enumeration object is required")
    if enumeration.get("strategy") != "cartesian":
        raise InputDomainError("only cartesian enumeration is supported in v0.1")
    if enumeration.get("ordering") != "air-declaration-order":
        raise InputDomainError("ordering must be air-declaration-order")
    maximum_input_vectors = enumeration.get("maximum_input_vectors")
    if not isinstance(maximum_input_vectors, int) or isinstance(maximum_input_vectors, bool):
        raise InputDomainError("maximum_input_vectors must be an integer")
    if maximum_input_vectors < 1:
        raise InputDomainError("maximum_input_vectors must be positive")

    if domain.get("initial_state_policy") != "air_initializers":
        raise InputDomainError("initial_state_policy must be air_initializers")

    constraints = domain.get("constraints")
    if constraints != []:
        raise InputDomainError("v0.1 requires an empty constraints array")

    air_inputs = program.get("inputs")
    domain_inputs = domain.get("inputs")
    if not isinstance(air_inputs, list) or not air_inputs:
        raise InputDomainError("AIR program has no input declarations")
    if not isinstance(domain_inputs, list) or not domain_inputs:
        raise InputDomainError("domain inputs array is required")
    if len(domain_inputs) != len(air_inputs):
        raise InputDomainError(
            f"domain defines {len(domain_inputs)} inputs but AIR declares {len(air_inputs)}"
        )

    enums = _enum_members(program)
    vector_count = 1
    seen_names: set[str] = set()

    for index, (air_input, domain_input) in enumerate(zip(air_inputs, domain_inputs)):
        if not isinstance(air_input, dict) or not isinstance(domain_input, dict):
            raise InputDomainError(f"invalid input record at index {index}")
        expected_name = air_input.get("name")
        expected_type = air_input.get("type")
        actual_name = domain_input.get("name")
        actual_type = domain_input.get("type")
        if actual_name != expected_name:
            raise InputDomainError(
                f"input order/name mismatch at index {index}: "
                f"expected {expected_name!r}, got {actual_name!r}"
            )
        if actual_name in seen_names:
            raise InputDomainError(f"duplicate input name: {actual_name}")
        seen_names.add(actual_name)
        if actual_type != expected_type:
            raise InputDomainError(
                f"type mismatch for {actual_name}: expected {expected_type}, got {actual_type}"
            )
        values = domain_input.get("values")
        if not isinstance(values, list) or not values:
            raise InputDomainError(f"input {actual_name} has an empty value domain")
        canonical_values = [json.dumps(value, sort_keys=True) for value in values]
        if len(set(canonical_values)) != len(canonical_values):
            raise InputDomainError(f"input {actual_name} contains duplicate values")
        for value in values:
            _validate_value(str(expected_type), value, enums)
        if expected_type == "BOOL" and values != [False, True]:
            raise InputDomainError(
                f"BOOL input {actual_name} must use canonical [false, true] ordering"
            )
        vector_count *= len(values)

    if vector_count > maximum_input_vectors:
        raise InputDomainError(
            f"Cartesian domain has {vector_count} vectors, exceeding "
            f"maximum_input_vectors={maximum_input_vectors}"
        )

    bounds = domain.get("search_bounds")
    if not isinstance(bounds, dict):
        raise InputDomainError("search_bounds object is required")
    bound_names = (
        "maximum_sequence_depth",
        "maximum_reachable_states",
        "maximum_transition_evaluations",
    )
    parsed_bounds: dict[str, int] = {}
    for name in bound_names:
        value = bounds.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise InputDomainError(f"{name} must be a positive integer")
        parsed_bounds[name] = value

    estimated = vector_count * parsed_bounds["maximum_reachable_states"]
    if estimated > parsed_bounds["maximum_transition_evaluations"]:
        raise InputDomainError(
            "maximum_transition_evaluations is smaller than the full Cartesian "
            "evaluation of maximum_reachable_states"
        )

    return ValidationSummary(
        program_id=program_id,
        domain_id=domain_id,
        input_count=len(air_inputs),
        input_vector_count=vector_count,
        maximum_sequence_depth=parsed_bounds["maximum_sequence_depth"],
        maximum_reachable_states=parsed_bounds["maximum_reachable_states"],
        maximum_transition_evaluations=parsed_bounds[
            "maximum_transition_evaluations"
        ],
        estimated_full_state_transition_evaluations=estimated,
    )


def enumerate_input_vectors(domain: dict[str, Any]) -> Iterable[dict[str, Any]]:
    inputs = domain["inputs"]
    names = [item["name"] for item in inputs]
    value_sets = [item["values"] for item in inputs]
    for values in itertools.product(*value_sets):
        yield dict(zip(names, values))


def command_validate(args: argparse.Namespace) -> int:
    air_path = Path(args.air)
    domain_path = Path(args.domain)
    air = load_json(air_path)
    domain = load_json(domain_path)
    summary = validate_domain(air, domain)
    report = summary.to_document(
        air_sha256=sha256_bytes(canonical_json_bytes(air)),
        domain_sha256=sha256_bytes(canonical_json_bytes(domain)),
    )
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(canonical_json_bytes(report))
    print("bounded input-domain validation PASS")
    print(f"program: {summary.program_id}")
    print(f"domain: {summary.domain_id}")
    print(f"inputs: {summary.input_count}")
    print(f"Cartesian input vectors: {summary.input_vector_count}")
    print(f"maximum sequence depth: {summary.maximum_sequence_depth}")
    print(
        "maximum reachable states: "
        f"{summary.maximum_reachable_states}"
    )
    print(
        "estimated full-state transition evaluations: "
        f"{summary.estimated_full_state_transition_evaluations}"
    )
    return 0


def command_enumerate(args: argparse.Namespace) -> int:
    domain = load_json(Path(args.domain))
    count = 0
    first: dict[str, Any] | None = None
    last: dict[str, Any] | None = None
    for vector in enumerate_input_vectors(domain):
        if first is None:
            first = vector
        last = vector
        count += 1
    print(f"input vectors: {count}")
    print("first: " + json.dumps(first, sort_keys=True))
    print("last: " + json.dumps(last, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate finite input domains against exported AEROST AIR."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--air", required=True)
    validate.add_argument("--domain", required=True)
    validate.add_argument("--output")
    validate.set_defaults(func=command_validate)

    enumerate_parser = subparsers.add_parser("enumerate")
    enumerate_parser.add_argument("--domain", required=True)
    enumerate_parser.set_defaults(func=command_enumerate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except InputDomainError as exc:
        print(f"input-domain validation FAIL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
