# AEROST Bounded Input-Domain Contract
## Status

This document defines the controlled input-domain contract used by the
assurance-directed scenario-sequence synthesis experiment.

The contract is an experimental research artifact. It does not define aircraft
operational envelopes or certified environmental assumptions.

## 1. Purpose

The input-domain document defines the finite set of input snapshots that a
bounded synthesis run may apply to an accepted AIR program. It separates the
search space from application execution logic and makes the experiment
repeatable.

## 2. Required Inputs

A domain validator consumes:

- an accepted AIR document;
- one input-domain document;
- the input-domain JSON schema.

The validator shall not import compiler, AIR interpreter, backend generator, or
application-specific execution code.

## 3. Program Binding

Every input-domain document shall identify:

- `profile_version`;
- `program_id`;
- `schema_version`;
- `domain_id`.

The profile and program identities shall match the supplied AIR document.

## 4. Input Coverage

Every AIR input shall occur exactly once in `inputs`.

The input records shall appear in AIR declaration order. Missing, duplicate,
unknown, or reordered inputs are invalid because they would make deterministic
input-vector enumeration ambiguous.

## 5. Supported Value Domains

Version 0.1 supports finite domains for:

- `BOOL` values;
- named enumeration values declared by the AIR program;
- bounded integer values explicitly listed in the domain document.

Every declared value shall be compatible with the corresponding AIR input type.
An empty value set is invalid. Duplicate values are invalid.

## 6. Enumeration Strategy

Version 0.1 defines the `cartesian` strategy. The candidate input alphabet is
the Cartesian product of the value sets in AIR declaration order.

For Boolean values the canonical order is `false` before `true`.
Enumeration shall be deterministic and shall not depend on hash-map iteration,
filesystem order, locale, or process scheduling.

The total input-vector count is:

`product(len(input.values) for input in inputs)`

The count shall not exceed `maximum_input_vectors`.

## 7. Constraints

Version 0.1 reserves a `constraints` array for future generic restrictions.
The controlled power-supervisor baseline uses an empty array so that all 14
Boolean inputs are explored without application-specific filtering.

A synthesis implementation shall reject unsupported constraint kinds rather
than silently ignore them.

## 8. Search Bounds

The domain document shall declare:

- `maximum_sequence_depth`;
- `maximum_reachable_states`;
- `maximum_transition_evaluations`.

These bounds define the experimental search envelope. Reaching a bound shall be
reported explicitly and shall not be represented as proof that an obligation is
unreachable.

## 9. Initial-State Policy

Version 0.1 uses `air_initializers`. The synthesis run begins from the retained
state and local-state initializers published in the AIR document.

## 10. Deterministic Identity

`domain_id` is a controlled identifier for the domain specification. The input
document bytes shall also be hashed by the pipeline. Repeated validation of the
same AIR and domain shall produce byte-identical validation reports.

## 11. Application-Independence Requirement

The validator and later synthesizer shall derive input names and types from AIR.
They shall not contain power-management, communication-link, battery, payload,
or mission-specific variable names or rules.

The controlled domain file may name the inputs of its bound program; the generic
validator and synthesis algorithm may not hard-code those names.

## 12. Claim Boundary

A complete Cartesian input alphabet does not imply unbounded behavioral
completeness. The experiment may claim only completeness relative to:

- the declared finite input value sets;
- the AIR initial state;
- the supported execution semantics;
- the declared sequence and resource bounds.
