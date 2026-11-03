# Adapters

Adapters implement ports and translate external systems into ATG contracts.

Adapter families:

- `input/`: VPE request and VEX format adapters.
- `policy/`: evaluated VPE decisions and optional policy-bundle adapters.
- `repository/`: immutable source retrieval and repository profiles.
- `framework/`: Java 21/Quarkus native framework behavior.
- `model/`: approved OpenAI-compatible endpoint profiles and model-task runners.
- `execution/`: Tekton or other approved execution providers.
- `evidence/`: artifact store, signer, verifier, and publication integrations.
- `source_control/`: conditional PR/MR compatibility behavior.

Every adapter needs contract tests and should provide a local fake, simulator, or replay path where practical.
