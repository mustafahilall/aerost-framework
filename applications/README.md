# AEROST Applications

The research system contains two controlled application manifests:

- `power-supervisor.application.json`
- `communication-link-supervisor.application.json`

`research-suite.json` binds both applications to the shared multi-case assurance pipeline.

Each manifest identifies the controlled ASCP source, requirements, runtime-fault policy, scenario suites, bounded input domain, AIR artifact identity, and mutation profile for one application.

For schema compatibility, the manifest field `manual_closure_scenarios` is retained as an implementation key. The referenced closure file is a structural-closure supplement and must not be interpreted as uniformly manually authored. Requirement-oriented manual scenarios are identified separately by `manual_requirements_scenarios`.
