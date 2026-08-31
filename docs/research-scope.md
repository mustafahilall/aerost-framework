# AEROST Research Scope

## Objective

AEROST investigates whether a restricted, implementation-replaceable supervisory-control profile can provide deterministic application semantics together with reproducible machine-readable assurance evidence for a narrow class of avionics supervisory functions.

The study is software-only and host-based. It evaluates the profile and evidence pipeline using two controlled unmanned-aircraft supervisory applications.

## Evaluated Applications

### Dual-Source Electrical-Power Supervisor

The power case evaluates discrete supervisory behavior including initialization, source usability, source isolation, energy-conservation behavior, degraded and critical modes, invalid-input handling, runtime-fault response, reset, recovery, and conservative output requests.

The study evaluates supervisory policy expressed over validated logical classifications. It does not validate batteries, contactors, sensors, physical electrical behavior, or aircraft-level energy safety.

### Dual-Link Communication Supervisor

The communication case evaluates nominal, degraded, lost-link, recovery-pending, and lockout behavior together with link selection, command permission, invalid-input handling, runtime-fault response, reset, recovery, and conservative communication outputs.

The study evaluates supervisory policy over validated communication-status classifications. It does not validate radios, propagation, networking hardware, or aircraft-level communication safety.

## Profile and Execution Model

Both applications use ASCP-0.2 and the same generic toolchain. The implemented profile defines explicit retained state, restricted deterministic control constructs, separate transition and output stages, unconditional output defaults, and controlled assurance annotations.

Each logical cycle uses a complete immutable input snapshot, a private retained-state working copy, transition evaluation, output evaluation on the post-transition state, runtime-policy checks, conservative-output selection when required, atomic state/output commit, and bounded semantic and diagnostic events.

## Execution Paths

The controlled closure suites are evaluated through three paths:

1. a generic AIR reference interpreter;
2. generated Safe Rust compiled and executed as a separate process; and
3. a separately implemented external AIR executor.

The external AIR executor is implementation-separated from the reference evaluator but consumes the same exported AIR. It is therefore not a fully independent second ASCP frontend.

## Evidence Model

The research artifact includes requirement-linked evidence for statement, decision, condition, case-arm, selected MC/DC, runtime-fault, conservative-output, reset, and recovery obligations. Stable identifiers are carried through source, AIR, generated backend mappings, tests, and results.

Bounded breadth-first exploration over declared finite input domains generates stateful witness scenarios. Deterministic reduction is evaluated in two forms: obligation-only reduction and mutation-aware reduction. The latter additionally preserves distinctions induced by the predeclared controlled mutation profiles.

## Scenario Provenance

Requirement-oriented scenario suites are manually authored and linked to controlled high-level requirements. Structural-closure supplements are maintained separately and are deterministically selected or derived to close structural evidence gaps. Their union forms the controlled closure suite used for the three-path comparison.

Generated witness suites are produced separately by bounded exploration. They are not described as manually authored scenarios.

## Claim Boundary

The artifact supports claims only for the implemented ASCP-0.2 subset, the two evaluated applications, the declared bounded models, the recorded execution paths, the controlled scenarios, and the declared mutation profiles.

It does not establish certification, DO-178C compliance, tool qualification, target-platform validity or WCET, hardware-in-the-loop or flight validation, physical aircraft safety, universal avionics generality, formal semantic preservation for every accepted program, cross-platform reproducibility, or predictive defect-detection effectiveness beyond the declared mutation set.
