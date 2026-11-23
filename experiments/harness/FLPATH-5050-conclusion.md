# FLPATH-5050: Candidate Conclusion

**Status:** Aider, Pydantic AI, and the LangGraph adapter are promising candidates
for further evaluation. The available evidence is not yet sufficient to make
a production selection. Claude Code remains a useful benchmark; review its
[product terms](https://code.claude.com/docs/en/legal-and-compliance) and
licensing before deciding whether it fits the intended product.

## What was tested

Each candidate received the same objective and pinned Java application for a
task. It produced a test through Analyze → Plan → Code. A shared offline
evaluator then ran the test on the original code and a copy containing a
seeded defect. A result is **discriminates** when the test passes on the
original and fails by assertion on the defect. The objectives already specify
the behavior and required assertions, so these runs do not establish that a
candidate independently discovers a coverage gap or selects what to test.

All four candidates detected the Quarkus blank-user defect. Pydantic AI,
LangGraph, and Claude Code detected the Spring XML lowercase-deposit defect in
the four-candidate run. Aider timed out in Analyze after 600 seconds in that
run, then detected the Spring defect in a separate Aider-only diagnostic.

The Aider diagnostic used a different run specification and evaluator revision.
It is a separate evidence cohort. One timeout and one success do not establish
Aider's reliability or prove the timeout is fixed. These checks cover seeded
application defects; they do not prove a CVE fix.

## Generated test size

Each successful Quarkus test contained **24 lines**. For the successful Spring
tests of the three candidates under consideration, the sizes were:

1. Aider: **75 lines** in the separate diagnostic.
2. Pydantic AI: **86 lines**.
3. LangGraph adapter: **90 lines**.

A shorter test is not automatically better. Review its assertions and
maintainability as well as its size.

## Coverage

For the Spring target method, the existing suite covered **45/45 lines** and
**25/28 branches**. The numbers stayed the same after each successful generated
test was added, even though the tests detected the seeded defect.

For Pydantic AI, LangGraph, and Claude Code, these method counters were derived
after the run from saved JaCoCo XML, because the original report targeted the
wrong method. Aider's separate diagnostic used the corrected method target and
measured the same counters directly. The original Aider run has no test or
coverage result. Quarkus coverage was **not measured**.

Coverage records what code ran; it cannot establish that a test checks the
right behavior.

## Timing

The following times are the sum of **Analyze + Plan + Code**, in seconds.
They exclude Execute, which uses the shared evaluator. These are single
attempts; they are not reliability estimates or definitive rankings.

| Task | Candidate | Analyze + Plan + Code |
| --- | --- | ---: |
| Quarkus | Pydantic AI | 115.2 s |
| Quarkus | LangGraph adapter | 220.3 s |
| Quarkus | Aider | 251.4 s |
| Spring XML | LangGraph adapter | 147.2 s |
| Spring XML | Pydantic AI | 267.8 s |
| Spring XML | Aider, separate diagnostic | 377.8 s |

The Spring Aider timing is from the separate diagnostic and is not a
fixed-configuration repetition of the original Spring campaign. Differences
can reflect the task, tool, model response, and execution environment. Do not
compare times across the two tasks as a tool ranking.

## Next steps

Repeat the tasks under one pinned configuration, including failures and
timeouts. Add further behaviors, review each stage and generated test, and
check model compatibility, security, licensing, support, and maintenance.
Then record a reviewed per-candidate and per-stage decision.

See the [campaign findings](FLPATH-5050-campaign-findings.md) for all four
candidates, stage measurements, and provenance. The
[evaluation architecture](../../docs/architecture/flpath-5050-evaluation-architecture.md)
explains the flow.
