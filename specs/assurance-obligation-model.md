# AEROST Assurance Obligation Model

## Status

This document defines the controlled research model used to represent assurance objectives that can be exercised by bounded scenario synthesis and evaluated through exported execution evidence. It is normative for the AEROST research pipeline; it is not a certification standard and does not claim DO-178C compliance or certification credit.

## Purpose

The model separates three activities:

1. extraction of generic assurance obligations from AIR and declared metadata;
2. bounded synthesis of input sequences that witness reachable obligations; and
3. validation of reported witnesses, reductions, and aggregate results against exported evidence.

An obligation identifies a measurable execution event or relation. It does not prove that a requirement is correct, complete, safe, or properly allocated.

## Inputs

The obligation extractor consumes published AIR and controlled metadata, including the execution contract, runtime-fault policy, requirement links, selected MC/DC declarations, and profile/schema identifiers. Generic tooling shall not infer obligations from application-specific variable names or domain vocabulary.

## Obligation Kinds

| Kind | Satisfaction rule |
|---|---|
| `STATEMENT_REACHED` | The identified AIR statement appears in a recorded statement event. |
| `DECISION_TRUE` | The identified AIR decision records outcome `true`. |
| `DECISION_FALSE` | The identified AIR decision records outcome `false`. |
| `CONDITION_TRUE` | The identified atomic AIR condition records value `true`. |
| `CONDITION_FALSE` | The identified atomic AIR condition records value `false`. |
| `MCDC_PAIR` | Two recorded evaluations demonstrate the declared condition's independent effect on its decision while the other declared conditions remain fixed. |
| `CASE_ARM_REACHED` | The identified `CASE` arm appears in a recorded case-arm event. |
| `FAULT_POLICY_ACTIVATED` | The declared blocking-fault policy is activated and its diagnostic identity is recorded. |
| `NORMAL_COMMIT_INHIBITED` | A cycle records that normal output commit was inhibited. |
| `CONSERVATIVE_OUTPUT_APPLIED` | The declared conservative-output policy is applied and committed outputs equal the policy result. |
| `RESET_PATH_EXECUTED` | The declared reset path is taken and the required retained-state effect is observed. |
| `RECOVERY_PATH_EXECUTED` | A declared recovery transition is taken under its authorization guard. |
| `REQUIREMENT_EXERCISED` | At least one controlled witness is linked to the identified requirement root. |

## Obligation Records

Each obligation records a deterministic identifier, kind, subject identities, associated requirements, bounded reachability status, kind-specific attributes, and a concise description.

Allowed reachability states are:

- `UNKNOWN` — extraction completed but bounded search has not classified the obligation;
- `REACHABLE` — a conforming witness sequence has been produced;
- `UNREACHABLE_WITHIN_BOUND` — no witness was found within the declared search domain and bounds.

`UNREACHABLE_WITHIN_BOUND` shall never be interpreted as globally unreachable.

## Deterministic Identity Rules

Obligation identifiers are derived from canonical semantic identities rather than line numbers, filesystem paths, timestamps, traversal order, or process identifiers. Current forms include:

```text
OBL-STMT-<statement-id>
OBL-DEC-<decision-id>-TRUE
OBL-DEC-<decision-id>-FALSE
OBL-COND-<condition-id>-TRUE
OBL-COND-<condition-id>-FALSE
OBL-MCDC-<decision-id>-<condition-id>
OBL-CASE-<case-arm-id>
OBL-FAULT-<fault-policy-id>
OBL-COMMIT-INHIBITED-<fault-policy-id>
OBL-CONSERVATIVE-OUTPUT-<fault-policy-id>
OBL-RESET-<transition-or-policy-id>
OBL-RECOVERY-<transition-id>
OBL-REQ-<requirement-id>
```

Invalid identity characters are rejected rather than silently normalized. Canonical serialization of identical controlled inputs shall produce byte-identical obligation artifacts.

## Bounded Search Contract

Each synthesis run declares its finite input domains, initial retained state, maximum sequence depth, state/resource limits, and deterministic enumeration order. Completeness statements apply only to the declared bounded model. Reaching a search limit must be reported explicitly and must not be reported as proof of unreachability.

## Witness Contract

A witness sequence satisfies an obligation only when it:

1. conforms to the controlled scenario schema;
2. executes under the declared profile and execution contract;
3. contains the required semantic event or state relation in exported traces;
4. matches its recorded expected state and output values; and
5. is linked to the target obligation identifier.

For `MCDC_PAIR`, both evaluations and the independence relation are required.

## Suite Reduction

Obligation-only reduction selects a deterministic subset of candidate scenarios while preserving the declared witness requirements. Mutation-aware reduction additionally preserves the predeclared mutation distinctions used by the controlled experiment. Neither reduction mode is claimed to find a globally minimum suite.

## Application Independence

The obligation extractor, synthesizer, reducers, and evidence validators shall not contain power-management, communication-link, or other case-specific execution rules. Case-specific information enters only through controlled AIR, metadata, input-domain, requirement, mutation-profile, or scenario artifacts.

## Output Artifacts

The current pipeline emits machine-readable evidence including:

- `assurance-obligations.json`;
- `synthesized-scenarios.json`;
- `synthesis-report.json`;
- `suite-reduction-report.json`;
- `mutation-aware-suite-reduction-report.json`; and
- `assurance-suite-comparison.json`.

Each artifact identifies the relevant schema/profile/program identities and generation method.

## Claim Boundary

The model supports claims about declared obligations and witnesses within the evaluated bounded models. It does not support claims of unbounded completeness, global minimum suite size, proof of requirement correctness, proof of aircraft-level safety, certification credit, or correctness for unsupported language or AIR constructs.
