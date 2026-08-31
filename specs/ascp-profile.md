# AEROST Supervisory Control Profile
## Supported declarations

- one top-level `PROGRAM`;
- enumeration `TYPE` declarations;
- `VAR_INPUT`, `VAR_OUTPUT`, and explicitly initialized `VAR_STATE` sections;
- `BOOL`, `U16`, `I32`, and declared enumeration types.

## Supported executable constructs

- deterministic assignment;
- `IF` / `ELSIF` / `ELSE`;
- exhaustive enumeration `CASE`;
- Boolean literals, integer literals, enumeration literals and variable references;
- `NOT`, `AND`, `OR`, equality/inequality and ordered integer comparisons;
- parentheses.

## Static restrictions

- no recursion, dynamic allocation, pointers, unbounded loops, floating point, direct hardware access, hidden global state, or silent extensions;
- inputs are read-only;
- the transition stage writes only retained state;
- the output stage writes only outputs;
- every output receives an unconditional default assignment before output-stage control flow;
- identifiers and enumeration arms are unique;
- every identifier resolves and every assignment/expression is type correct;
- enumeration `CASE` statements are exhaustive.

## Controlled annotations

`(*@req=... test=... claim-critical mcdc mutate=...*)` binds requirements, tests, claim-critical coverage, selected MC/DC and controlled mutation operators to the following construct. These annotations are assurance metadata and do not alter nominal application semantics.
