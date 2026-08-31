# AEROST Research Tools

This directory contains the implementation of the AEROST research toolchain.

Core components include:

- `aerost_tool.py` — ASCP parsing, semantic validation, AIR construction, reference execution, Safe Rust generation, traceability, coverage, and evidence production;
- `external_air_executor.py` — separately implemented AIR execution path;
- `assurance_obligations.py` — assurance-obligation extraction;
- `input_domain_validator.py` — bounded input-domain validation;
- `assurance_test_synthesizer.py` — bounded stateful witness generation;
- `assurance_suite_reducer.py` — deterministic obligation-only reduction;
- `mutation_aware_suite_reducer.py` — mutation-aware reduction;
- `application_mutations.py` — controlled application-profile mutation construction;
- `compare_assurance_suites.py` — manual/controlled/generated suite comparison;
- `application_case_validator.py` and `multi_case_assurance_pipeline.py` — per-case and aggregate orchestration.

Files named `test_*.py` form the regression and validation suite.

The implementation is research software. Tool outputs are evaluated as controlled evidence; they are not presented as qualified certification-tool outputs.
