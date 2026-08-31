from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from collections import deque
from itertools import chain
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from external_air_executor import AirExecutor, ExecutorError
from input_domain_validator import (
    InputDomainError,
    enumerate_input_vectors,
    validate_domain,
)


ALGORITHM_NAME = "aerost-bounded-assurance-bfs"
ALGORITHM_VERSION = "0.1.0"


class SynthesisError(RuntimeError):
    pass


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")


def canonical_digest(value: Any, length: int = 12) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()[:length].upper()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SynthesisError(f"expected JSON object: {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def canonical_state(state: Mapping[str, Any]) -> str:
    return json.dumps(state, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class PathCycle:
    inputs: dict[str, Any]
    blocking_runtime_fault: bool
    expected: dict[str, Any]
    obligation_hits: tuple[str, ...]
    decisions: tuple[dict[str, Any], ...]

    def to_document(self, extra_hits: Iterable[str] = ()) -> dict[str, Any]:
        hits = sorted(set(self.obligation_hits).union(extra_hits))
        return {
            "inputs": copy.deepcopy(self.inputs),
            "blocking_runtime_fault": self.blocking_runtime_fault,
            "expected": copy.deepcopy(self.expected),
            "obligation_hits": hits,
        }


@dataclass
class SearchNode:
    state: dict[str, Any]
    path: tuple[PathCycle, ...]


@dataclass(frozen=True)
class DecisionObservation:
    decision_id: str
    result: bool
    conditions: tuple[tuple[str, bool], ...]
    path: tuple[PathCycle, ...]


def _obligation_index(
    obligation_document: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[tuple[Any, ...], str]]:
    records = obligation_document.get("obligations")
    if not isinstance(records, list):
        raise SynthesisError("obligation document has no obligations array")
    by_id: dict[str, dict[str, Any]] = {}
    event_map: dict[tuple[Any, ...], str] = {}
    for raw in records:
        if not isinstance(raw, dict):
            raise SynthesisError("obligation record must be an object")
        obligation_id = raw.get("id")
        kind = raw.get("kind")
        if not isinstance(obligation_id, str) or not isinstance(kind, str):
            raise SynthesisError("obligation id and kind must be strings")
        if obligation_id in by_id:
            raise SynthesisError(f"duplicate obligation id: {obligation_id}")
        by_id[obligation_id] = raw
        attributes = raw.get("attributes", {})
        subjects = raw.get("subject_ids", [])
        if kind == "STATEMENT_REACHED" and subjects:
            event_map[("statement", subjects[0])] = obligation_id
        elif kind in {"DECISION_TRUE", "DECISION_FALSE"} and subjects:
            event_map[("decision", subjects[0], attributes.get("required_outcome"))] = obligation_id
        elif kind in {"CONDITION_TRUE", "CONDITION_FALSE"} and subjects:
            event_map[("condition", subjects[0], attributes.get("required_value"))] = obligation_id
        elif kind == "CASE_ARM_REACHED":
            event_map[("case", attributes.get("semantic_event_id"))] = obligation_id
        elif kind == "NORMAL_COMMIT_INHIBITED":
            event_map[("commit_inhibited", True)] = obligation_id
        elif kind == "FAULT_POLICY_ACTIVATED":
            event_map[("fault_policy", attributes.get("diagnostic"))] = obligation_id
        elif kind == "CONSERVATIVE_OUTPUT_APPLIED":
            event_map[("conservative", obligation_id)] = obligation_id
    return by_id, event_map


def _conservative_output_matches(
    result: Mapping[str, Any], obligation: Mapping[str, Any]
) -> bool:
    attributes = obligation.get("attributes", {})
    outputs = result.get("outputs", {})
    state = result.get("committed_state", {})
    for assignment in attributes.get("expected_output_assignments", []):
        target = assignment.get("target")
        if "value" in assignment:
            expected = assignment["value"]
        else:
            expected = state.get(assignment.get("from_state"))
        if outputs.get(target) != expected:
            return False
    for assignment in attributes.get("expected_retained_assignments", []):
        target = assignment.get("target")
        if "value" in assignment:
            expected = assignment["value"]
        else:
            expected = state.get(assignment.get("from_state"))
        if state.get(target) != expected:
            return False
    return True


def cycle_obligation_hits(
    result: Mapping[str, Any],
    blocking_runtime_fault: bool,
    obligations_by_id: Mapping[str, Mapping[str, Any]],
    event_map: Mapping[tuple[Any, ...], str],
) -> set[str]:
    hits: set[str] = set()
    for statement_id in result.get("statements", []):
        obligation_id = event_map.get(("statement", statement_id))
        if obligation_id:
            hits.add(obligation_id)
    for decision in result.get("decisions", []):
        decision_id = decision.get("decision_id")
        decision_result = decision.get("result")
        obligation_id = event_map.get(("decision", decision_id, decision_result))
        if obligation_id:
            hits.add(obligation_id)
        for condition in decision.get("conditions", []):
            condition_id = condition.get("id")
            value = condition.get("value")
            obligation_id = event_map.get(("condition", condition_id, value))
            if obligation_id:
                hits.add(obligation_id)
    for semantic_event_id in result.get("case_arms", []):
        obligation_id = event_map.get(("case", semantic_event_id))
        if obligation_id:
            hits.add(obligation_id)
    if result.get("normal_commit_inhibited") is True:
        obligation_id = event_map.get(("commit_inhibited", True))
        if obligation_id:
            hits.add(obligation_id)
    if blocking_runtime_fault:
        diagnostics = set(result.get("diagnostics", []))
        for obligation_id, obligation in obligations_by_id.items():
            if obligation.get("kind") == "FAULT_POLICY_ACTIVATED":
                diagnostic = obligation.get("attributes", {}).get("diagnostic")
                if diagnostic in diagnostics:
                    hits.add(obligation_id)
            elif obligation.get("kind") == "CONSERVATIVE_OUTPUT_APPLIED":
                if _conservative_output_matches(result, obligation):
                    hits.add(obligation_id)

    resident_state = result.get("resident_state", {})
    committed_state = result.get("committed_state", {})
    if isinstance(resident_state, Mapping) and isinstance(committed_state, Mapping):
        for obligation_id, obligation in obligations_by_id.items():
            if obligation.get("kind") not in {
                "RESET_PATH_EXECUTED",
                "RECOVERY_PATH_EXECUTED",
            }:
                continue
            attributes = obligation.get("attributes", {})
            state_variable = attributes.get("state_variable")
            from_values = attributes.get("from_values", [])
            to_value = attributes.get("to_value")
            retained_assignments = attributes.get("retained_assignments", [])
            if (
                isinstance(state_variable, str)
                and resident_state.get(state_variable) in from_values
                and committed_state.get(state_variable) == to_value
                and all(
                    isinstance(item, Mapping)
                    and committed_state.get(item.get("target")) == item.get("value")
                    for item in retained_assignments
                )
            ):
                hits.add(obligation_id)

    direct_requirements: set[str] = set()
    for obligation_id in tuple(hits):
        for requirement in obligations_by_id[obligation_id].get("requirements", []):
            direct_requirements.add(requirement)
    for obligation_id, obligation in obligations_by_id.items():
        if obligation.get("kind") != "REQUIREMENT_EXERCISED":
            continue
        subjects = obligation.get("subject_ids", [])
        if subjects and subjects[0] in direct_requirements:
            hits.add(obligation_id)
    return hits


def _decision_observations(path: tuple[PathCycle, ...]) -> list[DecisionObservation]:
    if not path:
        return []
    latest = path[-1]
    observations: list[DecisionObservation] = []
    for decision in latest.decisions:
        conditions = tuple(
            sorted(
                (str(item["id"]), bool(item["value"]))
                for item in decision.get("conditions", [])
            )
        )
        observations.append(
            DecisionObservation(
                decision_id=str(decision["decision_id"]),
                result=bool(decision["result"]),
                conditions=conditions,
                path=path,
            )
        )
    return observations


def _is_mcdc_pair(
    left: DecisionObservation,
    right: DecisionObservation,
    target_condition: str,
    other_conditions: set[str],
) -> bool:
    if left.decision_id != right.decision_id or left.result == right.result:
        return False
    left_map = dict(left.conditions)
    right_map = dict(right.conditions)
    if target_condition not in left_map or target_condition not in right_map:
        return False
    if left_map[target_condition] == right_map[target_condition]:
        return False
    for condition_id in other_conditions:
        if condition_id not in left_map or condition_id not in right_map:
            return False
        if left_map[condition_id] != right_map[condition_id]:
            return False
    return True


def _scenario_from_path(
    target_obligations: Sequence[str],
    path: tuple[PathCycle, ...],
    initial_state: Mapping[str, Any],
    requirements: Sequence[str],
    suffix: str = "",
) -> dict[str, Any]:
    target_list = sorted(set(target_obligations))
    digest = canonical_digest(
        {
            "targets": target_list,
            "path": [
                {
                    "inputs": cycle.inputs,
                    "fault": cycle.blocking_runtime_fault,
                    "expected": cycle.expected,
                }
                for cycle in path
            ],
            "suffix": suffix,
        }
    )
    scenario_id = f"SYN-{digest}{suffix}"
    cycles: list[dict[str, Any]] = []
    for index, cycle in enumerate(path):
        extras = target_list if index == len(path) - 1 else []
        cycles.append(cycle.to_document(extras))
    return {
        "id": scenario_id,
        "requirements": sorted(set(requirements)),
        "target_obligations": target_list,
        "initial_state": copy.deepcopy(dict(initial_state)),
        "cycles": cycles,
    }


def synthesize(
    air: dict[str, Any],
    obligation_document: dict[str, Any],
    domain: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        domain_summary = validate_domain(air, domain)
    except InputDomainError as exc:
        raise SynthesisError(str(exc)) from exc
    program_id = air.get("program", {}).get("id")
    if program_id != obligation_document.get("program_id"):
        raise SynthesisError("AIR and obligation program identities do not match")
    if program_id != domain.get("program_id"):
        raise SynthesisError("AIR and input-domain program identities do not match")
    obligations_by_id, event_map = _obligation_index(obligation_document)
    executor = AirExecutor(air)
    initial_state = executor.initial_state()
    input_vectors = list(enumerate_input_vectors(domain))
    if not input_vectors:
        raise SynthesisError("input domain produced no vectors")
    canonical_fault_inputs = copy.deepcopy(input_vectors[0])
    bounds = domain["search_bounds"]
    maximum_depth = int(bounds["maximum_sequence_depth"])
    maximum_states = int(bounds["maximum_reachable_states"])
    maximum_transitions = int(bounds["maximum_transition_evaluations"])

    queue: deque[SearchNode] = deque([SearchNode(initial_state, ())])
    seen_states = {canonical_state(initial_state)}
    witness_paths: dict[str, tuple[PathCycle, ...]] = {}
    decision_observations: dict[str, dict[tuple[Any, ...], DecisionObservation]] = {}
    states_explored = 0
    transitions_explored = 0
    state_limit_reached = False
    transition_limit_reached = False
    depth_frontier_remaining = False

    while queue:
        node = queue.popleft()
        states_explored += 1
        if len(node.path) >= maximum_depth:
            depth_frontier_remaining = True
            continue
        candidates = chain(
            ((vector, False) for vector in input_vectors),
            ((canonical_fault_inputs, True),),
        )
        for inputs, blocking_runtime_fault in candidates:
            if transitions_explored >= maximum_transitions:
                transition_limit_reached = True
                queue.clear()
                break
            transitions_explored += 1
            try:
                result = executor.execute_cycle(
                    node.state,
                    inputs,
                    "SYNTH-SEARCH",
                    len(node.path) + 1,
                    blocking_runtime_fault,
                )
            except ExecutorError as exc:
                raise SynthesisError(f"AIR execution failed during synthesis: {exc}") from exc
            hits = cycle_obligation_hits(
                result,
                blocking_runtime_fault,
                obligations_by_id,
                event_map,
            )
            expected = {
                "committed_state": copy.deepcopy(result["committed_state"]),
                "outputs": copy.deepcopy(result["outputs"]),
                "normal_commit_inhibited": bool(result["normal_commit_inhibited"]),
            }
            cycle = PathCycle(
                inputs=copy.deepcopy(inputs),
                blocking_runtime_fault=blocking_runtime_fault,
                expected=expected,
                obligation_hits=tuple(sorted(hits)),
                decisions=tuple(copy.deepcopy(result.get("decisions", []))),
            )
            path = node.path + (cycle,)
            for obligation_id in sorted(hits):
                witness_paths.setdefault(obligation_id, path)
            for observation in _decision_observations(path):
                signature = (observation.result, observation.conditions)
                decision_observations.setdefault(observation.decision_id, {}).setdefault(
                    signature, observation
                )
            next_state = copy.deepcopy(result["committed_state"])
            next_key = canonical_state(next_state)
            if next_key not in seen_states and len(path) < maximum_depth:
                if len(seen_states) < maximum_states:
                    seen_states.add(next_key)
                    queue.append(SearchNode(next_state, path))
                else:
                    state_limit_reached = True

    mcdc_pairs: dict[str, tuple[tuple[PathCycle, ...], tuple[PathCycle, ...]]] = {}
    for obligation_id, obligation in sorted(obligations_by_id.items()):
        if obligation.get("kind") != "MCDC_PAIR":
            continue
        attributes = obligation.get("attributes", {})
        decision_id = attributes.get("decision_id")
        target_condition = attributes.get("condition_id")
        other_conditions = set(attributes.get("other_condition_ids", []))
        observations = list(decision_observations.get(decision_id, {}).values())
        found: tuple[DecisionObservation, DecisionObservation] | None = None
        for left_index, left in enumerate(observations):
            for right in observations[left_index + 1 :]:
                if _is_mcdc_pair(left, right, target_condition, other_conditions):
                    found = (left, right)
                    break
            if found:
                break
        if found:
            mcdc_pairs[obligation_id] = (found[0].path, found[1].path)

    scenarios: list[dict[str, Any]] = []
    covered: set[str] = set(witness_paths)
    for obligation_id, path in sorted(witness_paths.items()):
        obligation = obligations_by_id[obligation_id]
        scenarios.append(
            _scenario_from_path(
                [obligation_id],
                path,
                initial_state,
                obligation.get("requirements", []),
            )
        )
    for obligation_id, (left_path, right_path) in sorted(mcdc_pairs.items()):
        obligation = obligations_by_id[obligation_id]
        requirements = obligation.get("requirements", [])
        scenarios.append(
            _scenario_from_path(
                [obligation_id], left_path, initial_state, requirements, "-A"
            )
        )
        scenarios.append(
            _scenario_from_path(
                [obligation_id], right_path, initial_state, requirements, "-B"
            )
        )
        covered.add(obligation_id)
    scenarios.sort(key=lambda item: item["id"])
    all_ids = set(obligations_by_id)
    uncovered = sorted(all_ids - covered)
    synthesis_run_id = "SYNTH-RUN-" + canonical_digest(
        {
            "air": air,
            "obligations": obligation_document,
            "domain": domain,
            "algorithm": ALGORITHM_VERSION,
        }
    )
    scenario_document = {
        "schema_version": "AEROST-SYNTHESIZED-SCENARIOS-0.1",
        "profile_version": air.get("profile_version", "ASCP-0.2"),
        "program_id": program_id,
        "synthesis_run_id": synthesis_run_id,
        "origin": "automatic-assurance-test-synthesis",
        "scenarios": scenarios,
    }
    selected_cycles = sum(len(item["cycles"]) for item in scenarios)
    search_truncated = state_limit_reached or transition_limit_reached
    if transition_limit_reached:
        termination_reason = "transition-limit-reached"
    elif state_limit_reached:
        termination_reason = "state-limit-reached"
    elif depth_frontier_remaining:
        termination_reason = "bounded-depth-exhausted"
    else:
        termination_reason = "reachable-frontier-exhausted"

    report = {
        "schema_version": "AEROST-SYNTHESIS-REPORT-0.1",
        "profile_version": air.get("profile_version", "ASCP-0.2"),
        "program_id": program_id,
        "synthesis_run_id": synthesis_run_id,
        "algorithm": {
            "name": ALGORITHM_NAME,
            "version": ALGORITHM_VERSION,
            "exploration": "bounded breadth-first retained-state exploration",
            "reduction": "none; unreduced per-obligation witness suite",
        },
        "bounds": {
            "maximum_sequence_depth": maximum_depth,
            "maximum_explored_states": maximum_states,
            "maximum_transition_evaluations": maximum_transitions,
        },
        "obligation_summary": {
            "total": len(all_ids),
            "reachable": len(covered),
            "covered": len(covered),
            "uncovered": len(uncovered),
            "unreachable_within_bound": 0 if search_truncated else len(uncovered),
            "unresolved_due_to_resource_limit": len(uncovered) if search_truncated else 0,
        },
        "search_summary": {
            "states_explored": states_explored,
            "transitions_explored": transitions_explored,
            "candidate_sequences": len(scenarios),
            "search_complete": not search_truncated,
            "termination_reason": termination_reason,
            "state_limit_reached": state_limit_reached,
            "transition_limit_reached": transition_limit_reached,
            "depth_frontier_remaining": depth_frontier_remaining,
        },
        "suite_summary": {
            "selected_scenarios": len(scenarios),
            "selected_cycles": selected_cycles,
            "covered_obligations": len(covered),
        },
        "uncovered_obligations": uncovered,
        "deterministic": True,
        "passed": not uncovered and not search_truncated,
    }
    return scenario_document, report


def command_synthesize(args: argparse.Namespace) -> int:
    air = load_json(Path(args.air))
    obligations = load_json(Path(args.obligations))
    domain = load_json(Path(args.input_domain))
    scenarios, report = synthesize(air, obligations, domain)
    write_json(Path(args.output_scenarios), scenarios)
    write_json(Path(args.output_report), report)
    summary = report["obligation_summary"]
    search = report["search_summary"]
    suite = report["suite_summary"]
    print("bounded assurance scenario synthesis " + ("PASS" if report["passed"] else "INCOMPLETE"))
    print(f"program: {report['program_id']}")
    print(f"states explored: {search['states_explored']}")
    print(f"transitions explored: {search['transitions_explored']}")
    print(f"obligations covered: {summary['covered']}/{summary['total']}")
    print(f"witness scenarios: {suite['selected_scenarios']}")
    print(f"witness cycles: {suite['selected_cycles']}")
    if report["uncovered_obligations"]:
        print(f"uncovered obligations: {len(report['uncovered_obligations'])}")
    return 0 if report["passed"] else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Synthesize bounded stateful assurance witness sequences from AIR."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    synthesize_parser = subparsers.add_parser("synthesize")
    synthesize_parser.add_argument("--air", required=True)
    synthesize_parser.add_argument("--obligations", required=True)
    synthesize_parser.add_argument("--input-domain", required=True)
    synthesize_parser.add_argument("--output-scenarios", required=True)
    synthesize_parser.add_argument("--output-report", required=True)
    synthesize_parser.set_defaults(func=command_synthesize)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (SynthesisError, ExecutorError, InputDomainError, OSError, ValueError) as exc:
        print(f"assurance synthesis FAIL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
