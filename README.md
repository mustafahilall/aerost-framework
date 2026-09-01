# AEROST

AEROST is a standardization-oriented research prototype for deterministic supervisory control and reproducible assurance evidence in unmanned aerial vehicle avionics. The project evaluates a restricted supervisory-control profile across two controlled applications: a dual-source electrical-power supervisor and a dual-link communication supervisor.

## Research Scope

The implemented system includes:

- the AEROST Supervisory Control Profile (ASCP-0.2);
- explicit deterministic cycle semantics with initialized retained state, transition/output separation, conservative defaults, and atomic commit;
- lowering to an executable Assurance Intermediate Representation (AIR) with trace metadata;
- a generic AIR reference interpreter;
- generated Safe Rust compiled and executed as a separate process;
- a separately implemented external AIR executor;
- cycle-level differential comparison on the controlled closure suites;
- requirement-linked structural coverage and selected modified condition/decision coverage (MC/DC) evidence;
- bounded stateful assurance-test synthesis;
- deterministic obligation-only and mutation-aware suite reduction;
- controlled AIR-level mutation analysis; and
- schema validation and clean-directory byte reproducibility.

The research suite contains exactly two application cases. Application-specific behavior is supplied through controlled source, requirements, policies, scenario sets, input-domain definitions, and mutation profiles; the execution and evidence tooling is shared.

## Controlled Evaluation Results

The repository contains the controlled evidence used for the evaluation:

| Measure | Result |
|---|---:|
| Controlled applications | 2 |
| Controlled closure scenarios | 95 |
| Controlled closure cycles | 132 |
| Three-path equivalent cycles | 132 / 132 |
| Declared assurance obligations covered | 538 / 538 |
| Selected MC/DC objectives covered | 13 / 13 |
| Unreduced synthesized suite | 551 scenarios / 1,195 cycles |
| Obligation-only reduced suite | 86 scenarios / 202 cycles |
| Declared mutation distinctions retained by obligation-only reduction | 11 / 16 |
| Mutation-aware reduced suite | 90 scenarios / 213 cycles |
| Predeclared mutation distinctions retained by mutation-aware reduction | 16 / 16 |
| Clean-directory reproducibility | 76 / 76 controlled artifacts byte-identical |

The 16/16 mutation-aware result is an in-sample preservation result: the same predeclared mutation profiles guide reduction and final scoring. It is not a prediction of effectiveness on unseen defects.

## Repository Layout

- `applications/` — manifests for the two controlled applications and the research suite.
- `examples/` — controlled ASCP source, requirements, runtime-fault policies, and mutation profiles.
- `specs/` — current ASCP, execution, AIR, conformance, bounded-domain, and assurance-obligation contracts.
- `tests/` — requirement-oriented scenarios, deterministic structural-closure supplements, and bounded input domains.
- `conformance/` — accepted and rejected ASCP programs.
- `tools/` — compiler/evidence implementation, external AIR executor, synthesis/reduction tools, and regression tests.
- `schemas/` — machine-readable artifact schemas.
- `artifacts/multi-case/latest/` — controlled two-case evidence bundle.
- `docs/` — concise technical documentation for the implemented research system.
- `scripts/run-evaluation.ps1` — authoritative two-case runner.

## Reproducing the Two-Case Evaluation

The recorded evaluation environment used Windows 11, Python 3.13.7, and Rust/Cargo 1.97.1. The Rust toolchain is pinned in `rust-toolchain.toml`.

On Windows with Python and Rust available:

```powershell
.\run-evaluation.cmd
```

The runner executes the multi-case regression checks, rebuilds the generated Rust backends, executes the authoritative assurance pipeline, validates the aggregate schemas, and performs the clean-directory reproducibility check. Results are written to `artifacts/multi-case/latest/`.

## Interpretation Boundaries

AEROST is a host-based research prototype and candidate application/execution profile. The project does not establish an avionics industry standard, IEC 61131-3 conformance, DO-178C compliance, tool qualification, target-platform timing validity or WCET, hardware-in-the-loop or flight validation, aircraft-level safety, universal UAV applicability, formal compiler semantic preservation for every accepted program, or predictive effectiveness on unseen defects.

The external AIR executor is separately implemented, but it consumes the same AIR produced by the common frontend and is not a fully independent second ASCP frontend. The three-path comparison covers the controlled closure suites; the complete generated suite is evaluated through the assurance synthesis, reduction, and mutation pipeline rather than through the same full three-path replay boundary.

## Authors and Contributions

This research artifact was developed jointly by:

- **Mustafa Hilal** — avionics supervisory-control logic and application-case design, system requirements, assurance criteria and evaluation, controlled scenario design, and evidence analysis.

- **Edanur Arslan** — compiler and software-toolchain development, AIR execution, Safe Rust backend generation, assurance synthesis and reduction, test automation, and validation tooling.

- **Joint contributions** — research methodology, test and validation strategy, result interpretation, reproducibility analysis, and manuscript preparation.

## License

See `NOTICE.md`.
