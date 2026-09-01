from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import aerost_tool as tool
from application_mutations import ApplicationMutationError, load_mutation_profile
from assurance_obligations import extract_obligations
from assurance_suite_reducer import reduce_suite
from assurance_test_synthesizer import synthesize
from compare_assurance_suites import compare_suites
from input_domain_validator import validate_domain
from mutation_aware_suite_reducer import reduce_suite as reduce_mutation_aware_suite


class CaseValidationError(RuntimeError):
    pass


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tool.canonical_json(value), encoding="utf-8", newline="\n")


def _suite_by_name(comparison: dict[str, Any], name: str) -> dict[str, Any]:
    for suite in comparison.get("suites", []):
        if suite.get("name") == name:
            return suite
    raise CaseValidationError(f"comparison is missing suite: {name}")


def _run_generated_suite_three_way(
    root: Path,
    output_dir: Path,
    air_path: Path,
    bundle: dict[str, Any],
    synthesized: dict[str, Any],
) -> dict[str, Any]:
    scenarios = synthesized.get("scenarios")
    if not isinstance(scenarios, list):
        raise CaseValidationError("synthesized scenario document is missing scenarios")

    binary = bundle.get("binary")
    if binary is None:
        raise CaseValidationError(
            "generated-suite three-path execution requires a compiled backend"
        )

    protocol = output_dir / "generated-suite-protocol.txt"
    tool.build_protocol(bundle["air"], scenarios, protocol)

    reference = tool.run_reference_scenarios(bundle["air"], scenarios)

    run = tool.run_command([str(binary), "run", str(protocol)])
    backend = tool.parse_backend_output(run.stdout, bundle["air"])

    external_path = output_dir / "generated-suite-external-executor-traces.json"
    external_document = tool.run_external_air_executor(
        root,
        air_path,
        protocol,
        external_path,
    )
    external = external_document["cycles"]

    report = tool.three_way_differential_report(reference, backend, external)
    report["suite"] = "automatic-unreduced"
    report["scenario_count"] = len(scenarios)

    _write_json(
        output_dir / "generated-suite-reference-traces.json",
        tool.traces_document(reference),
    )
    _write_json(
        output_dir / "generated-suite-backend-traces.json",
        tool.backend_traces_document(backend),
    )
    _write_json(
        output_dir / "generated-suite-three-way-differential-results.json",
        report,
    )

    return report


def validate_application_case(
    root: Path,
    manifest_path: Path,
    output_dir: Path,
    *,
    compile_backend: bool,
) -> dict[str, Any]:
    root = root.resolve()
    application = tool.load_application_manifest(root, manifest_path)
    mutation_profile = load_mutation_profile(application.mutation_profile_path)
    if mutation_profile.get("application_id") != application.application_id:
        raise CaseValidationError("mutation profile application_id does not match manifest")
    if mutation_profile.get("profile_version") != application.profile_version:
        raise CaseValidationError("mutation profile profile_version does not match manifest")

    bundle = tool.build_core_bundle(
        root,
        output_dir,
        compile_backend=compile_backend,
        application=application,
    )

    air_path = output_dir / application.air_artifact_name
    policy_document = tool.load_json(application.runtime_fault_policy_path)
    traceability_document = tool.load_json(output_dir / "traceability-report.json")
    obligations = extract_obligations(
        air=bundle["air"],
        source_air_sha256=tool.sha256_file(air_path),
        fault_policy_document=policy_document,
        traceability_document=traceability_document,
    )
    domain = tool.load_json(application.input_domain_path)
    domain_summary = validate_domain(bundle["air"], domain)
    synthesized, synthesis_report = synthesize(bundle["air"], obligations, domain)

    generated_three_way = None
    if compile_backend:
        generated_three_way = _run_generated_suite_three_way(
            root,
            output_dir,
            air_path,
            bundle,
            synthesized,
        )

    reduced, reduction_report = reduce_suite(synthesized, obligations)
    mutation_reduced, mutation_reduction_report = reduce_mutation_aware_suite(
        bundle["air"],
        synthesized,
        obligations,
        mutation_profile=mutation_profile,
    )
    comparison = compare_suites(
        bundle["air"],
        obligations,
        tool.load_json(application.manual_requirements_path),
        tool.load_json(application.manual_closure_path),
        synthesized,
        reduced,
        mutation_reduced,
        mutation_profile=mutation_profile,
    )

    shutil.copy2(application.input_domain_path, output_dir / "controlled-input-domain.json")
    shutil.copy2(application.mutation_profile_path, output_dir / "mutation-profile.json")
    _write_json(
        output_dir / "input-domain-validation.json",
        domain_summary.to_document(
            air_sha256=tool.sha256_file(air_path),
            domain_sha256=tool.sha256_file(application.input_domain_path),
        ),
    )
    _write_json(output_dir / "assurance-obligations.json", obligations)
    _write_json(output_dir / "synthesized-scenarios.json", synthesized)
    _write_json(output_dir / "synthesis-report.json", synthesis_report)
    _write_json(output_dir / "obligation-reduced-scenarios.json", reduced)
    _write_json(output_dir / "suite-reduction-report.json", reduction_report)
    _write_json(output_dir / "mutation-aware-reduced-scenarios.json", mutation_reduced)
    _write_json(
        output_dir / "mutation-aware-suite-reduction-report.json",
        mutation_reduction_report,
    )
    _write_json(output_dir / "assurance-suite-comparison.json", comparison)

    schema_pairs = {
        "application-manifest.json": "application-manifest.schema.json",
        "mutation-profile.json": "mutation-profile.schema.json",
        "runtime-fault-policy.json": "runtime-fault-policy.schema.json",
        application.air_artifact_name: "air.schema.json",
        "controlled-input-domain.json": "input-domain.schema.json",
        "input-domain-validation.json": "input-domain-validation.schema.json",
        "assurance-obligations.json": "assurance-obligations.schema.json",
        "synthesized-scenarios.json": "synthesized-scenarios.schema.json",
        "synthesis-report.json": "synthesis-report.schema.json",
        "obligation-reduced-scenarios.json": "synthesized-scenarios.schema.json",
        "suite-reduction-report.json": "suite-reduction-report.schema.json",
        "mutation-aware-reduced-scenarios.json": "synthesized-scenarios.schema.json",
        "mutation-aware-suite-reduction-report.json": "mutation-aware-suite-reduction-report.schema.json",
        "assurance-suite-comparison.json": "assurance-suite-comparison.schema.json",
    }
    if compile_backend:
        schema_pairs[
            "generated-suite-three-way-differential-results.json"
        ] = "three-way-differential.schema.json"

    schema_records: list[dict[str, Any]] = []
    schemas_valid = True
    for artifact_name, schema_name in schema_pairs.items():
        artifact = tool.load_json(output_dir / artifact_name)
        schema = tool.load_json(root / "schemas" / schema_name)
        errors = tool.validate_json_schema(artifact, schema)
        schema_records.append(
            {
                "artifact": artifact_name,
                "schema": schema_name,
                "valid": not errors,
                "errors": errors,
            }
        )
        schemas_valid = schemas_valid and not errors

    core_passed = all(
        [
            bundle["diff"]["mismatch_count"] == 0,
            bundle["three_way"]["passed"],
            bundle["coverage"]["complete"],
            bundle["mcdc"]["complete"],
            bundle["traceability"]["complete"],
            bundle["expressive"]["complete"],
            bundle["external_executor_independence"]["passed"],
            bundle["backend_independence"]["passed"],
        ]
    )
    synthesis_passed = bool(synthesis_report["passed"])
    reduction_passed = bool(reduction_report["passed"])
    mutation_reduction_passed = bool(mutation_reduction_report["passed"])
    comparison_passed = bool(comparison["passed"] and comparison["suite_count"] == 5)
    final_suite = _suite_by_name(comparison, "automatic-mutation-aware-reduced")
    final_complete = all(
        [
            final_suite["obligations"]["covered"] == final_suite["obligations"]["total"],
            final_suite["mcdc"]["covered"] == final_suite["mcdc"]["total"],
            final_suite["mutation"]["killed"] == final_suite["mutation"]["total"],
        ]
    )
    generated_three_way_passed = (
        not compile_backend
        or (
            generated_three_way is not None
            and bool(generated_three_way["passed"])
        )
    )

    passed = all(
        [
            core_passed,
            generated_three_way_passed,
            synthesis_passed,
            reduction_passed,
            mutation_reduction_passed,
            comparison_passed,
            final_complete,
            schemas_valid,
        ]
    )

    summary = {
        "schema_version": "AEROST-APPLICATION-CASE-VALIDATION-0.2",
        "tool_version": tool.TOOL_VERSION,
        "profile_version": tool.PROFILE_VERSION,
        "application": application.summary(root),
        "validation_scope": {
            "generated_backend_compiled_and_executed": compile_backend,
            "generated_suite_three_path_execution_included": bool(compile_backend),
            "mutation_aware_evaluation_included": True,
            "authoritative_reproducibility_included": False,
            "description": (
                "case-study implementation, three-path core execution, bounded synthesis, "
                "obligation-only reduction, profile-driven mutation-aware reduction, and "
                "five-suite comparison"
            ),
        },
        "core": {
            "controlled_scenarios": len(bundle["scenarios"]),
            "executed_cycles": len(bundle["reference"]),
            "three_way_equivalent_cycles": bundle["three_way"]["equivalent_cycles"],
            "three_way_mismatch_count": bundle["three_way"]["mismatch_count"],
            "coverage_complete": bundle["coverage"]["complete"],
            "mcdc_complete": bundle["mcdc"]["complete"],
            "traceability_complete": bundle["traceability"]["complete"],
            "expressive_adequacy_percent": bundle["expressive"]["percent"],
            "external_executor_independence": bundle["external_executor_independence"]["passed"],
            "backend_independence": bundle["backend_independence"]["passed"],
            "passed": core_passed,
        },
        "bounded_input_domain": domain_summary.to_document(
            air_sha256=tool.sha256_file(air_path),
            domain_sha256=tool.sha256_file(application.input_domain_path),
        ),
        "automatic_test_synthesis": {
            "obligation_summary": synthesis_report["obligation_summary"],
            "search_summary": synthesis_report["search_summary"],
            "suite_summary": synthesis_report["suite_summary"],
            "generated_suite_three_way": {
                "suite": "automatic-unreduced",
                "executed": generated_three_way is not None,
                "scenario_count": len(synthesized["scenarios"]),
                "cycle_count": sum(
                    len(scenario["cycles"])
                    for scenario in synthesized["scenarios"]
                ),
                "execution_path_count": (
                    generated_three_way["execution_path_count"]
                    if generated_three_way is not None else 0
                ),
                "executed_cycles": (
                    generated_three_way["executed_cycles"]
                    if generated_three_way is not None else 0
                ),
                "equivalent_cycles": (
                    generated_three_way["equivalent_cycles"]
                    if generated_three_way is not None else 0
                ),
                "mismatch_count": (
                    generated_three_way["mismatch_count"]
                    if generated_three_way is not None else 0
                ),
                "passed": (
                    bool(generated_three_way["passed"])
                    if generated_three_way is not None else None
                ),
            },
            "passed": synthesis_passed,
        },
        "obligation_only_reduction": {
            "input_summary": reduction_report["input_summary"],
            "output_summary": reduction_report["output_summary"],
            "mcdc_summary": reduction_report["mcdc_summary"],
            "passed": reduction_passed,
        },
        "mutation_aware_reduction": {
            "input_summary": mutation_reduction_report["input_summary"],
            "output_summary": mutation_reduction_report["output_summary"],
            "mcdc_summary": mutation_reduction_report["mcdc_summary"],
            "mutation_summary": mutation_reduction_report["mutation_summary"],
            "claim_boundary": mutation_reduction_report["claim_boundary"],
            "passed": mutation_reduction_passed,
        },
        "assurance_suite_comparison": {
            "suite_count": comparison["suite_count"],
            "suites": comparison["suites"],
            "comparisons": comparison["comparisons"],
            "claim_boundary": comparison["claim_boundary"],
            "replay_passed": comparison["replay_passed"],
            "final_automatic_complete": comparison["final_automatic_complete"],
            "manual_suite_gate": comparison["manual_suite_gate"],
            "manual_suite_gate_passed": comparison["manual_suite_gate_passed"],
            "acceptance_passed": comparison["acceptance_passed"],
            "passed": comparison_passed,
        },
        "final_automatic_suite": {
            "scenario_count": final_suite["scenario_count"],
            "cycle_count": final_suite["cycle_count"],
            "obligations": final_suite["obligations"],
            "mcdc": final_suite["mcdc"],
            "mutation": final_suite["mutation"],
            "complete": final_complete,
        },
        "schema_validation": {
            "all_valid": schemas_valid,
            "records": schema_records,
        },
        "passed": passed,
    }
    summary_path = output_dir / "application-case-validation-summary.json"
    _write_json(summary_path, summary)
    summary_schema = tool.load_json(
        root / "schemas" / "application-case-validation-summary.schema.json"
    )
    summary_errors = tool.validate_json_schema(summary, summary_schema)
    summary_record = {
        "artifact": "application-case-validation-summary.json",
        "schema": "application-case-validation-summary.schema.json",
        "valid": not summary_errors,
        "errors": summary_errors,
    }
    summary["schema_validation"]["records"].append(summary_record)
    summary["schema_validation"]["all_valid"] = (
        summary["schema_validation"]["all_valid"] and not summary_errors
    )
    summary["passed"] = summary["passed"] and not summary_errors
    _write_json(summary_path, summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate an AEROST application case through core execution, bounded "
            "assurance synthesis, profile-driven mutation-aware reduction, and "
            "five-suite comparison."
        )
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--application", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compile-backend", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = validate_application_case(
            args.root,
            args.application,
            args.output,
            compile_backend=bool(args.compile_backend),
        )
    except (
        CaseValidationError,
        ApplicationMutationError,
        tool.AerostError,
        OSError,
        ValueError,
        KeyError,
    ) as exc:
        print(f"application case validation FAIL: {exc}", file=sys.stderr)
        return 2

    core = summary["core"]
    synthesis_summary = summary["automatic_test_synthesis"]
    obligation_reduction = summary["obligation_only_reduction"]
    mutation_reduction = summary["mutation_aware_reduction"]
    final_suite = summary["final_automatic_suite"]
    print("application case validation " + ("PASS" if summary["passed"] else "FAIL"))
    print(f"application: {summary['application']['id']}")
    print(
        "three-way cycles: "
        f"{core['three_way_equivalent_cycles']}/{core['executed_cycles']}"
    )
    obligations = synthesis_summary["obligation_summary"]
    print(f"bounded obligations: {obligations['covered']}/{obligations['total']}")
    before = obligation_reduction["input_summary"]
    after = obligation_reduction["output_summary"]
    print(
        "obligation-only suite: "
        f"{before['scenarios']} -> {after['selected_scenarios']} scenarios, "
        f"{before['cycles']} -> {after['selected_cycles']} cycles"
    )
    mutation = mutation_reduction["mutation_summary"]
    print(
        "mutation-aware suite: "
        f"{final_suite['scenario_count']} scenarios / {final_suite['cycle_count']} cycles, "
        f"mutants={mutation['killed']}/{mutation['total']}"
    )
    print(f"schemas valid: {summary['schema_validation']['all_valid']}")
    return 0 if summary["passed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
