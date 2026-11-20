# FLPATH-5050: two-task campaign and Aider diagnostic

**Status:** local experiment; no production selection or human `use`/`use_with_fork`/`reject` decision yet. All four formal decisions remain `insufficient_evidence`.

## Experiment and evidence

Four candidate tools ran with the same EnMaaS endpoint and `rits/zai-org/glm-5-3` model. They were asked to Analyze → Plan → Code a Java test. The shared offline evaluator ran each saved test on the original app and on a copy with one hidden defect. A test `discriminates` only when it passes on the original and fails **by assertion** on the defective copy.

The final machine report, original two-task report, and run archives are
historical evidence stored on the experiment host; they are not in this
repository. Use the run ID when requesting an archive:

- Quarkus staged run: `e27ce990-0ae3-41e1-a5b9-24cb84f34868`.
- Spring XML run: `03d151b0-9e35-4b54-b5df-1cf3d3935b56`.
- Aider-only Spring diagnostic: `6cc4c96e-7af0-4350-90d2-f3c057b8ed3e`.

All three archived summaries report `evidence_scan: passed`, and each archive has `archive-manifest.json` with hashes for retained files. The original Quarkus and Spring runs share runner and evaluator hashes. The Aider diagnostic keeps the runner hash but changes the run specification and evaluator hash, so it is a **third provenance cohort**, not a fixed-configuration repeat or a reliability estimate. The Quarkus run-001 pilot used an incomplete Maven cache and was stopped as **setup-invalid**; it is excluded.

## Measured results

Stage times below are **Analyze / Plan / Code / Execute** in seconds. Execute includes offline Maven evaluation. Sizes are saved Java test lines and bytes. Each configuration has one attempt; timings and outcomes are descriptive, not reliability estimates.

### Quarkus blank-user task

All four generated tests passed original code and failed by assertion on the seeded defect. Each test asserts HTTP 200, preservation of a new `requestReference`, and `accepted=false` for an empty user. Method coverage is `not_applicable`: this fixture has no JaCoCo method contract.

| Candidate | Result | Generated Java | Stage times (s) |
| --- | --- | ---: | --- |
| Aider | discriminates | 24 lines / 739 bytes | 119.5 / 90.5 / 41.4 / 87.0 |
| Pydantic AI | discriminates | 24 lines / 749 bytes | 80.5 / 16.9 / 17.8 / 88.3 |
| Claude Code | discriminates | 24 lines / 739 bytes | 53.0 / 29.3 / 26.9 / 88.8 |
| LangGraph adapter | discriminates | 24 lines / 730 bytes | 90.9 / 111.9 / 17.5 / 87.8 |

The generated tests and each candidate's `evaluation/evaluation.json` are in the host-local Quarkus archive listed above. Aider, Claude, Pydantic AI, and LangGraph all used the native Quarkus/RestAssured pattern.

### Spring XML lowercase-deposit task

In the four-tool Spring run, Aider timed out in Analyze after 600.0 seconds and produced no test. Its archived analysis log contains 40 repeated 104-line blocks; the origin of that loop is unknown. A single Aider-only diagnostic using the same model, objective/prompt, and 600-second Analyze limit then completed all stages. The other three tools also generated tests. All four saved Spring tests assert one imported transaction, the credited balance of 1100.00, and a saved deposit; each passed original code and failed by assertion on the hidden lower-case type defect.

| Candidate | Result | Generated Java | Stage times (s) |
| --- | --- | ---: | --- |
| Aider, original four-tool run | blocked: Analyze timeout | none | 600.0 / — / — / — |
| Aider, diagnostic | discriminates | 75 lines / 2911 bytes | 135.1 / 142.2 / 100.5 / 156.1 |
| Pydantic AI | discriminates | 86 lines / 3654 bytes | 146.8 / 77.9 / 43.1 / 158.3 |
| Claude Code | discriminates | 89 lines / 3505 bytes | 47.6 / 50.8 / 54.7 / 156.7 |
| LangGraph adapter | discriminates | 90 lines / 3865 bytes | 70.9 / 30.1 / 46.2 / 159.1 |

The original generated tests and each candidate's `evaluation/evaluation.json` are in the host-local Spring XML archive listed above; Aider's later test and evaluation are in the host-local diagnostic archive. The prior 121-test suite passes even on the defective app; the reference test and four generated tests expose the missed behavior.

**QE review of Aider diagnostic:** The 75-line JUnit 5/Mockito test creates a fresh account at 1000.00, imports a lower-case 100.00 deposit, and asserts count 1, balance 1100.00, and captured deposit type, amount, and balance-after. Baseline 1/1 passes; mutant 1/1 fails by assertion at the balance check. Because execution stops at that failure, the mutant run does not independently exercise the later type assertion. QE found no forbidden edit or shortcut. The shorter file mainly omits comments and setup splitting; it has no demonstrated quality advantage.

**Coverage correction:** The original four-tool Spring run and its automated report remain `coverage_unknown` because the configured Spring XML target was `StatementImportService.importStatement`, which has no branch counter. The changed condition is inside `StatementImportService.parseAndCreateTransactions`. The host-local derived coverage metrics were produced by [`reanalyze_coverage.py`](reanalyze_coverage.py). That tool verified the archived JaCoCo XML hashes against the archive manifest and found **45/45 lines and 25/28 branches before and after** adding each of the three successful tests (delta 0). Aider's original run is `unknown` because it produced no test or JaCoCo XML. Its separate diagnostic used the corrected method target and directly recorded **45/45 lines and 25/28 branches before and after** its test (delta 0). The first run's derivation is provenance-marked saved-XML analysis, **not** an evaluator replay or a change to the raw result. The zero delta does not negate defect detection: the old tests executed the branch without asserting the lower-case behavior.

## Review and candidate disposition

The QE semantic review checked the generated Java tests against each task's required assertions. That review is distinct from the report's machine checks; the current machine report still marks `semantic review: pending`. Formal candidate statuses remain `insufficient_evidence` until a named reviewer records a decision with its authorization reference.

| Candidate | Current recommendation to investigate | Product-use question |
| --- | --- | --- |
| Pydantic AI | Continue; both tasks discriminated. | [MIT license](https://github.com/pydantic/pydantic-ai/blob/main/LICENSE); check adapter ownership, isolation, and repeatability. |
| LangGraph adapter | Continue; both tasks discriminated. | [MIT license](https://github.com/langchain-ai/langgraph/blob/main/LICENSE). [LangGraph is an orchestration framework](https://docs.langchain.com/oss/python/langgraph/overview); the custom adapter and workflow remain our responsibility. |
| Aider | Investigate the original Spring Analyze loop. The Aider-only diagnostic succeeded, but its changed spec/evaluator prevents a reliability estimate; neither one timeout nor one success settles use or rejection. | [Apache 2.0 license](https://github.com/Aider-AI/aider/blob/main/LICENSE.txt); explicitly configure and verify [analytics opt-out](https://github.com/Aider-AI/aider/blob/main/aider/website/docs/config/options.md), model gateway behavior, and timeout handling. |
| Claude Code | Keep as a benchmark pending product/legal review; both tasks discriminated. | Its [license](https://github.com/anthropics/claude-code/blob/main/LICENSE.md) reserves rights. [Anthropic's product terms](https://code.claude.com/docs/en/legal-and-compliance) address embedding, unmodified binary, and customer authentication. A fork of the Claude Code binary is not the proposed path. Obtain an applicable terms review before product integration. |

These are experiment follow-ups, **not** legal conclusions or product approvals. The same model was used for all tools, so this comparison does not evaluate model quality. A production decision also needs repeated fixed-configuration runs, more behavior types, security/isolation review, operating cost, and support for customer-provided model endpoints.

**Future-candidate security gate:** The security review found no blocker for this four-candidate campaign. A new third-party `/adapter` image is different: [`run_comparison.py`](run_comparison.py) passes it `ENMAAS_API_KEY` with host networking. Review and allowlist the exact image before a live run, and restrict its network egress or use a credential scoped to this experiment.

## Next validation

1. Keep the derived coverage JSON with the immutable archives. For future runs, configure the Spring XML coverage target as `parseAndCreateTransactions` so the evaluator records the method counters directly. The current raw run stays `coverage_unknown`.
2. If work resumes, investigate the source of Aider's repeated Analyze blocks and repeat with one fixed spec/evaluator to measure variability. The current diagnostic does not supply that repeat.
3. Add at least one distinct task or repository, then record named per-stage and overall `use`, `use_with_fork`, `reject`, or `insufficient_evidence` decisions. Document any bounded fork changes and rerun after them.
