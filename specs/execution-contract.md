# AEROST Deterministic Execution Contract
For each logical cycle:

1. platform data is acquired and validated outside the ASCP application;
2. a complete immutable input snapshot is formed;
3. resident retained state is copied into a private working state;
4. output working values are initialized;
5. transition-stage AIR executes exactly once;
6. output-stage AIR executes exactly once using post-transition working state;
7. runtime checks and the versioned execution-contract policy are evaluated;
8. normal or conservative outputs are selected;
9. retained state and outputs are committed atomically;
10. bounded statement, decision, condition, case-arm and diagnostic events are recorded.

A blocking runtime fault is handled by `execution_contract.runtime_fault_policy` in AIR. The policy declares:

- whether normal commit is inhibited;
- retained-state assignments applied on a blocking fault;
- conservative output assignments;
- any output copied from a declared retained-state variable; and
- the emitted diagnostic identity.

Diagnostic behavior is declared separately by `execution_contract.diagnostic_policy`, including the state-transition diagnostic and any retained latch values that emit bounded diagnostics.

The interpreter and Rust generator must consume this metadata. They must not infer safety behavior from application-specific variable names such as `State`, `BlockingFaultLatched`, or `BlockingPowerFault`. Policy targets and copied values are type-checked against the ASCP symbol table before AIR is accepted.

Recovery remains distinct from restart and requires explicit guards and authorization in the ASCP program.
