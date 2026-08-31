from __future__ import annotations

import argparse
import copy
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ExecutorError(RuntimeError):
    pass


@dataclass
class DecisionEvent:
    decision_id: str
    result: bool
    conditions: list[tuple[str, bool]]
    scenario_id: str
    cycle: int

    def to_json(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "result": self.result,
            "conditions": [
                {"id": condition_id, "value": value}
                for condition_id, value in self.conditions
            ],
            "scenario_id": self.scenario_id,
            "cycle": self.cycle,
        }


@dataclass
class ExecutionTrace:
    statements: list[str] = field(default_factory=list)
    decisions: list[DecisionEvent] = field(default_factory=list)
    case_arms: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)


class AirExecutor:
    def __init__(self, air: dict[str, Any]) -> None:
        self.air = air
        self.program = self._require_object(air, "program")
        self.contract = self._require_object(air, "execution_contract")
        self.inputs = self._require_list(self.program, "inputs")
        self.outputs = self._require_list(self.program, "outputs")
        self.states = self._require_list(self.program, "states")
        self.types = self._require_list(self.program, "types")
        self.type_values = {
            item["name"]: list(item["values"])
            for item in self.types
        }
        self.enum_literals = {
            value
            for values in self.type_values.values()
            for value in values
        }
        self.input_types = {item["name"]: item["type"] for item in self.inputs}
        self.output_types = {item["name"]: item["type"] for item in self.outputs}
        self.state_types = {item["name"]: item["type"] for item in self.states}
        self._validate_declarations()

    @staticmethod
    def _require_object(parent: dict[str, Any], key: str) -> dict[str, Any]:
        value = parent.get(key)
        if not isinstance(value, dict):
            raise ExecutorError(f"AIR field {key!r} must be an object")
        return value

    @staticmethod
    def _require_list(parent: dict[str, Any], key: str) -> list[Any]:
        value = parent.get(key)
        if not isinstance(value, list):
            raise ExecutorError(f"AIR field {key!r} must be an array")
        return value

    def _validate_declarations(self) -> None:
        known_types = {"BOOL", "U16", "I32", *self.type_values.keys()}
        names: set[str] = set()
        for section_name, declarations in (
            ("inputs", self.inputs),
            ("outputs", self.outputs),
            ("states", self.states),
        ):
            for declaration in declarations:
                name = declaration.get("name")
                type_name = declaration.get("type")
                if not isinstance(name, str) or not name:
                    raise ExecutorError(f"invalid name in AIR {section_name}")
                if name in names:
                    raise ExecutorError(f"duplicate AIR declaration: {name}")
                names.add(name)
                if type_name not in known_types:
                    raise ExecutorError(
                        f"unsupported AIR type {type_name!r} for {name}"
                    )

    def parse_typed_value(self, text: str, type_name: str) -> Any:
        if type_name == "BOOL":
            if text in {"1", "true", "TRUE"}:
                return True
            if text in {"0", "false", "FALSE"}:
                return False
            raise ExecutorError(f"invalid BOOL protocol value: {text!r}")
        if type_name == "U16":
            value = int(text)
            if not 0 <= value <= 65535:
                raise ExecutorError(f"U16 value out of range: {value}")
            return value
        if type_name == "I32":
            value = int(text)
            if not -(2**31) <= value <= (2**31 - 1):
                raise ExecutorError(f"I32 value out of range: {value}")
            return value
        values = self.type_values.get(type_name)
        if values is None or text not in values:
            raise ExecutorError(
                f"invalid {type_name} protocol value: {text!r}"
            )
        return text

    def initial_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {}
        for declaration in self.states:
            if "initializer" not in declaration:
                raise ExecutorError(
                    f"state {declaration['name']} has no initializer"
                )
            state[declaration["name"]] = copy.deepcopy(
                declaration["initializer"]
            )
        return state

    def default_outputs(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for declaration in self.outputs:
            type_name = declaration["type"]
            if type_name == "BOOL":
                value: Any = False
            elif type_name in {"U16", "I32"}:
                value = 0
            else:
                enum_values = self.type_values[type_name]
                if not enum_values:
                    raise ExecutorError(f"empty enumeration type: {type_name}")
                value = enum_values[0]
            values[declaration["name"]] = value
        return values

    def eval_expression(
        self,
        expression: dict[str, Any],
        environment: dict[str, Any],
    ) -> Any:
        kind = expression.get("kind")
        if kind == "name":
            name = expression.get("value")
            if name in environment:
                return environment[name]
            if name in self.enum_literals:
                return name
            raise ExecutorError(f"unknown AIR name expression: {name!r}")
        if kind == "bool":
            value = expression.get("value")
            if not isinstance(value, bool):
                raise ExecutorError("AIR bool expression has non-boolean value")
            return value
        if kind == "int":
            value = expression.get("value")
            if not isinstance(value, int) or isinstance(value, bool):
                raise ExecutorError("AIR int expression has non-integer value")
            return value
        if kind == "unary":
            operand = self.eval_expression(expression["operand"], environment)
            operator = expression.get("operator")
            if operator == "NOT":
                return not bool(operand)
            if operator == "MINUS":
                return -int(operand)
            if operator == "PLUS":
                return int(operand)
            raise ExecutorError(f"unsupported AIR unary operator: {operator}")
        if kind != "binary":
            raise ExecutorError(f"unsupported AIR expression kind: {kind}")

        left = self.eval_expression(expression["left"], environment)
        right = self.eval_expression(expression["right"], environment)
        operator = expression.get("operator")
        operations = {
            "AND": lambda: bool(left) and bool(right),
            "OR": lambda: bool(left) or bool(right),
            "EQ": lambda: left == right,
            "NE": lambda: left != right,
            "LT": lambda: left < right,
            "LE": lambda: left <= right,
            "GT": lambda: left > right,
            "GE": lambda: left >= right,
        }
        operation = operations.get(operator)
        if operation is None:
            raise ExecutorError(f"unsupported AIR binary operator: {operator}")
        return operation()

    def execute_statements(
        self,
        statements: list[dict[str, Any]],
        environment: dict[str, Any],
        trace: ExecutionTrace,
        scenario_id: str,
        cycle: int,
    ) -> None:
        for statement in statements:
            statement_id = statement.get("id")
            if not isinstance(statement_id, str):
                raise ExecutorError("AIR statement is missing an id")
            trace.statements.append(statement_id)
            kind = statement.get("kind")
            if kind == "assign":
                target = statement["target"]
                if target not in environment:
                    raise ExecutorError(f"assignment to unknown target: {target}")
                environment[target] = self.eval_expression(
                    statement["expression"], environment
                )
                continue
            if kind == "if":
                taken = False
                for branch in statement["branches"]:
                    conditions = [
                        (
                            condition["id"],
                            bool(
                                self.eval_expression(
                                    condition["expression"], environment
                                )
                            ),
                        )
                        for condition in branch["conditions"]
                    ]
                    result = bool(
                        self.eval_expression(branch["expression"], environment)
                    )
                    trace.decisions.append(
                        DecisionEvent(
                            branch["decision_id"],
                            result,
                            conditions,
                            scenario_id,
                            cycle,
                        )
                    )
                    if result:
                        self.execute_statements(
                            branch["body"],
                            environment,
                            trace,
                            scenario_id,
                            cycle,
                        )
                        taken = True
                        break
                if not taken:
                    self.execute_statements(
                        statement["else_body"],
                        environment,
                        trace,
                        scenario_id,
                        cycle,
                    )
                continue
            if kind == "case":
                selector = self.eval_expression(
                    statement["selector"], environment
                )
                for arm in statement["arms"]:
                    if selector == arm["value"]:
                        trace.case_arms.append(
                            f"{statement_id}::{arm['value']}"
                        )
                        self.execute_statements(
                            arm["body"],
                            environment,
                            trace,
                            scenario_id,
                            cycle,
                        )
                        break
                else:
                    raise ExecutorError(
                        f"no CASE arm for {selector!r} in {statement_id}"
                    )
                continue
            raise ExecutorError(f"unsupported AIR statement kind: {kind}")

    def execute_cycle(
        self,
        retained: dict[str, Any],
        inputs: dict[str, Any],
        scenario_id: str,
        cycle: int,
        blocking_runtime_fault: bool,
    ) -> dict[str, Any]:
        missing_inputs = sorted(set(self.input_types) - set(inputs))
        extra_inputs = sorted(set(inputs) - set(self.input_types))
        if missing_inputs or extra_inputs:
            raise ExecutorError(
                f"input-set mismatch: missing={missing_inputs}, extra={extra_inputs}"
            )
        resident_state = copy.deepcopy(retained)
        working_state = copy.deepcopy(retained)
        outputs = self.default_outputs()
        environment = {**inputs, **working_state, **outputs}
        trace = ExecutionTrace()

        self.execute_statements(
            self.program["transition_stage"],
            environment,
            trace,
            scenario_id,
            cycle,
        )
        for name in working_state:
            working_state[name] = environment[name]
        environment.update(working_state)
        self.execute_statements(
            self.program["output_stage"],
            environment,
            trace,
            scenario_id,
            cycle,
        )
        for name in outputs:
            outputs[name] = environment[name]

        runtime_policy = self._require_object(
            self.contract, "runtime_fault_policy"
        )
        diagnostic_policy = self._require_object(
            self.contract, "diagnostic_policy"
        )
        normal_commit_inhibited = False
        if blocking_runtime_fault:
            normal_commit_inhibited = bool(
                runtime_policy["normal_commit_inhibited"]
            )
            self.apply_assignments(
                runtime_policy["retained_assignments"], working_state, working_state
            )
            self.apply_assignments(
                runtime_policy["output_assignments"], outputs, working_state
            )
            trace.diagnostics.append(runtime_policy["diagnostic"])

        if resident_state != working_state:
            trace.diagnostics.append(diagnostic_policy["state_transition"])
        for flag in diagnostic_policy["latched_flags"]:
            state_name = flag["state_variable"]
            if working_state[state_name] == flag["active_value"]:
                trace.diagnostics.append(flag["diagnostic"])

        return {
            "scenario_id": scenario_id,
            "cycle": cycle,
            "resident_state": resident_state,
            "committed_state": working_state,
            "outputs": outputs,
            "normal_commit_inhibited": normal_commit_inhibited,
            "statements": trace.statements,
            "decisions": [item.to_json() for item in trace.decisions],
            "case_arms": trace.case_arms,
            "diagnostics": trace.diagnostics,
        }

    @staticmethod
    def apply_assignments(
        assignments: list[dict[str, Any]],
        destination: dict[str, Any],
        state: dict[str, Any],
    ) -> None:
        for assignment in assignments:
            target = assignment["target"]
            if target not in destination:
                raise ExecutorError(
                    f"runtime policy targets unknown field: {target}"
                )
            has_value = "value" in assignment
            has_source = "from_state" in assignment
            if has_value == has_source:
                raise ExecutorError(
                    f"runtime assignment for {target} must use exactly one source"
                )
            if has_value:
                destination[target] = copy.deepcopy(assignment["value"])
            else:
                source = assignment["from_state"]
                if source not in state:
                    raise ExecutorError(
                        f"runtime assignment reads unknown state: {source}"
                    )
                destination[target] = copy.deepcopy(state[source])

    def run_protocol(self, protocol_path: Path) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        scenario_id: str | None = None
        retained: dict[str, Any] | None = None

        for line_number, raw_line in enumerate(
            protocol_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            record_type = parts[0]
            if record_type == "S":
                if len(parts) < 2:
                    raise ExecutorError(
                        f"protocol line {line_number}: malformed scenario record"
                    )
                scenario_id = parts[1]
                retained = self.initial_state()
                fields = self.parse_fields(parts[2:], line_number)
                missing = sorted(set(self.state_types) - set(fields))
                extra = sorted(set(fields) - set(self.state_types))
                if missing or extra:
                    raise ExecutorError(
                        f"protocol line {line_number}: state-set mismatch "
                        f"missing={missing}, extra={extra}"
                    )
                retained = {
                    name: self.parse_typed_value(fields[name], type_name)
                    for name, type_name in self.state_types.items()
                }
                continue
            if record_type == "C":
                if scenario_id is None or retained is None:
                    raise ExecutorError(
                        f"protocol line {line_number}: cycle before scenario"
                    )
                if len(parts) < 2:
                    raise ExecutorError(
                        f"protocol line {line_number}: malformed cycle record"
                    )
                cycle = int(parts[1])
                fields = self.parse_fields(parts[2:], line_number)
                runtime_text = fields.pop("BlockingRuntimeFault", "0")
                inputs = {
                    name: self.parse_typed_value(fields[name], type_name)
                    for name, type_name in self.input_types.items()
                    if name in fields
                }
                result = self.execute_cycle(
                    retained,
                    inputs,
                    scenario_id,
                    cycle,
                    self.parse_typed_value(runtime_text, "BOOL"),
                )
                results.append(result)
                retained = copy.deepcopy(result["committed_state"])
                continue
            raise ExecutorError(
                f"protocol line {line_number}: unknown record {record_type!r}"
            )
        if not results:
            raise ExecutorError("protocol contains no executable cycles")
        return results

    @staticmethod
    def parse_fields(parts: list[str], line_number: int) -> dict[str, str]:
        fields: dict[str, str] = {}
        for part in parts:
            if "=" not in part:
                raise ExecutorError(
                    f"protocol line {line_number}: expected key=value, got {part!r}"
                )
            key, value = part.split("=", 1)
            if not key or key in fields:
                raise ExecutorError(
                    f"protocol line {line_number}: duplicate or empty field {key!r}"
                )
            fields[key] = value
        return fields


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        value,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
        separators=(",", ": "),
    ) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def run(air_path: Path, protocol_path: Path, output_path: Path) -> None:
    air = load_json(air_path)
    executor = AirExecutor(air)
    cycles = executor.run_protocol(protocol_path)
    write_json(
        output_path,
        {
            "schema_version": air["schema_version"],
            "profile_version": air["profile_version"],
            "executor": {
                "name": "aerost-external-air-executor",
                "implementation": "standalone-python-standard-library",
                "imports_project_executor": False,
                "application_specific_logic": False,
            },
            "cycles": cycles,
        },
    )


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Standalone executor for exported AEROST AIR and protocol files"
    )
    parser.add_argument("--air", required=True, type=Path)
    parser.add_argument("--protocol", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        run(args.air.resolve(), args.protocol.resolve(), args.output.resolve())
        print(f"external AIR execution PASS: {args.output}")
        return 0
    except (ExecutorError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"EXTERNAL AIR EXECUTOR ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(cli_main())
