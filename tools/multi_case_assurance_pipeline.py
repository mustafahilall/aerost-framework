from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

import aerost_tool as tool
from application_case_validator import validate_application_case


SUITE_SCHEMA_VERSION = "AEROST-RESEARCH-SUITE-0.1"
SUMMARY_SCHEMA_VERSION = "AEROST-MULTI-CASE-RESULTS-0.1"
REPRO_SCHEMA_VERSION = "AEROST-MULTI-CASE-REPRODUCIBILITY-0.1"


class MultiCasePipelineError(RuntimeError):
    pass


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tool.canonical_json(value), encoding="utf-8", newline="\n")


def _resolve_repository_file(root: Path, raw: Any, field: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise MultiCasePipelineError(f"{field} must be a non-empty path string")
    candidate = (root / raw).resolve()
    if candidate != root and root not in candidate.parents:
        raise MultiCasePipelineError(f"{field} escapes repository root")
    if not candidate.is_file():
        raise MultiCasePipelineError(f"{field} does not exist: {raw}")
    return candidate


def load_research_suite(root: Path, suite_path: Path) -> dict[str, Any]:
    root = root.resolve()
    resolved = suite_path if suite_path.is_absolute() else root / suite_path
    resolved = resolved.resolve()
    if resolved != root and root not in resolved.parents:
        raise MultiCasePipelineError("research-suite manifest must be inside repository root")
    try:
        suite = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MultiCasePipelineError(f"cannot read research suite: {exc}") from exc
    if not isinstance(suite, dict):
        raise MultiCasePipelineError("research suite must be a JSON object")
    if suite.get("schema_version") != SUITE_SCHEMA_VERSION:
        raise MultiCasePipelineError("unsupported research-suite schema_version")
    if suite.get("profile_version") != tool.PROFILE_VERSION:
        raise MultiCasePipelineError("research-suite profile_version does not match tool")
    raw_applications = suite.get("applications")
    if not isinstance(raw_applications, list) or len(raw_applications) < 2:
        raise MultiCasePipelineError("research suite requires at least two applications")
    application_paths = [
        _resolve_repository_file(root, value, f"applications[{index}]")
        for index, value in enumerate(raw_applications)
    ]
    if len(set(application_paths)) != len(application_paths):
        raise MultiCasePipelineError("research suite contains duplicate application manifests")
    suite["_path"] = resolved
    suite["_application_paths"] = application_paths
    return suite


def _validate_output_directory(root: Path, output_dir: Path) -> Path:
    resolved = output_dir.resolve()
    filesystem_root = Path(resolved.anchor)
    if resolved == filesystem_root:
        raise MultiCasePipelineError("output directory may not be a filesystem root")
    if resolved == root or resolved in root.parents:
        raise MultiCasePipelineError(
            "output directory may not be the repository root or one of its ancestors"
        )
    if output_dir.exists() and output_dir.is_symlink():
        raise MultiCasePipelineError("output directory may not be a symbolic link")
    return resolved


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _controlled_files(root: Path) -> dict[str, str]:
    records: dict[str, str] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root)
        if "target" in relative.parts:
            continue
        records[relative.as_posix()] = _digest(path)
    return records


def _reproducibility_report(
    baseline_root: Path,
    reproduced_root: Path,
    *,
    enabled: bool,
) -> dict[str, Any]:
    if not enabled:
        return {
            "schema_version": REPRO_SCHEMA_VERSION,
            "enabled": False,
            "files_compared": 0,
            "files_identical": 0,
            "byte_identical": False,
            "mismatches": [],
        }
    baseline = _controlled_files(baseline_root)
    reproduced = _controlled_files(reproduced_root)
    names = sorted(set(baseline) | set(reproduced))
    mismatches: list[dict[str, Any]] = []
    identical = 0
    for name in names:
        left = baseline.get(name)
        right = reproduced.get(name)
        if left == right and left is not None:
            identical += 1
        else:
            mismatches.append(
                {
                    "path": name,
                    "baseline_sha256": left,
                    "reproduced_sha256": right,
                }
            )
    return {
        "schema_version": REPRO_SCHEMA_VERSION,
        "enabled": True,
        "files_compared": len(names),
        "files_identical": identical,
        "byte_identical": not mismatches,
        "mismatches": mismatches,
    }


def _compact_application(summary: dict[str, Any]) -> dict[str, Any]:
    final_suite = summary["final_automatic_suite"]
    synthesis = summary["automatic_test_synthesis"]
    generated_three_way = synthesis["generated_suite_three_way"]
    return {
        "id": summary["application"]["id"],
        "manifest": summary["application"]["manifest"],
        "mutation_profile": summary["application"]["mutation_profile"],
        "compiled_backend": summary["validation_scope"][
            "generated_backend_compiled_and_executed"
        ],
        "controlled_scenarios": summary["core"]["controlled_scenarios"],
        "executed_cycles": summary["core"]["executed_cycles"],
        "equivalent_cycles": summary["core"]["three_way_equivalent_cycles"],
        "generated_suite_three_way": generated_three_way,
        "assurance_obligations": synthesis["obligation_summary"],
        "final_automatic_suite": {
            "scenario_count": final_suite["scenario_count"],
            "cycle_count": final_suite["cycle_count"],
            "obligations": final_suite["obligations"],
            "mcdc": final_suite["mcdc"],
            "mutation": final_suite["mutation"],
        },
        "schema_validation": summary["schema_validation"]["all_valid"],
        "passed": summary["passed"],
    }


def _aggregate(applications: Iterable[dict[str, Any]]) -> dict[str, int]:
    items = list(applications)
    return {
        "controlled_scenarios": sum(item["controlled_scenarios"] for item in items),
        "executed_cycles": sum(item["executed_cycles"] for item in items),
        "equivalent_cycles": sum(item["equivalent_cycles"] for item in items),
        "generated_scenarios": sum(
            item["generated_suite_three_way"]["scenario_count"] for item in items
        ),
        "generated_cycles": sum(
            item["generated_suite_three_way"]["cycle_count"] for item in items
        ),
        "generated_three_way_executed_cycles": sum(
            item["generated_suite_three_way"]["executed_cycles"] for item in items
        ),
        "generated_three_way_equivalent_cycles": sum(
            item["generated_suite_three_way"]["equivalent_cycles"] for item in items
        ),
        "generated_three_way_mismatches": sum(
            item["generated_suite_three_way"]["mismatch_count"] for item in items
        ),
        "assurance_obligations_covered": sum(
            item["assurance_obligations"]["covered"] for item in items
        ),
        "assurance_obligations_total": sum(
            item["assurance_obligations"]["total"] for item in items
        ),
        "selected_mcdc_covered": sum(
            item["final_automatic_suite"]["mcdc"]["covered"] for item in items
        ),
        "selected_mcdc_total": sum(
            item["final_automatic_suite"]["mcdc"]["total"] for item in items
        ),
        "controlled_mutants_killed": sum(
            item["final_automatic_suite"]["mutation"]["killed"] for item in items
        ),
        "controlled_mutants_total": sum(
            item["final_automatic_suite"]["mutation"]["total"] for item in items
        ),
        "final_automatic_scenarios": sum(
            item["final_automatic_suite"]["scenario_count"] for item in items
        ),
        "final_automatic_cycles": sum(
            item["final_automatic_suite"]["cycle_count"] for item in items
        ),
    }


def _run_applications(
    root: Path,
    application_paths: list[Path],
    output_root: Path,
    *,
    compile_backend: bool,
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for manifest_path in application_paths:
        application = tool.load_application_manifest(root, manifest_path)
        if application.application_id in seen_ids:
            raise MultiCasePipelineError(
                f"duplicate application_id in research suite: {application.application_id}"
            )
        seen_ids.add(application.application_id)
        case_output = output_root / application.application_id
        command = [
            sys.executable,
            str(root / "tools" / "application_case_validator.py"),
            "--root",
            str(root),
            "--application",
            str(manifest_path),
            "--output",
            str(case_output),
        ]
        if compile_backend:
            command.append("--compile-backend")
        print(
            f"[multi-case] validating {application.application_id} "
            f"(compiled_backend={compile_backend})",
            flush=True,
        )
        environment = dict(__import__("os").environ)
        environment["PYTHONPATH"] = str(root / "tools")
        result = subprocess.run(
            command,
            cwd=root,
            env=environment,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise MultiCasePipelineError(
                f"application validation failed for {application.application_id}"
            )
        summary_path = case_output / "application-case-validation-summary.json"
        summaries.append(tool.load_json(summary_path))
    return summaries


def run_multi_case_pipeline(
    root: Path,
    suite_path: Path,
    output_dir: Path,
    *,
    compile_backend: bool,
    check_reproducibility: bool,
) -> dict[str, Any]:
    root = root.resolve()
    suite = load_research_suite(root, suite_path)
    output_dir = _validate_output_directory(root, output_dir)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    shutil.copy2(suite["_path"], output_dir / "research-suite.json")

    summaries = _run_applications(
        root,
        suite["_application_paths"],
        output_dir / "applications",
        compile_backend=compile_backend,
    )
    compact = [_compact_application(summary) for summary in summaries]

    if check_reproducibility:
        with tempfile.TemporaryDirectory() as temporary:
            reproduced_root = Path(temporary) / "applications"
            _run_applications(
                root,
                suite["_application_paths"],
                reproduced_root,
                compile_backend=compile_backend,
            )
            reproducibility = _reproducibility_report(
                output_dir / "applications",
                reproduced_root,
                enabled=True,
            )
    else:
        reproducibility = _reproducibility_report(
            output_dir / "applications",
            output_dir / "applications",
            enabled=False,
        )
    _write_json(output_dir / "multi-case-reproducibility.json", reproducibility)

    authoritative = bool(compile_backend and check_reproducibility)
    aggregate = _aggregate(compact)
    generated_three_way_complete = (
        not compile_backend
        or (
            aggregate["generated_three_way_executed_cycles"]
            == aggregate["generated_cycles"]
            and aggregate["generated_three_way_equivalent_cycles"]
            == aggregate["generated_three_way_executed_cycles"]
            and aggregate["generated_three_way_mismatches"] == 0
        )
    )

    aggregate_complete = all(
        [
            aggregate["equivalent_cycles"] == aggregate["executed_cycles"],
            generated_three_way_complete,
            aggregate["assurance_obligations_covered"]
            == aggregate["assurance_obligations_total"],
            aggregate["selected_mcdc_covered"] == aggregate["selected_mcdc_total"],
            aggregate["controlled_mutants_killed"]
            == aggregate["controlled_mutants_total"],
        ]
    )
    passed = all(item["passed"] for item in compact) and aggregate_complete
    if check_reproducibility:
        passed = passed and reproducibility["byte_identical"]

    summary = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "tool_version": tool.TOOL_VERSION,
        "profile_version": tool.PROFILE_VERSION,
        "suite_id": suite["suite_id"],
        "authoritative_full_pipeline": authoritative,
        "assembly_validation_mode": not compile_backend,
        "application_count": len(compact),
        "applications": compact,
        "aggregate": aggregate,
        "reproducibility": reproducibility,
        "passed": passed,
    }
    _write_json(output_dir / "multi-case-results-summary.json", summary)

    schema_pairs = {
        "research-suite.json": "research-suite.schema.json",
        "multi-case-results-summary.json": "multi-case-results-summary.schema.json",
        "multi-case-reproducibility.json": "multi-case-reproducibility.schema.json",
    }
    schema_records: list[dict[str, Any]] = []
    all_valid = True
    for artifact_name, schema_name in schema_pairs.items():
        errors = tool.validate_json_schema(
            tool.load_json(output_dir / artifact_name),
            tool.load_json(root / "schemas" / schema_name),
        )
        schema_records.append(
            {
                "artifact": artifact_name,
                "schema": schema_name,
                "valid": not errors,
                "errors": errors,
            }
        )
        all_valid = all_valid and not errors
    schema_report = {"all_valid": all_valid, "records": schema_records}
    _write_json(output_dir / "multi-case-schema-validation.json", schema_report)
    if not all_valid:
        summary["passed"] = False
        _write_json(output_dir / "multi-case-results-summary.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run application-manifest-driven multi-case AEROST assurance evaluation."
        )
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compile-backend", action="store_true")
    parser.add_argument("--check-reproducibility", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = run_multi_case_pipeline(
            args.root,
            args.suite,
            args.output,
            compile_backend=bool(args.compile_backend),
            check_reproducibility=bool(args.check_reproducibility),
        )
    except (
        MultiCasePipelineError,
        tool.AerostError,
        OSError,
        ValueError,
        KeyError,
    ) as exc:
        print(f"multi-case assurance pipeline FAIL: {exc}", file=sys.stderr)
        return 2

    aggregate = summary["aggregate"]
    print("multi-case assurance pipeline " + ("PASS" if summary["passed"] else "FAIL"))
    print(f"suite: {summary['suite_id']}")
    print(f"applications: {summary['application_count']}")
    print(
        "three-way cycles: "
        f"{aggregate['equivalent_cycles']}/{aggregate['executed_cycles']}"
    )
    print(
        "assurance obligations: "
        f"{aggregate['assurance_obligations_covered']}/"
        f"{aggregate['assurance_obligations_total']}"
    )
    print(
        "selected MC/DC: "
        f"{aggregate['selected_mcdc_covered']}/{aggregate['selected_mcdc_total']}"
    )
    print(
        "controlled mutants: "
        f"{aggregate['controlled_mutants_killed']}/"
        f"{aggregate['controlled_mutants_total']}"
    )
    print(
        "final automatic suites: "
        f"{aggregate['final_automatic_scenarios']} scenarios / "
        f"{aggregate['final_automatic_cycles']} cycles"
    )
    reproduction = summary["reproducibility"]
    if reproduction["enabled"]:
        print(
            "reproducibility: "
            f"{reproduction['files_identical']}/{reproduction['files_compared']}"
        )
    print(f"authoritative: {summary['authoritative_full_pipeline']}")
    return 0 if summary["passed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
