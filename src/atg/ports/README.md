# Ports

Ports are the stable interfaces between ATG core behavior and replaceable external implementations.

Expected port families include input, policy, repository/source, framework, model, execution, evidence, and conditional source-control publication.

Each port must document:

- Version and owner.
- Input/output schema.
- Authentication and authorization expectations.
- Failure, timeout, cancellation, and retry behavior.
- Local fake/simulator contract.
- Real integration test location.

Vendor SDK types, Tekton types, GitLab types, and model-framework types must stay inside adapters.
