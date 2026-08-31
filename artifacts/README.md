# Evidence Artifacts

The authoritative controlled evidence is stored under:

`artifacts/multi-case/latest/`

This bundle contains the two-application results, including per-application AIR, traces, differential comparisons, assurance obligations, selected MC/DC evidence, synthesis and reduction reports, mutation results, traceability records, generated Safe Rust, schema-validation results, and the aggregate multi-case summary.

The primary aggregate entry points are:

- `multi-case-results-summary.json`
- `multi-case-reproducibility.json`
- `multi-case-schema-validation.json`

The artifact filenames are schema-stable implementation identifiers. They should not be interpreted as certification claims or organizational-independence claims. In particular, the external AIR executor is separately implemented but shares the AIR produced by the common frontend.

`reference-host.json` records the host environment associated with the controlled evaluation baseline.

The evidence bundle can be regenerated with `run-evaluation.cmd` on a compatible Windows environment with Python 3.13 and Rust/Cargo 1.97.1.
