# AEROST Assurance Intermediate Representation
AIR is an executable, backend-neutral representation produced from a semantically valid ASCP program.

Each executable item preserves:

- a distinct normalized source identity (`ASCP-SRC-*`);
- a distinct AIR identity (`AIR-*`);
- source span;
- stage ownership;
- static type;
- requirement and test links;
- claim-critical, MC/DC and controlled-mutation metadata.

AIR statement forms are assignment, branching and enumeration case selection. Expressions are typed constants, reads, unary operations and binary operations. Canonical serialization is deterministic JSON. The generated source map links source identity → AIR identity → generated Rust block.

AIR also contains a versioned `execution_contract` object. It carries the validated runtime-fault and diagnostic policy for the compiled program. Runtime-fault retained assignments, conservative output assignments, state-to-output copies and latch diagnostics are represented as typed metadata. Reference execution and generated Rust consume the same metadata; neither implementation may select behavior using hard-coded application variable names.
