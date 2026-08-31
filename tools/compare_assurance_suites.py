from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from aerost_tool import MUTATION_TAGS, mutate_air
from application_mutations import (
    ApplicationMutationError,
    build_mutated_airs,
    mutation_operator_map,
)
from assurance_test_synthesizer import (
    DecisionObservation,
    _is_mcdc_pair,
    _obligation_index,
    cycle_obligation_hits,
)
from external_air_executor import AirExecutor, ExecutorError


TOOL_NAME = "aerost-assurance-suite-comparison"
TOOL_VERSION = "0.2.0"


class ComparisonError(RuntimeError):
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
        raise ComparisonError(f"expected JSON object: {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def _scenario_list(document: Mapping[str, Any], label: str) -> list[dict[str, Any]]:
    scenarios = document.get("scenarios")
    if not isinstance(scenarios, list):
        raise ComparisonError(f"{label} has no scenarios array")
    copied: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in scenarios:
        if not isinstance(item, dict):
            raise ComparisonError(f"{label} contains a non-object scenario")
        scenario_id = item.get("id")
        if not isinstance(scenario_id, str) or not scenario_id:
            raise ComparisonError(f"{label} contains a scenario without a valid id")
        if scenario_id in seen:
            raise ComparisonError(f"duplicate scenario id in {label}: {scenario_id}")
        seen.add(scenario_id)
        copied.append(copy.deepcopy(item))
    return copied


def combine_manual_suites(
    requirements_document: Mapping[str, Any],
    closure_document: Mapping[str, Any],
) -> dict[str, Any]:
    requirements = _scenario_list(requirements_document, "manual requirements suite")
    closure = _scenario_list(closure_document, "manual closure supplement")
    combined = requirements + closure
    ids = [item["id"] for item in combined]
    if len(ids) != len(set(ids)):
        raise ComparisonError("manual requirements and closure suites have duplicate ids")
    return {"scenarios": combined}


@dataclass(frozen=True)
class ExecutedCycle:
    scenario_id: str
    cycle: int
    result: dict[str, Any]
    blocking_runtime_fault: bool


def _expected_projection(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "committed_state": copy.deepcopy(result["committed_state"]),
        "outputs": copy.deepcopy(result["outputs"]),
        "normal_commit_inhibited": bool(result["normal_commit_inhibited"]),
    }


def execute_suite(
    air: Mapping[str, Any],
    scenarios: Iterable[Mapping[str, Any]],
    *,
    verify_expected: bool,
    stop_on_first_mismatch: bool = False,
) -> tuple[list[ExecutedCycle], list[dict[str, Any]]]:
    executor = AirExecutor(dict(air))
    executed: list[ExecutedCycle] = []
    mismatches: list[dict[str, Any]] = []
    for scenario in scenarios:
        scenario_id = str(scenario["id"])
        initial_state = scenario.get("initial_state")
        retained = (
            copy.deepcopy(initial_state)
            if isinstance(initial_state, dict)
            else executor.initial_state()
        )
        cycles = scenario.get("cycles")
        if not isinstance(cycles, list) or not cycles:
            raise ComparisonError(f"scenario has no cycles: {scenario_id}")
        for cycle_index, cycle in enumerate(cycles):
            if not isinstance(cycle, dict):
                raise ComparisonError(f"invalid cycle in scenario: {scenario_id}")
            inputs = cycle.get("inputs")
            if not isinstance(inputs, dict):
                raise ComparisonError(f"cycle has no input object: {scenario_id}/{cycle_index}")
            blocking_fault = bool(cycle.get("blocking_runtime_fault", False))
            result = executor.execute_cycle(
                retained,
                inputs,
                scenario_id,
                cycle_index,
                blocking_fault,
            )
            if verify_expected:
                expected = cycle.get("expected")
                if not isinstance(expected, dict):
                    raise ComparisonError(
                        f"cycle has no expected result: {scenario_id}/{cycle_index}"
                    )
                actual_projection = _expected_projection(result)
                if actual_projection != expected:
                    mismatches.append(
                        {
                            "scenario_id": scenario_id,
                            "cycle": cycle_index,
                            "expected": copy.deepcopy(expected),
                            "actual": actual_projection,
                        }
                    )
                    if stop_on_first_mismatch:
                        return executed, mismatches
            executed.append(
                ExecutedCycle(
                    scenario_id=scenario_id,
                    cycle=cycle_index,
                    result=result,
                    blocking_runtime_fault=blocking_fault,
                )
            )
            retained = copy.deepcopy(result["committed_state"])
    return executed, mismatches


def _decision_observations(
    executed: Iterable[ExecutedCycle],
) -> dict[str, list[DecisionObservation]]:
    observations: dict[str, list[DecisionObservation]] = {}
    for item in executed:
        for decision in item.result.get("decisions", []):
            conditions = tuple(
                sorted(
                    (str(entry["id"]), bool(entry["value"]))
                    for entry in decision.get("conditions", [])
                )
            )
            observation = DecisionObservation(
                decision_id=str(decision["decision_id"]),
                result=bool(decision["result"]),
                conditions=conditions,
                path=(),
            )
            observations.setdefault(observation.decision_id, []).append(observation)
    return observations


def _mcdc_covered_ids(
    obligations_by_id: Mapping[str, Mapping[str, Any]],
    executed: Iterable[ExecutedCycle],
) -> set[str]:
    observations = _decision_observations(executed)
    covered: set[str] = set()
    for obligation_id, obligation in obligations_by_id.items():
        if obligation.get("kind") != "MCDC_PAIR":
            continue
        attributes = obligation.get("attributes", {})
        decision_id = attributes.get("decision_id")
        target_condition = attributes.get("condition_id")
        other_conditions = set(attributes.get("other_condition_ids", []))
        candidates = observations.get(str(decision_id), [])
        found = False
        for left_index, left in enumerate(candidates):
            for right in candidates[left_index + 1 :]:
                if _is_mcdc_pair(
                    left,
                    right,
                    str(target_condition),
                    {str(item) for item in other_conditions},
                ):
                    covered.add(obligation_id)
                    found = True
                    break
            if found:
                break
    return covered


def _covered_obligations(
    obligation_document: Mapping[str, Any],
    executed: list[ExecutedCycle],
) -> set[str]:
    obligations_by_id, event_map = _obligation_index(obligation_document)
    covered: set[str] = set()
    for item in executed:
        covered.update(
            cycle_obligation_hits(
                item.result,
                item.blocking_runtime_fault,
                obligations_by_id,
                event_map,
            )
        )
    covered.update(_mcdc_covered_ids(obligations_by_id, executed))
    return covered


def _kind_summary(
    obligation_document: Mapping[str, Any],
    covered_ids: set[str],
) -> dict[str, dict[str, Any]]:
    records = obligation_document["obligations"]
    kinds = sorted({str(record["kind"]) for record in records})
    summary: dict[str, dict[str, Any]] = {}
    for kind in kinds:
        ids = [str(record["id"]) for record in records if record["kind"] == kind]
        covered = sum(obligation_id in covered_ids for obligation_id in ids)
        total = len(ids)
        summary[kind] = {
            "covered": covered,
            "total": total,
            "percent": round((covered / total * 100.0) if total else 100.0, 6),
        }
    return summary


def _mutation_summary(
    air: Mapping[str, Any],
    scenarios: list[dict[str, Any]],
    mutation_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    if mutation_profile is None:
        mutated_airs = {
            f"MUT-{tag.upper()}": mutate_air(copy.deepcopy(dict(air)), tag)
            for tag in MUTATION_TAGS
        }
        operator_by_id = {f"MUT-{tag.upper()}": tag for tag in MUTATION_TAGS}
    else:
        mutated_airs = build_mutated_airs(air, mutation_profile)
        operator_by_id = mutation_operator_map(mutation_profile)
    for mutant_id, mutated_air in mutated_airs.items():
        tag = operator_by_id[mutant_id]
        _, mismatches = execute_suite(
            mutated_air,
            scenarios,
            verify_expected=True,
            stop_on_first_mismatch=True,
        )
        killed = bool(mismatches)
        detection = None
        if mismatches:
            detection = {
                "scenario_id": mismatches[0]["scenario_id"],
                "cycle": mismatches[0]["cycle"],
            }
        records.append(
            {
                "mutant_id": mutant_id,
                "operator": tag,
                "killed": killed,
                "first_detection": detection,
            }
        )
    killed_count = sum(bool(record["killed"]) for record in records)
    total = len(records)
    return {
        "killed": killed_count,
        "total": total,
        "score_percent": round((killed_count / total * 100.0) if total else 100.0, 6),
        "surviving": [record["mutant_id"] for record in records if not record["killed"]],
        "records": records,
    }


def analyze_suite(
    name: str,
    origin: str,
    air: Mapping[str, Any],
    obligation_document: Mapping[str, Any],
    suite_document: Mapping[str, Any],
    mutation_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    scenarios = _scenario_list(suite_document, name)
    executed, mismatches = execute_suite(
        air,
        scenarios,
        verify_expected=True,
    )
    if mismatches:
        first = mismatches[0]
        raise ComparisonError(
            f"suite replay mismatch in {name}: "
            f"{first['scenario_id']}/{first['cycle']}"
        )
    covered_ids = _covered_obligations(obligation_document, executed)
    all_ids = {str(item["id"]) for item in obligation_document["obligations"]}
    unknown = sorted(covered_ids - all_ids)
    if unknown:
        raise ComparisonError(f"suite produced unknown obligations: {unknown[:3]}")
    uncovered = sorted(all_ids - covered_ids)
    kinds = _kind_summary(obligation_document, covered_ids)
    mutation = _mutation_summary(air, scenarios, mutation_profile)
    requirements_kind = kinds.get("REQUIREMENT_EXERCISED", {"covered": 0, "total": 0})
    mcdc_kind = kinds.get("MCDC_PAIR", {"covered": 0, "total": 0})
    fault_kinds = {
        "FAULT_POLICY_ACTIVATED",
        "NORMAL_COMMIT_INHIBITED",
        "CONSERVATIVE_OUTPUT_APPLIED",
        "RESET_PATH_EXECUTED",
        "RECOVERY_PATH_EXECUTED",
    }
    fault_covered = sum(kinds.get(kind, {}).get("covered", 0) for kind in fault_kinds)
    fault_total = sum(kinds.get(kind, {}).get("total", 0) for kind in fault_kinds)
    return {
        "name": name,
        "origin": origin,
        "scenario_count": len(scenarios),
        "cycle_count": len(executed),
        "replay_passed": True,
        "obligations": {
            "covered": len(covered_ids),
            "total": len(all_ids),
            "percent": round(len(covered_ids) / len(all_ids) * 100.0, 6),
            "uncovered_count": len(uncovered),
            "uncovered_ids": uncovered,
            "by_kind": kinds,
        },
        "mcdc": {
            "covered": int(mcdc_kind["covered"]),
            "total": int(mcdc_kind["total"]),
            "complete": int(mcdc_kind["covered"]) == int(mcdc_kind["total"]),
        },
        "fault_reset_recovery": {
            "covered": fault_covered,
            "total": fault_total,
            "complete": fault_covered == fault_total,
        },
        "requirements": {
            "covered": int(requirements_kind["covered"]),
            "total": int(requirements_kind["total"]),
            "complete": int(requirements_kind["covered"]) == int(requirements_kind["total"]),
        },
        "mutation": mutation,
    }


def _delta(candidate: Mapping[str, Any], baseline: Mapping[str, Any]) -> dict[str, Any]:
    scenario_delta = int(candidate["scenario_count"]) - int(baseline["scenario_count"])
    cycle_delta = int(candidate["cycle_count"]) - int(baseline["cycle_count"])
    baseline_scenarios = int(baseline["scenario_count"])
    baseline_cycles = int(baseline["cycle_count"])
    return {
        "baseline": baseline["name"],
        "candidate": candidate["name"],
        "scenario_delta": scenario_delta,
        "cycle_delta": cycle_delta,
        "scenario_change_percent": round(
            scenario_delta / baseline_scenarios * 100.0 if baseline_scenarios else 0.0,
            6,
        ),
        "cycle_change_percent": round(
            cycle_delta / baseline_cycles * 100.0 if baseline_cycles else 0.0,
            6,
        ),
        "obligation_coverage_delta": int(candidate["obligations"]["covered"])
        - int(baseline["obligations"]["covered"]),
        "mcdc_delta": int(candidate["mcdc"]["covered"])
        - int(baseline["mcdc"]["covered"]),
        "mutation_kill_delta": int(candidate["mutation"]["killed"])
        - int(baseline["mutation"]["killed"]),
    }


def compare_suites(
    air: Mapping[str, Any],
    obligation_document: Mapping[str, Any],
    manual_requirements_document: Mapping[str, Any],
    manual_closure_supplement: Mapping[str, Any],
    automatic_unreduced_document: Mapping[str, Any],
    automatic_obligation_reduced_document: Mapping[str, Any],
    automatic_mutation_aware_reduced_document: Mapping[str, Any],
    mutation_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    program_id = air.get("semantic_identity") or air.get("program", {}).get("id")
    if not isinstance(program_id, str):
        raise ComparisonError("AIR has no semantic program identity")
    if obligation_document.get("program_id") != program_id:
        raise ComparisonError("AIR and obligation program identities do not match")
    for label, document in (
        ("automatic-unreduced", automatic_unreduced_document),
        ("automatic-obligation-reduced", automatic_obligation_reduced_document),
        ("automatic-mutation-aware-reduced", automatic_mutation_aware_reduced_document),
    ):
        if document.get("program_id") != program_id:
            raise ComparisonError(f"AIR and {label} program identities do not match")
    manual_closure_document = combine_manual_suites(
        manual_requirements_document,
        manual_closure_supplement,
    )
    inputs = [
        ("manual-requirements", "manually authored requirements suite", manual_requirements_document),
        ("manual-closure", "manual requirements plus coverage-closure supplement", manual_closure_document),
        ("automatic-unreduced", "bounded BFS per-obligation witness suite", automatic_unreduced_document),
        (
            "automatic-obligation-reduced",
            "deterministic obligation-preserving greedy multicover suite",
            automatic_obligation_reduced_document,
        ),
        (
            "automatic-mutation-aware-reduced",
            "deterministic obligation-and-mutation-preserving greedy multicover suite",
            automatic_mutation_aware_reduced_document,
        ),
    ]
    suites = [
        analyze_suite(
            name,
            origin,
            air,
            obligation_document,
            document,
            mutation_profile,
        )
        for name, origin, document in inputs
    ]
    by_name = {item["name"]: item for item in suites}
    replay_passed = all(item["replay_passed"] for item in suites)
    final_suite = by_name["automatic-mutation-aware-reduced"]
    final_automatic_complete = all(
        [
            final_suite["obligations"]["covered"] == final_suite["obligations"]["total"],
            final_suite["mcdc"]["covered"] == final_suite["mcdc"]["total"],
            final_suite["mutation"]["killed"] == final_suite["mutation"]["total"],
        ]
    )
    manual_suite_gate = bool(
        mutation_profile.get("manual_suite_gate", True)
        if mutation_profile is not None
        else True
    )
    manual_closure = by_name["manual-closure"]
    manual_suite_gate_passed = (
        not manual_suite_gate
        or manual_closure["mutation"]["killed"] == manual_closure["mutation"]["total"]
    )
    acceptance_passed = (
        replay_passed and final_automatic_complete and manual_suite_gate_passed
    )
    comparison_run_id = "COMPARE-RUN-" + canonical_digest(
        {
            "air": air,
            "obligations": obligation_document,
            "suites": [
                {"name": name, "document": document}
                for name, _, document in inputs
            ],
            "tool_version": TOOL_VERSION,
            "mutation_profile": mutation_profile,
        }
    )
    return {
        "schema_version": "AEROST-ASSURANCE-SUITE-COMPARISON-0.2",
        "profile_version": air.get("profile_version", "ASCP-0.2"),
        "program_id": program_id,
        "comparison_run_id": comparison_run_id,
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "suite_count": len(suites),
        "suites": suites,
        "comparisons": [
            _delta(by_name["manual-closure"], by_name["manual-requirements"]),
            _delta(by_name["automatic-unreduced"], by_name["manual-closure"]),
            _delta(
                by_name["automatic-obligation-reduced"],
                by_name["manual-closure"],
            ),
            _delta(
                by_name["automatic-mutation-aware-reduced"],
                by_name["manual-closure"],
            ),
            _delta(
                by_name["automatic-obligation-reduced"],
                by_name["automatic-unreduced"],
            ),
            _delta(
                by_name["automatic-mutation-aware-reduced"],
                by_name["automatic-unreduced"],
            ),
            _delta(
                by_name["automatic-mutation-aware-reduced"],
                by_name["automatic-obligation-reduced"],
            ),
        ],
        "claim_boundary": {
            "global_minimum_claimed": False,
            "bounded_completeness_claimed": True,
            "mutation_profile_driven": mutation_profile is not None,
            "mutation_scope": (
                f"{len(mutation_profile.get('mutations', []))} application-profile-controlled "
                "non-equivalent AIR mutants executed with the external AIR executor"
                if mutation_profile is not None
                else "eight controlled non-equivalent AIR mutants executed with the external AIR executor"
            ),
        },
        "deterministic": True,
        "replay_passed": replay_passed,
        "final_automatic_complete": final_automatic_complete,
        "manual_suite_gate": manual_suite_gate,
        "manual_suite_gate_passed": manual_suite_gate_passed,
        "acceptance_passed": acceptance_passed,
        "passed": acceptance_passed,
    }


def command_compare(args: argparse.Namespace) -> int:
    mutation_profile = (
        load_json(Path(args.mutation_profile))
        if args.mutation_profile
        else None
    )
    report = compare_suites(
        load_json(Path(args.air)),
        load_json(Path(args.obligations)),
        load_json(Path(args.manual_requirements)),
        load_json(Path(args.manual_closure)),
        load_json(Path(args.automatic_unreduced)),
        load_json(Path(args.automatic_obligation_reduced)),
        load_json(Path(args.automatic_mutation_aware_reduced)),
        mutation_profile=mutation_profile,
    )
    write_json(Path(args.output), report)
    print("assurance suite comparison " + ("PASS" if report["passed"] else "FAIL"))
    print(f"program: {report['program_id']}")
    for suite in report["suites"]:
        print(
            f"{suite['name']}: scenarios={suite['scenario_count']}, "
            f"cycles={suite['cycle_count']}, "
            f"obligations={suite['obligations']['covered']}/{suite['obligations']['total']}, "
            f"mcdc={suite['mcdc']['covered']}/{suite['mcdc']['total']}, "
            f"mutants={suite['mutation']['killed']}/{suite['mutation']['total']}"
        )
    return 0 if report["passed"] else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare manual and automatically synthesized assurance scenario suites."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("--air", required=True)
    compare_parser.add_argument("--obligations", required=True)
    compare_parser.add_argument("--manual-requirements", required=True)
    compare_parser.add_argument("--manual-closure", required=True)
    compare_parser.add_argument("--automatic-unreduced", required=True)
    compare_parser.add_argument("--automatic-obligation-reduced", required=True)
    compare_parser.add_argument("--automatic-mutation-aware-reduced", required=True)
    compare_parser.add_argument("--mutation-profile")
    compare_parser.add_argument("--output", required=True)
    compare_parser.set_defaults(func=command_compare)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ComparisonError, ApplicationMutationError, ExecutorError, OSError, ValueError, KeyError) as exc:
        print(f"assurance suite comparison FAIL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
