from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from aerost_tool import MUTATION_TAGS, mutate_air
from application_mutations import (
    ApplicationMutationError,
    build_mutated_airs,
    mutation_operator_map,
)
from external_air_executor import AirExecutor, ExecutorError


ALGORITHM_NAME = "aerost-deterministic-mutation-aware-greedy-multicover"
ALGORITHM_VERSION = "0.1.0"


class MutationAwareReductionError(RuntimeError):
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
        raise MutationAwareReductionError(f"expected JSON object: {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def _expected_projection(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "committed_state": copy.deepcopy(result["committed_state"]),
        "outputs": copy.deepcopy(result["outputs"]),
        "normal_commit_inhibited": bool(result["normal_commit_inhibited"]),
    }


def scenario_detects_mutant(
    mutated_air: Mapping[str, Any],
    scenario: Mapping[str, Any],
) -> tuple[bool, dict[str, Any] | None]:
    executor = AirExecutor(dict(mutated_air))
    scenario_id = scenario.get("id")
    cycles = scenario.get("cycles")
    if not isinstance(scenario_id, str) or not isinstance(cycles, list) or not cycles:
        raise MutationAwareReductionError("scenario has invalid identity or cycles")
    initial_state = scenario.get("initial_state")
    retained = (
        copy.deepcopy(initial_state)
        if isinstance(initial_state, dict)
        else executor.initial_state()
    )
    for cycle_index, cycle in enumerate(cycles):
        if not isinstance(cycle, dict):
            raise MutationAwareReductionError(f"invalid cycle in {scenario_id}")
        inputs = cycle.get("inputs")
        expected = cycle.get("expected")
        if not isinstance(inputs, dict) or not isinstance(expected, dict):
            raise MutationAwareReductionError(
                f"scenario {scenario_id} has invalid inputs or expected result"
            )
        result = executor.execute_cycle(
            retained,
            inputs,
            scenario_id,
            cycle_index,
            bool(cycle.get("blocking_runtime_fault", False)),
        )
        actual = _expected_projection(result)
        if actual != expected:
            return True, {"scenario_id": scenario_id, "cycle": cycle_index}
        retained = copy.deepcopy(result["committed_state"])
    return False, None


def _obligation_requirements(
    obligation_document: Mapping[str, Any],
) -> tuple[dict[str, int], set[str]]:
    records = obligation_document.get("obligations")
    if not isinstance(records, list) or not records:
        raise MutationAwareReductionError("obligation document has no obligations")
    required: dict[str, int] = {}
    mcdc_ids: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise MutationAwareReductionError("obligation record must be an object")
        obligation_id = record.get("id")
        kind = record.get("kind")
        if not isinstance(obligation_id, str) or not isinstance(kind, str):
            raise MutationAwareReductionError("invalid obligation identity")
        if obligation_id in required:
            raise MutationAwareReductionError(f"duplicate obligation: {obligation_id}")
        required[obligation_id] = 2 if kind == "MCDC_PAIR" else 1
        if kind == "MCDC_PAIR":
            mcdc_ids.add(obligation_id)
    return required, mcdc_ids


def _scenario_obligation_coverage(
    scenario: Mapping[str, Any], declared_ids: set[str]
) -> frozenset[str]:
    scenario_id = scenario.get("id")
    cycles = scenario.get("cycles")
    targets = scenario.get("target_obligations")
    if not isinstance(scenario_id, str) or not isinstance(cycles, list) or not cycles:
        raise MutationAwareReductionError("scenario has invalid identity or cycles")
    if not isinstance(targets, list):
        raise MutationAwareReductionError(
            f"scenario {scenario_id} has no target obligations"
        )
    coverage = {item for item in targets if isinstance(item, str)}
    for cycle in cycles:
        if not isinstance(cycle, dict):
            raise MutationAwareReductionError(f"invalid cycle in {scenario_id}")
        hits = cycle.get("obligation_hits", [])
        if not isinstance(hits, list):
            raise MutationAwareReductionError(
                f"invalid obligation hits in {scenario_id}"
            )
        coverage.update(item for item in hits if isinstance(item, str))
    unknown = sorted(coverage - declared_ids)
    if unknown:
        raise MutationAwareReductionError(
            f"scenario {scenario_id} references unknown obligations: "
            + ", ".join(unknown)
        )
    return frozenset(coverage)


@dataclass(frozen=True)
class Candidate:
    scenario_id: str
    cycle_count: int
    obligations: frozenset[str]
    mutants: frozenset[str]
    detections: Mapping[str, dict[str, Any]]
    scenario: dict[str, Any]


def _remaining_gain(
    candidate: Candidate,
    obligation_counts: Mapping[str, int],
    obligation_required: Mapping[str, int],
    killed_mutants: set[str],
) -> tuple[int, int, int]:
    obligation_gain = sum(
        1
        for obligation_id in candidate.obligations
        if obligation_counts[obligation_id] < obligation_required[obligation_id]
    )
    mutation_gain = sum(
        1 for mutant_id in candidate.mutants if mutant_id not in killed_mutants
    )
    return obligation_gain + mutation_gain, obligation_gain, mutation_gain


def reduce_suite(
    air: dict[str, Any],
    scenario_document: dict[str, Any],
    obligation_document: dict[str, Any],
    mutation_profile: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    program_id = air.get("semantic_identity") or air.get("program", {}).get("id")
    if not isinstance(program_id, str):
        raise MutationAwareReductionError("AIR has no semantic program identity")
    if scenario_document.get("program_id") != program_id:
        raise MutationAwareReductionError("AIR and scenario identities do not match")
    if obligation_document.get("program_id") != program_id:
        raise MutationAwareReductionError("AIR and obligation identities do not match")

    scenarios = scenario_document.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise MutationAwareReductionError("scenario document has no scenarios")
    obligation_required, mcdc_ids = _obligation_requirements(obligation_document)
    declared_ids = set(obligation_required)

    if mutation_profile is None:
        mutated_airs = {
            f"MUT-{tag.upper()}": mutate_air(copy.deepcopy(air), tag)
            for tag in MUTATION_TAGS
        }
        operator_by_id = {f"MUT-{tag.upper()}": tag for tag in MUTATION_TAGS}
        mutation_scope = "eight controlled non-equivalent AIR mutants"
    else:
        mutated_airs = build_mutated_airs(air, mutation_profile)
        operator_by_id = mutation_operator_map(mutation_profile)
        mutation_scope = (
            f"{len(mutated_airs)} application-profile-controlled non-equivalent AIR mutants"
        )
    candidates: list[Candidate] = []
    seen_ids: set[str] = set()
    detection_matrix: dict[str, dict[str, dict[str, Any]]] = {
        mutant_id: {} for mutant_id in mutated_airs
    }
    for raw in scenarios:
        if not isinstance(raw, dict):
            raise MutationAwareReductionError("scenario must be an object")
        scenario_id = raw.get("id")
        cycles = raw.get("cycles")
        if not isinstance(scenario_id, str) or scenario_id in seen_ids:
            raise MutationAwareReductionError("scenario ids must be valid and unique")
        seen_ids.add(scenario_id)
        if not isinstance(cycles, list) or not cycles:
            raise MutationAwareReductionError(f"scenario {scenario_id} has no cycles")
        mutant_ids: set[str] = set()
        detections: dict[str, dict[str, Any]] = {}
        for mutant_id, mutated_air in mutated_airs.items():
            killed, detection = scenario_detects_mutant(mutated_air, raw)
            if killed and detection is not None:
                mutant_ids.add(mutant_id)
                detections[mutant_id] = detection
                detection_matrix[mutant_id][scenario_id] = detection
        candidates.append(
            Candidate(
                scenario_id=scenario_id,
                cycle_count=len(cycles),
                obligations=_scenario_obligation_coverage(raw, declared_ids),
                mutants=frozenset(mutant_ids),
                detections=detections,
                scenario=copy.deepcopy(raw),
            )
        )

    insufficient_obligations = sorted(
        obligation_id
        for obligation_id, required in obligation_required.items()
        if sum(obligation_id in candidate.obligations for candidate in candidates)
        < required
    )
    if insufficient_obligations:
        raise MutationAwareReductionError(
            "input suite cannot satisfy obligation multiplicity: "
            + ", ".join(insufficient_obligations)
        )
    undetected_mutants = sorted(
        mutant_id
        for mutant_id in mutated_airs
        if not any(mutant_id in candidate.mutants for candidate in candidates)
    )
    if undetected_mutants:
        raise MutationAwareReductionError(
            "input suite cannot kill controlled mutants: "
            + ", ".join(undetected_mutants)
        )

    obligation_counts = {item: 0 for item in obligation_required}
    killed_mutants: set[str] = set()
    remaining = {candidate.scenario_id: candidate for candidate in candidates}
    selected: list[Candidate] = []
    trace: list[dict[str, Any]] = []

    def incomplete() -> bool:
        return any(
            obligation_counts[item] < required
            for item, required in obligation_required.items()
        ) or len(killed_mutants) < len(mutated_airs)

    while incomplete():
        ranked: list[tuple[int, int, int, int, str, Candidate]] = []
        for candidate in remaining.values():
            total_gain, obligation_gain, mutation_gain = _remaining_gain(
                candidate,
                obligation_counts,
                obligation_required,
                killed_mutants,
            )
            if total_gain:
                ranked.append(
                    (
                        -total_gain,
                        -mutation_gain,
                        -obligation_gain,
                        candidate.cycle_count,
                        candidate.scenario_id,
                        candidate,
                    )
                )
        if not ranked:
            break
        ranked.sort(key=lambda item: item[:5])
        _, _, _, _, _, chosen = ranked[0]
        new_obligations = sorted(
            item
            for item in chosen.obligations
            if obligation_counts[item] < obligation_required[item]
        )
        new_mutants = sorted(chosen.mutants - killed_mutants)
        for item in new_obligations:
            obligation_counts[item] += 1
        killed_mutants.update(new_mutants)
        selected.append(chosen)
        remaining.pop(chosen.scenario_id)
        trace.append(
            {
                "selection_index": len(selected),
                "scenario_id": chosen.scenario_id,
                "cycle_count": chosen.cycle_count,
                "gain": len(new_obligations) + len(new_mutants),
                "obligation_gain": len(new_obligations),
                "mutation_gain": len(new_mutants),
                "newly_covered_obligations": new_obligations,
                "newly_killed_mutants": new_mutants,
                "covered_obligation_witness_slots_after_selection": sum(
                    min(obligation_counts[item], required)
                    for item, required in obligation_required.items()
                ),
                "killed_mutants_after_selection": len(killed_mutants),
            }
        )

    uncovered = sorted(
        item
        for item, required in obligation_required.items()
        if obligation_counts[item] < required
    )
    surviving = sorted(set(mutated_airs) - killed_mutants)
    selected_scenarios = sorted(
        (copy.deepcopy(candidate.scenario) for candidate in selected),
        key=lambda item: item["id"],
    )
    input_cycles = sum(candidate.cycle_count for candidate in candidates)
    selected_cycles = sum(candidate.cycle_count for candidate in selected)
    required_obligation_slots = sum(obligation_required.values())
    covered_obligation_slots = sum(
        min(obligation_counts[item], required)
        for item, required in obligation_required.items()
    )
    selected_detection: dict[str, list[dict[str, Any]]] = {}
    selected_ids = {candidate.scenario_id for candidate in selected}
    for mutant_id in sorted(mutated_airs):
        selected_detection[mutant_id] = [
            copy.deepcopy(detection_matrix[mutant_id][scenario_id])
            for scenario_id in sorted(detection_matrix[mutant_id])
            if scenario_id in selected_ids
        ]
    run_id = "MUTATION-REDUCE-RUN-" + canonical_digest(
        {
            "air": air,
            "scenarios": scenario_document,
            "obligations": obligation_document,
            "algorithm": ALGORITHM_VERSION,
        }
    )
    reduced = {
        "schema_version": scenario_document.get("schema_version"),
        "profile_version": scenario_document.get("profile_version"),
        "program_id": program_id,
        "synthesis_run_id": scenario_document.get("synthesis_run_id"),
        "origin": scenario_document.get("origin"),
        "scenarios": selected_scenarios,
    }
    report = {
        "schema_version": "AEROST-MUTATION-AWARE-SUITE-REDUCTION-REPORT-0.1",
        "profile_version": scenario_document.get("profile_version"),
        "program_id": program_id,
        "synthesis_run_id": scenario_document.get("synthesis_run_id"),
        "reduction_run_id": run_id,
        "algorithm": {
            "name": ALGORITHM_NAME,
            "version": ALGORITHM_VERSION,
            "selection": "maximum remaining obligation-and-mutation-slot gain",
            "tie_breaking": [
                "maximum mutation gain",
                "maximum obligation gain",
                "minimum cycle count",
                "lexicographically smallest scenario id",
            ],
        },
        "input_summary": {
            "scenarios": len(candidates),
            "cycles": input_cycles,
            "obligations": len(obligation_required),
            "required_obligation_witness_slots": required_obligation_slots,
            "controlled_mutants": len(mutated_airs),
            "total_required_slots": required_obligation_slots + len(mutated_airs),
        },
        "output_summary": {
            "selected_scenarios": len(selected),
            "selected_cycles": selected_cycles,
            "covered_obligations": len(obligation_required) - len(uncovered),
            "covered_obligation_witness_slots": covered_obligation_slots,
            "killed_mutants": len(killed_mutants),
            "scenario_reduction_percent": round(
                100.0 * (len(candidates) - len(selected)) / len(candidates), 6
            ),
            "cycle_reduction_percent": round(
                100.0 * (input_cycles - selected_cycles) / input_cycles, 6
            ),
        },
        "mcdc_summary": {
            "obligations": len(mcdc_ids),
            "required_witnesses_per_obligation": 2,
            "witness_counts": {
                item: obligation_counts[item] for item in sorted(mcdc_ids)
            },
        },
        "mutation_summary": {
            "killed": len(killed_mutants),
            "total": len(mutated_airs),
            "score_percent": round(
                len(killed_mutants) / len(mutated_airs) * 100.0, 6
            ),
            "surviving": surviving,
            "selected_detection_cases": selected_detection,
        },
        "selection_trace": trace,
        "uncovered_obligations": uncovered,
        "claim_boundary": {
            "global_minimum_claimed": False,
            "mutation_scope": mutation_scope,
            "mutation_profile_driven": mutation_profile is not None,
            "operators": {
                mutant_id: operator_by_id[mutant_id]
                for mutant_id in sorted(operator_by_id)
            },
        },
        "deterministic": True,
        "passed": not uncovered and not surviving,
    }
    return reduced, report


def command_reduce(args: argparse.Namespace) -> int:
    mutation_profile = (
        load_json(Path(args.mutation_profile))
        if args.mutation_profile
        else None
    )
    reduced, report = reduce_suite(
        load_json(Path(args.air)),
        load_json(Path(args.scenarios)),
        load_json(Path(args.obligations)),
        mutation_profile=mutation_profile,
    )
    write_json(Path(args.output_scenarios), reduced)
    write_json(Path(args.output_report), report)
    before = report["input_summary"]
    after = report["output_summary"]
    mutation = report["mutation_summary"]
    print(
        "mutation-aware assurance suite reduction "
        + ("PASS" if report["passed"] else "INCOMPLETE")
    )
    print(f"program: {report['program_id']}")
    print(f"scenarios: {before['scenarios']} -> {after['selected_scenarios']}")
    print(f"cycles: {before['cycles']} -> {after['selected_cycles']}")
    print(
        "obligations covered: "
        f"{after['covered_obligations']}/{before['obligations']}"
    )
    print(
        "obligation witness slots: "
        f"{after['covered_obligation_witness_slots']}/"
        f"{before['required_obligation_witness_slots']}"
    )
    print(f"mutants killed: {mutation['killed']}/{mutation['total']}")
    return 0 if report["passed"] else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reduce synthesized assurance scenarios while preserving obligation "
            "witness multiplicity and controlled mutation detection."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    reduce_parser = subparsers.add_parser("reduce")
    reduce_parser.add_argument("--air", required=True)
    reduce_parser.add_argument("--scenarios", required=True)
    reduce_parser.add_argument("--obligations", required=True)
    reduce_parser.add_argument("--mutation-profile")
    reduce_parser.add_argument("--output-scenarios", required=True)
    reduce_parser.add_argument("--output-report", required=True)
    reduce_parser.set_defaults(func=command_reduce)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (
        MutationAwareReductionError,
        ApplicationMutationError,
        ExecutorError,
        OSError,
        ValueError,
        KeyError,
    ) as exc:
        print(f"mutation-aware suite reduction FAIL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
