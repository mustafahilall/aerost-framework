# AEROST Tool and Profile Requirements

- **ASCP-REQ-001**: Parse all supported declarations and executable constructs into a source-spanned abstract syntax tree.
- **ASCP-REQ-002**: Reject prohibited constructs, unresolved names, duplicates, type errors, stage-write violations, missing state initializers, non-exhaustive cases, and missing output defaults.
- **AIR-REQ-001**: Lower every accepted program into executable AIR without case-study-specific execution code.
- **AIR-REQ-002**: Preserve separate source, AIR, requirement, and test identities.
- **EXEC-REQ-001**: Execute the transition stage before the output stage and compute outputs from post-transition state.
- **EXEC-REQ-002**: Validate a versioned runtime-fault/output policy against the compiled ASCP symbol table and preserve it in AIR.
- **EXEC-REQ-003**: Reference execution and generated Rust shall apply runtime-fault and diagnostic behavior only from AIR execution-contract metadata, not from application-specific identifier conventions.
- **GEN-REQ-001**: Generate Safe Rust from AIR.
- **GEN-REQ-002**: Produce a standalone Cargo crate and deterministic source map.
- **GEN-REQ-003**: Generated application behavior shall not call the reference interpreter.
- **DIFF-REQ-001**: Compare every controlled cycle and fail on one unexplained mismatch.
- **COV-REQ-001**: Derive statement, decision, and condition coverage from AIR identities and execution events.
- **COV-REQ-002**: The controlled research baseline shall execute every executable AIR statement and both outcomes of every declared AIR decision and atomic condition included in the obligation model; any open declared obligation shall block the authoritative pipeline.
- **MCDC-REQ-001**: Compute selected MC/DC independence pairs from recorded condition vectors.
- **MUT-REQ-001**: Apply controlled mutations to AIR and evaluate the declared scenario suites against the resulting behavior.
- **TRACE-REQ-001**: Calculate separate requirement-to-source-to-AIR-to-backend and requirement-to-test-to-result boundaries.
- **SCHEMA-REQ-001**: Validate controlled artifacts against the repository JSON Schemas.
- **BUILD-REQ-001**: Identical controlled inputs and versions shall produce byte-identical authoritative source artifacts in clean directories on the recorded host environment.
