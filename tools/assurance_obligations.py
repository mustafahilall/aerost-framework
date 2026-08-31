#!/usr/bin/env python3
"""Extract deterministic assurance obligations from exported AEROST AIR.

This module intentionally uses only the Python standard library and does not
import compiler or pipeline implementation modules. It consumes published JSON
artifacts and emits the v0.1 assurance-obligation document.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

EXTRACTOR_VERSION = "AEROST-OBLIGATION-EXTRACTOR-0.1.0"
OUTPUT_SCHEMA_VERSION = "AEROST-ASSURANCE-OBLIGATIONS-0.1"
ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
AIR_STATEMENT_PATTERN = re.compile(r"^AIR-STMT-[A-Za-z0-9._-]+$")
AIR_DECISION_PATTERN = re.compile(r"^AIR-DEC-[A-Za-z0-9._-]+$")
AIR_CONDITION_PATTERN = re.compile(r"^AIR-COND-[A-Za-z0-9._-]+$")


class ObligationExtractionError(Exception):
    """Controlled extraction failure."""


def canonical_json(data: Any) -> str:
    return json.dumps(
        data,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
        separators=(",", ": "),
    ) + "\n"


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ObligationExtractionError(f"artifact not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ObligationExtractionError(
            f"invalid JSON in {path}: line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(data), encoding="utf-8", newline="\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_digest(value: Any, length: int = 12) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:length].upper()


def require_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ObligationExtractionError(f"{field} must be a non-empty string")
    if not ID_PATTERN.fullmatch(value):
        raise ObligationExtractionError(
            f"{field} contains unsupported identity characters: {value!r}"
        )
    return value


def string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ObligationExtractionError(f"{field} must be an array")
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(require_identifier(item, f"{field}[{index}]"))
    return sorted(set(result))


def walk_dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def collect_statement_records(program: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for stage_name in ("transition_stage", "output_stage"):
        stage = program.get(stage_name, [])
        if not isinstance(stage, list):
            raise ObligationExtractionError(f"program.{stage_name} must be an array")
        for node in walk_dicts(stage):
            statement_id = node.get("id")
            kind = node.get("kind")
            if isinstance(statement_id, str) and isinstance(kind, str):
                if not AIR_STATEMENT_PATTERN.fullmatch(statement_id):
                    raise ObligationExtractionError(
                        f"invalid AIR statement identity: {statement_id!r}"
                    )
                prior = records.get(statement_id)
                normalized = {
                    "id": statement_id,
                    "kind": kind,
                    "stage": node.get("stage"),
                    "requirements": string_list(
                        node.get("requirements", []),
                        f"statement {statement_id}.requirements",
                    ),
                    "source_id": node.get("source_id"),
                }
                if prior is not None and prior != normalized:
                    raise ObligationExtractionError(
                        f"conflicting AIR statement identity: {statement_id}"
                    )
                records[statement_id] = normalized
    return [records[key] for key in sorted(records)]


def collect_decision_records(program: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for stage_name in ("transition_stage", "output_stage"):
        stage = program.get(stage_name, [])
        for node in walk_dicts(stage):
            decision_id = node.get("decision_id")
            if not isinstance(decision_id, str):
                continue
            if not AIR_DECISION_PATTERN.fullmatch(decision_id):
                raise ObligationExtractionError(
                    f"invalid AIR decision identity: {decision_id!r}"
                )
            raw_conditions = node.get("conditions", [])
            if not isinstance(raw_conditions, list) or not raw_conditions:
                raise ObligationExtractionError(
                    f"decision {decision_id} has no condition identities"
                )
            condition_ids: list[str] = []
            for index, condition in enumerate(raw_conditions):
                if not isinstance(condition, dict):
                    raise ObligationExtractionError(
                        f"decision {decision_id}.conditions[{index}] must be an object"
                    )
                condition_id = condition.get("id")
                if not isinstance(condition_id, str) or not AIR_CONDITION_PATTERN.fullmatch(condition_id):
                    raise ObligationExtractionError(
                        f"invalid AIR condition identity in {decision_id}: {condition_id!r}"
                    )
                condition_ids.append(condition_id)
            normalized = {
                "decision_id": decision_id,
                "condition_ids": sorted(set(condition_ids)),
                "mcdc": bool(node.get("mcdc", False)),
                "requirements": string_list(
                    node.get("requirements", []),
                    f"decision {decision_id}.requirements",
                ),
                "source_id": node.get("source_id"),
            }
            prior = records.get(decision_id)
            if prior is not None and prior != normalized:
                raise ObligationExtractionError(
                    f"conflicting AIR decision identity: {decision_id}"
                )
            records[decision_id] = normalized
    return [records[key] for key in sorted(records)]


def collect_case_arm_records(program: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for stage_name in ("transition_stage", "output_stage"):
        stage = program.get(stage_name, [])
        for node in walk_dicts(stage):
            if node.get("kind") != "case":
                continue
            case_id = node.get("id")
            if not isinstance(case_id, str) or not AIR_STATEMENT_PATTERN.fullmatch(case_id):
                raise ObligationExtractionError(f"invalid CASE statement identity: {case_id!r}")
            parent_requirements = string_list(
                node.get("requirements", []),
                f"CASE {case_id}.requirements",
            )
            arms = node.get("arms", [])
            if not isinstance(arms, list):
                raise ObligationExtractionError(f"CASE {case_id}.arms must be an array")
            for index, arm in enumerate(arms):
                if not isinstance(arm, dict):
                    raise ObligationExtractionError(
                        f"CASE {case_id}.arms[{index}] must be an object"
                    )
                value = arm.get("value")
                value_id = require_identifier(value, f"CASE {case_id}.arms[{index}].value")
                arm_id = f"AIR-CASEARM-{canonical_digest({'case_id': case_id, 'value': value_id})}"
                event_id = f"{case_id}::{value_id}"
                requirements = string_list(
                    arm.get("requirements", parent_requirements),
                    f"CASE arm {arm_id}.requirements",
                )
                record = {
                    "id": arm_id,
                    "case_statement_id": case_id,
                    "value": value_id,
                    "event_id": event_id,
                    "requirements": requirements,
                }
                prior = records.get(arm_id)
                if prior is not None and prior != record:
                    raise ObligationExtractionError(f"conflicting CASE arm identity: {arm_id}")
                records[arm_id] = record
    return [records[key] for key in sorted(records)]


def fault_policy_record(
    air: Mapping[str, Any],
    fault_policy_document: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    air_contract = air.get("execution_contract", {})
    if not isinstance(air_contract, dict):
        raise ObligationExtractionError("execution_contract must be an object")
    policy = air_contract.get("runtime_fault_policy")
    diagnostic_policy = air_contract.get("diagnostic_policy", {})

    if fault_policy_document is not None:
        external_policy = fault_policy_document.get("runtime_fault_policy")
        external_diagnostic = fault_policy_document.get("diagnostic_policy", {})
        if policy is None:
            policy = external_policy
        elif external_policy is not None and policy != external_policy:
            raise ObligationExtractionError(
                "AIR and runtime-fault-policy artifacts disagree"
            )
        if diagnostic_policy in ({}, None):
            diagnostic_policy = external_diagnostic
        elif external_diagnostic not in ({}, None) and diagnostic_policy != external_diagnostic:
            raise ObligationExtractionError(
                "AIR and runtime-fault-policy diagnostic metadata disagree"
            )

    if policy is None:
        return None
    if not isinstance(policy, dict):
        raise ObligationExtractionError("runtime_fault_policy must be an object")
    if not isinstance(diagnostic_policy, dict):
        raise ObligationExtractionError("diagnostic_policy must be an object")

    policy_id = f"AEROST-FAULT-POLICY-{canonical_digest({'policy': policy, 'diagnostic_policy': diagnostic_policy})}"
    diagnostic = policy.get("diagnostic")
    if diagnostic is not None:
        diagnostic = require_identifier(diagnostic, "runtime_fault_policy.diagnostic")
    output_assignments = policy.get("output_assignments", [])
    retained_assignments = policy.get("retained_assignments", [])
    if not isinstance(output_assignments, list) or not isinstance(retained_assignments, list):
        raise ObligationExtractionError(
            "runtime fault-policy assignments must be arrays"
        )
    return {
        "id": policy_id,
        "diagnostic": diagnostic,
        "normal_commit_inhibited": bool(policy.get("normal_commit_inhibited", False)),
        "output_assignments": output_assignments,
        "retained_assignments": retained_assignments,
        "diagnostic_policy": diagnostic_policy,
    }


def optional_path_records(
    air: Mapping[str, Any],
    key: str,
    contract_document: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    contract = air.get("execution_contract", {})
    air_metadata = contract.get("assurance_obligations", {}) if isinstance(contract, dict) else {}
    document_metadata = (
        contract_document.get("assurance_obligations", {})
        if isinstance(contract_document, Mapping)
        else {}
    )
    if air_metadata not in ({}, None) and document_metadata not in ({}, None):
        if air_metadata != document_metadata:
            raise ObligationExtractionError(
                "AIR assurance_obligations do not match controlled policy document"
            )
    metadata = document_metadata if document_metadata not in ({}, None) else air_metadata
    if metadata in ({}, None):
        return []
    if not isinstance(metadata, dict):
        raise ObligationExtractionError(
            "execution_contract.assurance_obligations must be an object"
        )
    records = metadata.get(key, [])
    if not isinstance(records, list):
        raise ObligationExtractionError(
            f"execution_contract.assurance_obligations.{key} must be an array"
        )
    normalized: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ObligationExtractionError(f"{key}[{index}] must be an object")
        path_id = require_identifier(record.get("id"), f"{key}[{index}].id")
        subject_ids = string_list(record.get("subject_ids", [path_id]), f"{key}[{index}].subject_ids")
        if not subject_ids:
            raise ObligationExtractionError(f"{key}[{index}].subject_ids may not be empty")
        normalized.append(
            {
                "id": path_id,
                "subject_ids": subject_ids,
                "requirements": string_list(
                    record.get("requirements", []),
                    f"{key}[{index}].requirements",
                ),
                "attributes": record.get("attributes", {}),
                "description": str(record.get("description", "")),
            }
        )
    return sorted(normalized, key=lambda item: item["id"])


def add_obligation(
    result: dict[str, dict[str, Any]],
    *,
    obligation_id: str,
    kind: str,
    subject_ids: Iterable[str],
    requirements: Iterable[str] = (),
    attributes: Mapping[str, Any] | None = None,
    description: str,
) -> None:
    require_identifier(obligation_id, "obligation.id")
    normalized_subjects = sorted(
        {require_identifier(value, "obligation.subject_id") for value in subject_ids}
    )
    if not normalized_subjects:
        raise ObligationExtractionError(f"obligation {obligation_id} has no subject IDs")
    normalized_requirements = sorted(
        {require_identifier(value, "obligation.requirement") for value in requirements}
    )
    record = {
        "id": obligation_id,
        "kind": kind,
        "subject_ids": normalized_subjects,
        "requirements": normalized_requirements,
        "reachable_status": "UNKNOWN",
        "attributes": dict(attributes or {}),
        "description": description,
    }
    prior = result.get(obligation_id)
    if prior is not None and prior != record:
        raise ObligationExtractionError(f"conflicting obligation identity: {obligation_id}")
    result[obligation_id] = record


def extract_obligations(
    *,
    air: Mapping[str, Any],
    source_air_sha256: str,
    fault_policy_document: Mapping[str, Any] | None = None,
    traceability_document: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    profile_version = require_identifier(air.get("profile_version"), "profile_version")
    program = air.get("program")
    if not isinstance(program, dict):
        raise ObligationExtractionError("AIR program must be an object")
    program_id = require_identifier(program.get("id"), "program.id")

    obligations: dict[str, dict[str, Any]] = {}
    statements = collect_statement_records(program)
    decisions = collect_decision_records(program)
    case_arms = collect_case_arm_records(program)

    for statement in statements:
        statement_id = statement["id"]
        add_obligation(
            obligations,
            obligation_id=f"OBL-STMT-{statement_id}",
            kind="STATEMENT_REACHED",
            subject_ids=[statement_id],
            requirements=statement["requirements"],
            attributes={
                "air_kind": statement["kind"],
                "stage": statement["stage"],
                "source_id": statement["source_id"],
            },
            description=f"Reach AIR statement {statement_id}.",
        )

    for decision in decisions:
        decision_id = decision["decision_id"]
        common_attributes = {
            "condition_ids": decision["condition_ids"],
            "selected_for_mcdc": decision["mcdc"],
            "source_id": decision["source_id"],
        }
        for outcome, kind in ((True, "DECISION_TRUE"), (False, "DECISION_FALSE")):
            suffix = "TRUE" if outcome else "FALSE"
            add_obligation(
                obligations,
                obligation_id=f"OBL-DEC-{decision_id}-{suffix}",
                kind=kind,
                subject_ids=[decision_id],
                requirements=decision["requirements"],
                attributes={**common_attributes, "required_outcome": outcome},
                description=f"Evaluate AIR decision {decision_id} to {str(outcome).lower()}.",
            )
        for condition_id in decision["condition_ids"]:
            for outcome, kind in ((True, "CONDITION_TRUE"), (False, "CONDITION_FALSE")):
                suffix = "TRUE" if outcome else "FALSE"
                add_obligation(
                    obligations,
                    obligation_id=f"OBL-COND-{condition_id}-{suffix}",
                    kind=kind,
                    subject_ids=[condition_id, decision_id],
                    requirements=decision["requirements"],
                    attributes={
                        "decision_id": decision_id,
                        "required_value": outcome,
                    },
                    description=(
                        f"Evaluate atomic condition {condition_id} to "
                        f"{str(outcome).lower()} in decision {decision_id}."
                    ),
                )
            if decision["mcdc"]:
                add_obligation(
                    obligations,
                    obligation_id=f"OBL-MCDC-{decision_id}-{condition_id}",
                    kind="MCDC_PAIR",
                    subject_ids=[decision_id, condition_id],
                    requirements=decision["requirements"],
                    attributes={
                        "decision_id": decision_id,
                        "condition_id": condition_id,
                        "other_condition_ids": [
                            value
                            for value in decision["condition_ids"]
                            if value != condition_id
                        ],
                    },
                    description=(
                        f"Demonstrate the independent effect of {condition_id} "
                        f"on decision {decision_id}."
                    ),
                )

    for arm in case_arms:
        add_obligation(
            obligations,
            obligation_id=f"OBL-CASE-{arm['id']}",
            kind="CASE_ARM_REACHED",
            subject_ids=[arm["id"], arm["case_statement_id"]],
            requirements=arm["requirements"],
            attributes={
                "case_statement_id": arm["case_statement_id"],
                "case_value": arm["value"],
                "semantic_event_id": arm["event_id"],
            },
            description=(
                f"Reach CASE arm {arm['value']} of {arm['case_statement_id']}."
            ),
        )

    policy = fault_policy_record(air, fault_policy_document)
    if policy is not None:
        policy_id = policy["id"]
        base_attributes = {
            "diagnostic": policy["diagnostic"],
            "normal_commit_inhibited": policy["normal_commit_inhibited"],
            "retained_assignments": policy["retained_assignments"],
            "output_assignments": policy["output_assignments"],
        }
        add_obligation(
            obligations,
            obligation_id=f"OBL-FAULT-{policy_id}",
            kind="FAULT_POLICY_ACTIVATED",
            subject_ids=[policy_id],
            attributes=base_attributes,
            description=f"Activate declared runtime fault policy {policy_id}.",
        )
        if policy["normal_commit_inhibited"]:
            add_obligation(
                obligations,
                obligation_id=f"OBL-COMMIT-INHIBITED-{policy_id}",
                kind="NORMAL_COMMIT_INHIBITED",
                subject_ids=[policy_id],
                attributes={"required_value": True},
                description=(
                    f"Observe inhibited normal commit under fault policy {policy_id}."
                ),
            )
        if policy["output_assignments"]:
            add_obligation(
                obligations,
                obligation_id=f"OBL-CONSERVATIVE-OUTPUT-{policy_id}",
                kind="CONSERVATIVE_OUTPUT_APPLIED",
                subject_ids=[policy_id],
                attributes={
                    "expected_output_assignments": policy["output_assignments"],
                    "expected_retained_assignments": policy["retained_assignments"],
                },
                description=(
                    f"Apply the declared conservative outputs of fault policy {policy_id}."
                ),
            )

    for record in optional_path_records(air, "reset_paths", fault_policy_document):
        add_obligation(
            obligations,
            obligation_id=f"OBL-RESET-{record['id']}",
            kind="RESET_PATH_EXECUTED",
            subject_ids=record["subject_ids"],
            requirements=record["requirements"],
            attributes=record["attributes"],
            description=record["description"] or f"Execute reset path {record['id']}.",
        )

    for record in optional_path_records(air, "recovery_paths", fault_policy_document):
        add_obligation(
            obligations,
            obligation_id=f"OBL-RECOVERY-{record['id']}",
            kind="RECOVERY_PATH_EXECUTED",
            subject_ids=record["subject_ids"],
            requirements=record["requirements"],
            attributes=record["attributes"],
            description=record["description"] or f"Execute recovery path {record['id']}.",
        )

    if traceability_document is not None:
        requirement_ids = string_list(
            traceability_document.get("application_requirements", []),
            "traceability.application_requirements",
        )
        for requirement_id in requirement_ids:
            add_obligation(
                obligations,
                obligation_id=f"OBL-REQ-{requirement_id}",
                kind="REQUIREMENT_EXERCISED",
                subject_ids=[requirement_id],
                requirements=[requirement_id],
                attributes={},
                description=f"Exercise controlled requirement root {requirement_id}.",
            )

    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "profile_version": profile_version,
        "program_id": program_id,
        "generated_by": EXTRACTOR_VERSION,
        "source_air_sha256": source_air_sha256,
        "obligations": [obligations[key] for key in sorted(obligations)],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract deterministic assurance obligations from AEROST AIR."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    extract = subparsers.add_parser("extract", help="extract obligation records")
    extract.add_argument("--air", required=True, type=Path)
    extract.add_argument("--fault-policy", type=Path)
    extract.add_argument("--traceability", type=Path)
    extract.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        air = load_json(args.air)
        fault_policy = load_json(args.fault_policy) if args.fault_policy else None
        traceability = load_json(args.traceability) if args.traceability else None
        result = extract_obligations(
            air=air,
            source_air_sha256=sha256_file(args.air),
            fault_policy_document=fault_policy,
            traceability_document=traceability,
        )
        write_json(args.output, result)
    except (ObligationExtractionError, OSError) as exc:
        print(f"AEROST obligation extraction ERROR: {exc}", file=sys.stderr)
        return 2

    counts: dict[str, int] = {}
    for obligation in result["obligations"]:
        counts[obligation["kind"]] = counts.get(obligation["kind"], 0) + 1
    print(f"assurance obligation extraction PASS: {args.output}")
    print(f"program: {result['program_id']}")
    print(f"obligations: {len(result['obligations'])}")
    for kind in sorted(counts):
        print(f"  {kind}: {counts[kind]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
