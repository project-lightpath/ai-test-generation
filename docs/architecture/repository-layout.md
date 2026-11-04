# Repository Layout and Integration Map

The product repository is organized around contracts, ports, adapters, tests, experiments, and deployment composition. See the root `README.md` and `CONTRIBUTING.md` for the contributor workflow.

## Parallel Paths

| Workstream | Primary paths | Integration point |
|---|---|---|
| VI: VEX and policy input | `contracts/`, future production adapters, input fixtures | Versioned VPE/VEX and policy contracts |
| TF: target and framework | future production adapters, target metadata | Java 21/Quarkus profile and framework port |
| TG: core pipeline | `src/atg/domain/`, `src/atg/application/`, `src/atg/workflow/`, `src/atg/gates/`, `src/atg/provenance/` | Driver-neutral lifecycle and evidence contracts |
| TG-S1/S2/S3 | `experiments/`, replay tests, architecture docs | Spike reports and port/ownership recommendations |
| Final demo | `deploy/final-demo/`, runbooks | ATG public interfaces only |

## Change Rule

Cross-workstream changes update the contract or port first, add compatibility tests, identify affected owners, and link the Jira dependency. Do not import another workstream's internal implementation to avoid defining an interface.
