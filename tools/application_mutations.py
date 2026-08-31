from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


PROFILE_SCHEMA_VERSION = "AEROST-MUTATION-PROFILE-0.1"


class ApplicationMutationError(RuntimeError):
    pass


@dataclass(frozen=True)
class NodeContext:
    stage: str
    case_value: str | None
    node: dict[str, Any]


def load_mutation_profile(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApplicationMutationError(f"cannot read mutation profile {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ApplicationMutationError("mutation profile must be a JSON object")
    if value.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise ApplicationMutationError(
            f"mutation profile schema_version must be {PROFILE_SCHEMA_VERSION}"
        )
    if not isinstance(value.get("manual_suite_gate"), bool):
        raise ApplicationMutationError("mutation profile manual_suite_gate must be boolean")
    mutations = value.get("mutations")
    if not isinstance(mutations, list) or not mutations:
        raise ApplicationMutationError("mutation profile must contain mutations")
    seen: set[str] = set()
    for index, mutation in enumerate(mutations):
        if not isinstance(mutation, dict):
            raise ApplicationMutationError(f"mutations[{index}] must be an object")
        mutation_id = mutation.get("id")
        if not isinstance(mutation_id, str) or not mutation_id.startswith("MUT-"):
            raise ApplicationMutationError(f"mutations[{index}].id must start with MUT-")
        if mutation_id in seen:
            raise ApplicationMutationError(f"duplicate mutation id: {mutation_id}")
        seen.add(mutation_id)
        if not isinstance(mutation.get("operator"), str):
            raise ApplicationMutationError(f"mutations[{index}].operator must be a string")
        if not isinstance(mutation.get("operation"), dict):
            raise ApplicationMutationError(f"mutations[{index}].operation must be an object")
    return value


def _walk_statements(
    statements: Iterable[dict[str, Any]],
    *,
    stage: str,
    case_value: str | None = None,
) -> Iterable[NodeContext]:
    for statement in statements:
        yield NodeContext(stage=stage, case_value=case_value, node=statement)
        kind = statement.get("kind")
        if kind == "if":
            for branch in statement.get("branches", []):
                yield NodeContext(stage=stage, case_value=case_value, node=branch)
                yield from _walk_statements(
                    branch.get("body", []),
                    stage=stage,
                    case_value=case_value,
                )
            yield from _walk_statements(
                statement.get("else_body", []),
                stage=stage,
                case_value=case_value,
            )
        elif kind == "case":
            for arm in statement.get("arms", []):
                yield from _walk_statements(
                    arm.get("body", []),
                    stage=stage,
                    case_value=str(arm.get("value")),
                )


def iter_contexts(air: Mapping[str, Any]) -> list[NodeContext]:
    program = air.get("program")
    if not isinstance(program, dict):
        raise ApplicationMutationError("AIR has no program object")
    result: list[NodeContext] = []
    for stage_name, field_name in (
        ("transition", "transition_stage"),
        ("output", "output_stage"),
    ):
        statements = program.get(field_name)
        if not isinstance(statements, list):
            raise ApplicationMutationError(f"AIR program.{field_name} must be an array")
        result.extend(_walk_statements(statements, stage=stage_name))
    return result


def _expression_matches(expression: Any, selector: Mapping[str, Any]) -> bool:
    if not isinstance(expression, dict):
        return False
    if "expression_kind" in selector and expression.get("kind") != selector["expression_kind"]:
        return False
    if "expression_value" in selector and expression.get("value") != selector["expression_value"]:
        return False
    if "expression_operator" in selector and expression.get("operator") != selector["expression_operator"]:
        return False
    return True


def _matches(context: NodeContext, selector: Mapping[str, Any], node_kind: str) -> bool:
    node = context.node
    if node_kind == "decision":
        if "decision_id" not in node:
            return False
    elif node.get("kind") != node_kind:
        return False
    if "stage" in selector and context.stage != selector["stage"]:
        return False
    if "case_value" in selector and context.case_value != selector["case_value"]:
        return False
    if "mutation_tag" in selector and selector["mutation_tag"] not in node.get("mutation_tags", []):
        return False
    if "target" in selector and node.get("target") != selector["target"]:
        return False
    if "decision_id" in selector and node.get("decision_id") != selector["decision_id"]:
        return False
    if "statement_id" in selector and node.get("id") != selector["statement_id"]:
        return False
    if any(key.startswith("expression_") for key in selector):
        if not _expression_matches(node.get("expression"), selector):
            return False
    return True


def _select(
    air: Mapping[str, Any],
    selector: Mapping[str, Any],
    node_kind: str,
) -> list[NodeContext]:
    return [
        context
        for context in iter_contexts(air)
        if _matches(context, selector, node_kind)
    ]


def _require_count(
    matches: list[NodeContext],
    expectation: str,
    mutation_id: str,
) -> None:
    if expectation == "exactly-one" and len(matches) != 1:
        raise ApplicationMutationError(
            f"{mutation_id} expected exactly one match, found {len(matches)}"
        )
    if expectation == "at-least-one" and not matches:
        raise ApplicationMutationError(f"{mutation_id} did not match any AIR node")
    if expectation not in {"exactly-one", "at-least-one"}:
        raise ApplicationMutationError(
            f"{mutation_id} has unsupported match expectation: {expectation}"
        )


def _replacement_expression(spec: Mapping[str, Any], span: Any) -> dict[str, Any]:
    kind = spec.get("kind")
    if kind == "bool":
        value = spec.get("value")
        if not isinstance(value, bool):
            raise ApplicationMutationError("boolean replacement requires a boolean value")
        return {"kind": "bool", "type": "BOOL", "span": copy.deepcopy(span), "value": value}
    if kind == "name":
        value = spec.get("value")
        if not isinstance(value, str) or not value:
            raise ApplicationMutationError("name replacement requires a non-empty value")
        return {"kind": "name", "type": "BOOL", "span": copy.deepcopy(span), "value": value}
    raise ApplicationMutationError(f"unsupported replacement expression kind: {kind}")


def _change_state_target(
    statements: list[dict[str, Any]],
    *,
    target: str,
    from_value: str,
    to_value: str,
) -> int:
    changed = 0
    for statement in statements:
        kind = statement.get("kind")
        if kind == "assign":
            expression = statement.get("expression")
            if (
                statement.get("target") == target
                and isinstance(expression, dict)
                and expression.get("value") == from_value
            ):
                expression["value"] = to_value
                changed += 1
        elif kind == "if":
            for branch in statement.get("branches", []):
                changed += _change_state_target(
                    branch.get("body", []),
                    target=target,
                    from_value=from_value,
                    to_value=to_value,
                )
            changed += _change_state_target(
                statement.get("else_body", []),
                target=target,
                from_value=from_value,
                to_value=to_value,
            )
        elif kind == "case":
            for arm in statement.get("arms", []):
                changed += _change_state_target(
                    arm.get("body", []),
                    target=target,
                    from_value=from_value,
                    to_value=to_value,
                )
    return changed


def apply_mutation(
    original_air: Mapping[str, Any],
    mutation: Mapping[str, Any],
) -> dict[str, Any]:
    air = copy.deepcopy(dict(original_air))
    mutation_id = str(mutation["id"])
    operation = mutation["operation"]
    kind = operation.get("kind")
    expectation = str(operation.get("match", "exactly-one"))

    if kind == "replace-decision-binary-operator":
        matches = _select(air, operation.get("selector", {}), "decision")
        _require_count(matches, expectation, mutation_id)
        source = operation.get("from")
        target = operation.get("to")
        changed = 0
        for context in matches:
            expression = context.node.get("expression")
            if not isinstance(expression, dict) or expression.get("kind") != "binary":
                raise ApplicationMutationError(f"{mutation_id} matched a non-binary decision")
            if expression.get("operator") != source:
                raise ApplicationMutationError(
                    f"{mutation_id} expected operator {source}, got {expression.get('operator')}"
                )
            expression["operator"] = target
            changed += 1
    elif kind == "replace-decision-expression":
        matches = _select(air, operation.get("selector", {}), "decision")
        _require_count(matches, expectation, mutation_id)
        replacement = operation.get("replacement")
        if not isinstance(replacement, dict):
            raise ApplicationMutationError(f"{mutation_id} has no replacement expression")
        changed = 0
        for context in matches:
            current = context.node.get("expression")
            span = current.get("span") if isinstance(current, dict) else None
            context.node["expression"] = _replacement_expression(replacement, span)
            changed += 1
    elif kind == "negate-decision-expression":
        matches = _select(air, operation.get("selector", {}), "decision")
        _require_count(matches, expectation, mutation_id)
        changed = 0
        for context in matches:
            current = context.node.get("expression")
            if not isinstance(current, dict):
                raise ApplicationMutationError(f"{mutation_id} matched a decision without expression")
            context.node["expression"] = {
                "kind": "unary",
                "type": "BOOL",
                "span": copy.deepcopy(current.get("span")),
                "operator": "NOT",
                "operand": current,
            }
            changed += 1
    elif kind == "replace-state-target-in-decision-body":
        matches = _select(air, operation.get("selector", {}), "decision")
        _require_count(matches, expectation, mutation_id)
        changed = 0
        for context in matches:
            changed += _change_state_target(
                context.node.get("body", []),
                target=str(operation.get("target", "State")),
                from_value=str(operation["from"]),
                to_value=str(operation["to"]),
            )
        if changed == 0:
            raise ApplicationMutationError(f"{mutation_id} did not change a state target")
    elif kind == "set-assignment-boolean":
        matches = _select(air, operation.get("selector", {}), "assign")
        _require_count(matches, expectation, mutation_id)
        value = operation.get("value")
        if not isinstance(value, bool):
            raise ApplicationMutationError(f"{mutation_id} requires a boolean value")
        changed = 0
        for context in matches:
            expression = context.node.get("expression")
            span = expression.get("span") if isinstance(expression, dict) else context.node.get("span")
            context.node["expression"] = {
                "kind": "bool",
                "type": "BOOL",
                "span": copy.deepcopy(span),
                "value": value,
            }
            changed += 1
    elif kind == "swap-assignment-expressions":
        left = _select(air, operation.get("left_selector", {}), "assign")
        right = _select(air, operation.get("right_selector", {}), "assign")
        _require_count(left, "exactly-one", mutation_id + ":left")
        _require_count(right, "exactly-one", mutation_id + ":right")
        left[0].node["expression"], right[0].node["expression"] = (
            right[0].node["expression"],
            left[0].node["expression"],
        )
        changed = 2
    else:
        raise ApplicationMutationError(f"unsupported mutation operation: {kind}")

    if changed <= 0:
        raise ApplicationMutationError(f"mutation did not change AIR: {mutation_id}")
    air["mutation"] = {
        "id": mutation_id,
        "operator": mutation["operator"],
        "description": mutation.get("description", ""),
        "profile_driven": True,
    }
    return air


def build_mutated_airs(
    air: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    mutations = profile.get("mutations")
    if not isinstance(mutations, list) or not mutations:
        raise ApplicationMutationError("mutation profile contains no mutations")
    result: dict[str, dict[str, Any]] = {}
    for mutation in mutations:
        if not isinstance(mutation, dict):
            raise ApplicationMutationError("mutation record must be an object")
        mutation_id = str(mutation["id"])
        if mutation_id in result:
            raise ApplicationMutationError(f"duplicate mutation id: {mutation_id}")
        result[mutation_id] = apply_mutation(air, mutation)
    return result


def mutation_operator_map(profile: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(mutation["id"]): str(mutation["operator"])
        for mutation in profile.get("mutations", [])
        if isinstance(mutation, dict)
    }
