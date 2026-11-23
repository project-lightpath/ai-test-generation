# FLPATH-5050: ENMAAS package-first experiment

This experiment checks whether tests produced through Aider and Pydantic AI
catch one known behavior change in a pinned Java 21/Quarkus fixture. Both used
ENMAAS `gpt-oss-120b` and the same [shared task](../../fixtures/blank-user-objective.md)
(`sha256:b433ea91f94e46ab00a36f25a435b8471ebad93ce8d0113651678be49c1e3c02`).
The fixture is `project-lightpath/java-analysis-adapter` at
`2aac181ae63d938c118bac133d33fb8ae19eeec9`, with verified tree digest
`sha256:5e19aa18e2019ecf9fe2559648002bac97467da685f5ef3fb4eefa0251080efe`.

## Results

- **Manual control:** [evaluation](control/evaluation.json) returned
  `discriminates`: the test passed on the unchanged fixture and failed by
  assertion on the seeded defect. No model generated this control test.
- **Aider 0.86.2:** [test](aider/TransferResourceEmptyUserTest.java) and
  [evaluation](aider/evaluation.json) returned `discriminates` with one
  baseline pass and one mutant assertion failure. The runtime was
  `quay.io/sscdemo/ai-agent-maven-aider:v1.0.1`, image ID
  `sha256:80d5a7e0cbd074f9b76c7f0db7441404521055268e3ba7d04df5d1bd2e91b456`.
  [Aider](https://github.com/Aider-AI/aider) is owned by Aider-AI and licensed
  Apache-2.0; the upstream Git commit behind this image was not recorded.
- **Pydantic AI 2.68.0:** [test](pydantic-ai/TransferResourceEmptyUserTest.java)
  and [evaluation](pydantic-ai/evaluation.json) returned `discriminates` with
  one baseline pass and one mutant assertion failure. The
  [adapter](pydantic-ai/adapter.py) digest is
  `sha256:93f3d5c6c945ebd11e0c286c798e478029666b53a8a89cb96b1ea0201f493223`;
  [requirements](pydantic-ai/requirements.txt) pin the library.
  [Pydantic AI](https://github.com/pydantic/pydantic-ai) is owned by Pydantic
  and licensed MIT. Its upstream Git commit was not recorded.

Every evaluation used the same offline Maven 3.9.11 / Java 21 image, ID
`b9611ad9b2f6ebef1c9369a32d794718fc28ebe2b9bb37b81a64b731370907bd`.
Packaging and candidate-test execution used separate network-disabled
containers, each with the same resource limits and read-only Maven cache.

## Reproduce

Use the [experiment guide](../../README.md) for the full evaluator command,
prerequisites, and expected result. Set the fixture root to the pinned revision,
use a warmed offline Maven cache, and choose a new output directory. Pass a
candidate test and its matching `invocation.json` from this folder. The
evaluator packages each fixture copy before running only the submitted class:

```sh
mvn -o -B -ntp -Ptest -DskipTests package
mvn -o -B -ntp -Ptest -Dtest=TransferResourceEmptyUserTest -Dsurefire.failIfNoSpecifiedTests=true test
```

Each candidate's `invocation.json` and `sanitized-config.json` record the
generation inputs. The `redacted_command` arrays are summaries: they shorten
`--read` paths and contain credential placeholders, so they are not
copy-pasteable shell commands. The exact original generation shell commands
were not retained. The saved outputs can be re-evaluated with the guide above;
reproducing the original generation call requires reconstructing its mounts
and paths from the sanitized configuration. The Pydantic AI adapter and pinned
requirement are included for a new run. Supply credentials through the
environment; no credential is stored here. Model output may vary on a rerun.

Each `evaluation.json` retains the absolute scratch paths from its original
run. For the four referenced Maven logs, use the files beside that evaluation
record: `baseline-package.log`, `baseline-test.log`, `mutant-package.log`, and
`mutant-test.log`. Their basenames and contents were preserved when the evidence
was curated.

## Limits and decision

The checks cover one transfer behavior and one seeded defect. Aider is a coding
agent; Pydantic AI is an orchestration library exercised through a small
adapter. These outcomes do not rank the tools against each other or establish
one provider for Analyze, Plan, Code, and Execute. Earlier runs used a different
prompt hash or Maven command and remain historical, separate from these runs.
Generated source, logs, and Surefire XML are untrusted review evidence.
These live runs used `sealusa35` scratch workspaces without an enforced total
disk quota, as the evaluator reports. The results are experimental review
evidence, not a security assurance. Tool maintenance, broader security
behavior, recovery, and multi-task quality have not been evaluated here.

**Decision:** retain both as experiments. Do not promote either to a production
ATG dependency from this single task.
