# AEROST

AEROST is a deterministic supervisory-control framework for UAV avionics. It provides a restricted supervisory-control profile, explicit cycle semantics, an executable Assurance Intermediate Representation (AIR), generated Safe Rust backends, and reproducible validation and assurance tooling.

The framework is evaluated using two representative supervisory applications:

- dual-source electrical-power supervision;
- dual-link communication supervision.

## Overview

AEROST is designed around deterministic cyclic supervisory logic with explicit state handling, transition priorities, conservative output behavior, fault policies, and traceable execution identities.

The current implementation includes:

- the AEROST Supervisory Control Profile (ASCP-0.2);
- initialized retained state;
- explicit transition and output stages;
- deterministic conservative output defaults;
- atomic cycle commit behavior;
- lowering to an executable Assurance Intermediate Representation (AIR);
- a generic AIR reference interpreter;
- generated Safe Rust backends;
- a separately implemented external AIR executor;
- cycle-level differential execution;
- requirement-linked structural coverage;
- selected modified condition/decision coverage (MC/DC);
- bounded stateful scenario synthesis;
- deterministic assurance-suite reduction;
- AIR-level mutation analysis;
- schema validation;
- clean-directory reproducibility checks.

## Architecture

The main execution flow is:

```text
ASCP source
    |
    v
Parser and semantic analysis
    |
    v
Assurance Intermediate Representation (AIR)
    |
    +----------------------+
    |                      |
    v                      v
Reference AIR        Generated Safe Rust
Interpreter          Backend
    |                      |
    +----------+-----------+
               |
               v
        Differential Evaluation
               |
               v
      Assurance and Validation
             Evidence
```

A third execution path is provided by a separately implemented external AIR executor.

The external executor consumes the same AIR representation produced by the common frontend. It therefore provides implementation diversity at the AIR execution level, but it is not an independently implemented ASCP frontend.

## Controlled Applications

### Electrical-Power Supervisor

The power-supervision case evaluates deterministic supervisory behavior involving:

- source availability;
- essential-power conditions;
- reserve-energy conditions;
- load shedding;
- source isolation;
- critical-bus handling;
- return-to-home and landing requests;
- recovery staging;
- invalid or stale inputs;
- blocking runtime faults.

### Communication-Link Supervisor

The communication-supervision case evaluates:

- primary and secondary link selection;
- link degradation;
- persistent link loss;
- command-channel authentication;
- degraded and lost-link modes;
- recovery staging;
- return-to-home requests;
- invalid or stale inputs;
- blocking runtime faults.

Application-specific behavior is supplied through controlled source programs, requirements, runtime policies, scenarios, bounded input-domain definitions, and mutation profiles. The execution and assurance tooling is shared between both applications.

## Evaluation Results

The current baseline contains the following validated results:

| Measure | Result |
|---|---:|
| Controlled applications | 2 |
| Controlled closure scenarios | 95 |
| Controlled closure cycles | 132 |
| Three-path equivalent controlled cycles | 132 / 132 |
| Declared assurance obligations covered | 538 / 538 |
| Selected MC/DC objectives covered | 13 / 13 |
| Unreduced generated suite | 551 scenarios / 1,195 cycles |
| Three-path equivalent generated-suite cycles | 1,195 / 1,195 |
| Obligation-only reduced suite | 86 scenarios / 202 cycles |
| Mutation distinctions retained by obligation-only reduction | 11 / 16 |
| Mutation-aware reduced suite | 90 scenarios / 213 cycles |
| Mutation distinctions retained by mutation-aware reduction | 16 / 16 |
| Clean-directory reproducibility | 86 / 86 evaluation artifacts byte-identical |

The mutation-aware result is an in-sample preservation result. The same predeclared mutation profiles are used during reduction and final scoring; the result is therefore not a prediction of effectiveness against previously unseen defects.

The three-path execution results demonstrate bounded empirical agreement for the evaluated programs and scenarios. They do not constitute a formal proof of semantic preservation for every program accepted by the profile.

## Repository Layout

```text
applications/
    Application manifests and multi-case configuration.

examples/
    ASCP application sources, requirements, runtime policies,
    and mutation profiles.

specs/
    ASCP, execution-contract, AIR, conformance,
    bounded-domain, and assurance-obligation specifications.

tests/
    Requirement-oriented scenarios, structural-closure scenarios,
    and bounded input-domain definitions.

conformance/
    Accepted and rejected ASCP conformance programs.

tools/
    Compiler, execution, assurance, synthesis, reduction,
    mutation, validation, and regression tooling.

schemas/
    Machine-readable schemas for generated evidence.

artifacts/
    Generated evaluation and reproducibility evidence.

docs/
    Technical documentation.

scripts/
    Evaluation and automation scripts.
```

## Reproducing the Evaluation

The recorded environment uses:

- Windows 11;
- Python 3.13.7;
- Rust/Cargo 1.97.1.

The Rust toolchain is pinned through `rust-toolchain.toml`.

From PowerShell:

```powershell
.\run-evaluation.cmd
```

The evaluation process:

1. executes the regression checks;
2. rebuilds the generated Rust backends;
3. executes the controlled application suites;
4. executes the bounded generated suites;
5. compares the three execution paths;
6. evaluates assurance obligations;
7. performs suite reduction and mutation analysis;
8. validates generated schemas and artifacts;
9. performs the clean-directory reproducibility check.

Evaluation outputs are written under:

```text
artifacts/multi-case/latest/
```

## Interpretation Boundaries

AEROST is a host-based framework for deterministic supervisory-control experimentation and validation.

The current implementation does not establish:

- an avionics industry standard;
- IEC 61131-3 conformance;
- DO-178C compliance;
- development-tool qualification;
- target-platform WCET;
- schedulability guarantees;
- hardware-in-the-loop validation;
- flight validation;
- aircraft-level safety;
- universal applicability to UAV software;
- formal semantic preservation for every accepted program;
- predictive mutation effectiveness against unseen defects.

The external AIR executor is separately implemented at the AIR execution level but shares the AIR generated by the common frontend.

Across the complete bounded generated suite, the two controlled applications contain 551 scenarios and 1,195 cycles. The reference AIR interpreter, generated Safe Rust backend, and external AIR executor produced identical observable behavior on all 1,195 evaluated cycles.

## Contributors

**Mustafa Hilal**

Supervisory-control logic, application-case design, system requirements, assurance criteria, controlled scenario design, and evidence analysis.

**Eda Nur Arslan**

Compiler and software-toolchain development, AIR execution, Safe Rust backend generation, assurance-suite synthesis and reduction, test automation, and validation tooling.

Joint work includes system methodology, validation strategy, result interpretation, reproducibility analysis, and technical documentation.

## License

See `NOTICE.md`.
