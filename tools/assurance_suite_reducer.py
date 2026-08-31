from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


ALGORITHM_NAME = "aerost-deterministic-greedy-multicover"
ALGORITHM_VERSION = "0.1.0"


class ReductionError(RuntimeError):
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
        raise ReductionError(f"expected JSON object: {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


@dataclass(frozen=True)
class Candidate:
    scenario_id: str
    cycle_count: int
    coverage: frozenset[str]
    scenario: dict[str, Any]


def _obligation_requirements(
    obligation_document: Mapping[str, Any],
) -> tuple[dict[str, int], set[str]]:
    records = obligation_document.get("obligations")
    if not isinstance(records, list):
        raise ReductionError("obligation document has no obligations array")
    required_counts: dict[str, int] = {}
    mcdc_ids: set[str] = set()
    for raw in records:
        if not isinstance(raw, dict):
            raise ReductionError("obligation record must be an object")
        obligation_id = raw.get("id")
        kind = raw.get("kind")
        if not isinstance(obligation_id, str) or not isinstance(kind, str):
            raise ReductionError("obligation id and kind must be strings")
        if obligation_id in required_counts:
            raise ReductionError(f"duplicate obligation id: {obligation_id}")
        required_counts[obligation_id] = 2 if kind == "MCDC_PAIR" else 1
        if kind == "MCDC_PAIR":
            mcdc_ids.add(obligation_id)
    if not required_counts:
        raise ReductionError("obligation document is empty")
    return required_counts, mcdc_ids


def _candidate_from_scenario(
    scenario: Mapping[str, Any],
    declared_ids: set[str],
) -> Candidate:
    scenario_id = scenario.get("id")
    cycles = scenario.get("cycles")
    targets = scenario.get("target_obligations")
    if not isinstance(scenario_id, str):
        raise ReductionError("scenario id must be a string")
    if not isinstance(cycles, list) or not cycles:
        raise ReductionError(f"scenario {scenario_id} has no cycles")
    if not isinstance(targets, list):
        raise ReductionError(f"scenario {scenario_id} has no target obligations")
    coverage: set[str] = set()
    for raw_target in targets:
        if isinstance(raw_target, str):
            coverage.add(raw_target)
    for cycle in cycles:
        if not isinstance(cycle, dict):
            raise ReductionError(f"scenario {scenario_id} has invalid cycle")
        hits = cycle.get("obligation_hits", [])
        if not isinstance(hits, list):
            raise ReductionError(
                f"scenario {scenario_id} has invalid obligation_hits"
            )
        for raw_hit in hits:
            if isinstance(raw_hit, str):
                coverage.add(raw_hit)
    unknown = sorted(coverage - declared_ids)
    if unknown:
        raise ReductionError(
            f"scenario {scenario_id} references unknown obligations: "
            + ", ".join(unknown)
        )
    return Candidate(
        scenario_id=scenario_id,
        cycle_count=len(cycles),
        coverage=frozenset(coverage),
        scenario=copy.deepcopy(dict(scenario)),
    )


def _remaining_gain(
    candidate: Candidate,
    coverage_counts: Mapping[str, int],
    required_counts: Mapping[str, int],
) -> int:
    return sum(
        1
        for obligation_id in candidate.coverage
        if coverage_counts[obligation_id] < required_counts[obligation_id]
    )


def reduce_suite(
    scenario_document: dict[str, Any],
    obligation_document: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    program_id = scenario_document.get("program_id")
    if program_id != obligation_document.get("program_id"):
        raise ReductionError(
            "scenario and obligation program identities do not match"
        )
    scenarios = scenario_document.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ReductionError("scenario document has no scenarios")
    required_counts, mcdc_ids = _obligation_requirements(obligation_document)
    declared_ids = set(required_counts)
    candidates = [
        _candidate_from_scenario(raw, declared_ids)
        for raw in scenarios
        if isinstance(raw, dict)
    ]
    if len(candidates) != len(scenarios):
        raise ReductionError("scenario document contains a non-object scenario")
    scenario_ids = [candidate.scenario_id for candidate in candidates]
    if len(set(scenario_ids)) != len(scenario_ids):
        raise ReductionError("scenario ids must be unique")

    available_counts = {obligation_id: 0 for obligation_id in declared_ids}
    for candidate in candidates:
        for obligation_id in candidate.coverage:
            available_counts[obligation_id] += 1
    insufficient = sorted(
        obligation_id
        for obligation_id, required in required_counts.items()
        if available_counts[obligation_id] < required
    )
    if insufficient:
        raise ReductionError(
            "input suite cannot satisfy required witness multiplicity: "
            + ", ".join(insufficient)
        )

    coverage_counts = {obligation_id: 0 for obligation_id in declared_ids}
    remaining = {candidate.scenario_id: candidate for candidate in candidates}
    selected: list[Candidate] = []
    selection_records: list[dict[str, Any]] = []

    while any(
        coverage_counts[obligation_id] < required
        for obligation_id, required in required_counts.items()
    ):
        ranked: list[tuple[int, int, str, Candidate]] = []
        for candidate in remaining.values():
            gain = _remaining_gain(candidate, coverage_counts, required_counts)
            if gain > 0:
                ranked.append(
                    (-gain, candidate.cycle_count, candidate.scenario_id, candidate)
                )
        if not ranked:
            break
        ranked.sort(key=lambda item: (item[0], item[1], item[2]))
        negative_gain, _, _, chosen = ranked[0]
        newly_covered = sorted(
            obligation_id
            for obligation_id in chosen.coverage
            if coverage_counts[obligation_id] < required_counts[obligation_id]
        )
        for obligation_id in newly_covered:
            coverage_counts[obligation_id] += 1
        selected.append(chosen)
        remaining.pop(chosen.scenario_id)
        selection_records.append(
            {
                "selection_index": len(selected),
                "scenario_id": chosen.scenario_id,
                "cycle_count": chosen.cycle_count,
                "gain": -negative_gain,
                "newly_covered_obligations": newly_covered,
                "covered_witness_slots_after_selection": sum(
                    min(coverage_counts[obligation_id], required)
                    for obligation_id, required in required_counts.items()
                ),
            }
        )

    uncovered = sorted(
        obligation_id
        for obligation_id, required in required_counts.items()
        if coverage_counts[obligation_id] < required
    )
    mcdc_witness_counts = {
        obligation_id: coverage_counts[obligation_id]
        for obligation_id in sorted(mcdc_ids)
    }
    missing_mcdc = sorted(
        obligation_id
        for obligation_id, count in mcdc_witness_counts.items()
        if count < 2
    )
    selected_scenarios = sorted(
        (copy.deepcopy(candidate.scenario) for candidate in selected),
        key=lambda item: item["id"],
    )
    input_cycles = sum(candidate.cycle_count for candidate in candidates)
    output_cycles = sum(candidate.cycle_count for candidate in selected)
    input_count = len(candidates)
    output_count = len(selected)
    required_witness_slots = sum(required_counts.values())
    covered_witness_slots = sum(
        min(coverage_counts[obligation_id], required)
        for obligation_id, required in required_counts.items()
    )
    reduction_run_id = "REDUCE-RUN-" + canonical_digest(
        {
            "scenario_document": scenario_document,
            "obligation_document": obligation_document,
            "algorithm": ALGORITHM_VERSION,
        }
    )
    reduced_document = {
        "schema_version": scenario_document.get("schema_version"),
        "profile_version": scenario_document.get("profile_version"),
        "program_id": program_id,
        "synthesis_run_id": scenario_document.get("synthesis_run_id"),
        "origin": scenario_document.get("origin"),
        "scenarios": selected_scenarios,
    }
    report = {
        "schema_version": "AEROST-SUITE-REDUCTION-REPORT-0.1",
        "profile_version": scenario_document.get("profile_version"),
        "program_id": program_id,
        "synthesis_run_id": scenario_document.get("synthesis_run_id"),
        "reduction_run_id": reduction_run_id,
        "algorithm": {
            "name": ALGORITHM_NAME,
            "version": ALGORITHM_VERSION,
            "selection": "maximum remaining witness-slot gain",
            "tie_breaking": [
                "minimum cycle count",
                "lexicographically smallest scenario id",
            ],
        },
        "input_summary": {
            "scenarios": input_count,
            "cycles": input_cycles,
            "obligations": len(required_counts),
            "required_witness_slots": required_witness_slots,
        },
        "output_summary": {
            "selected_scenarios": output_count,
            "selected_cycles": output_cycles,
            "covered_obligations": len(required_counts) - len(uncovered),
            "covered_witness_slots": covered_witness_slots,
            "scenario_reduction_percent": round(
                100.0 * (input_count - output_count) / input_count, 6
            ),
            "cycle_reduction_percent": round(
                100.0 * (input_cycles - output_cycles) / input_cycles, 6
            ),
        },
        "mcdc_summary": {
            "obligations": len(mcdc_ids),
            "required_witnesses_per_obligation": 2,
            "witness_counts": mcdc_witness_counts,
            "missing_witnesses": missing_mcdc,
        },
        "selection_trace": selection_records,
        "uncovered_obligations": uncovered,
        "deterministic": True,
        "passed": not uncovered and not missing_mcdc,
    }
    return reduced_document, report


def command_reduce(args: argparse.Namespace) -> int:
    scenarios = load_json(Path(args.scenarios))
    obligations = load_json(Path(args.obligations))
    reduced, report = reduce_suite(scenarios, obligations)
    write_json(Path(args.output_scenarios), reduced)
    write_json(Path(args.output_report), report)
    before = report["input_summary"]
    after = report["output_summary"]
    print(
        "deterministic assurance suite reduction "
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
        "witness slots covered: "
        f"{after['covered_witness_slots']}/{before['required_witness_slots']}"
    )
    return 0 if report["passed"] else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reduce synthesized assurance scenarios with deterministic greedy "
            "witness multicover."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    reduce_parser = subparsers.add_parser("reduce")
    reduce_parser.add_argument("--scenarios", required=True)
    reduce_parser.add_argument("--obligations", required=True)
    reduce_parser.add_argument("--output-scenarios", required=True)
    reduce_parser.add_argument("--output-report", required=True)
    reduce_parser.set_defaults(func=command_reduce)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ReductionError, OSError, ValueError) as exc:
        print(f"assurance suite reduction FAIL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
