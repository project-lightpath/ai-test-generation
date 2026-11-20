# FLPATH-5050: EnMaaS gateway check (2026-11-17)

This run used the pinned Java 21/Quarkus fixture, the same blank-user objective,
and the same offline differential evaluator for each generated test. The key came
from a local file and is absent from these records.

| Candidate | Gateway/model | Generated test | Evaluator result |
| --- | --- | --- | --- |
| Aider 0.86.2 | `https://api.enmaas.devshift.net/v1` / `rits/zai-org/glm-5-3` | `aider/TransferResourceEmptyUserTest.java` | Original passes; seeded defect fails by assertion (`discriminates`) |
| Claude Code 2.1.296 | `https://api.enmaas.devshift.net` / `rits/zai-org/glm-5-3` | `claude-code/EmptyUserTransferTest.java` | Original passes; seeded defect fails by assertion (`discriminates`) |
| LangGraph 1.0.8 adapter | `https://api.enmaas.devshift.net/v1` / `rits/zai-org/glm-5-3` | None | Inconclusive: the response hit its 8192-token limit while still in reasoning; no Java test was produced |

The Aider and Claude Code directories contain the generated test, redacted
invocation/configuration, evaluator JSON, and Maven logs. The LangGraph directory
contains the redacted invocation/configuration, error, and response diagnostics.
An earlier Aider attempt accidentally omitted a Java source file during fixture
copying and was excluded; this record uses the corrected full fixture.

These results show one behavior check only. They do not establish a tool winner,
general model quality, or vulnerability remediation. LangGraph's result is a
model/adapter output-budget issue, not proof that the framework cannot generate
tests. Review generated assertions and repeat across additional tasks before
comparing candidates.

Gateway usage: [EnMaaS quickstart](https://source.redhat.com/departments/products_and_global_engineering/workload_management/enmaas#quickstart).
