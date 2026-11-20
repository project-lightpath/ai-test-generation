# FLPATH-5050 Quarkus blank-user test objective

Use the pinned Java 21 Quarkus application at
`project-lightpath/java-analysis-adapter@2aac181ae63d938c118bac133d33fb8ae19eeec9`
under `spike/java21-quarkus`.

A POST to `/transfers` with an empty `user` string must return HTTP 200,
preserve the supplied `requestReference`, and report `accepted=false`.
Choose a reference different from the existing test's `req-001`.

First inspect the supplied application code and existing test. Record
`analysis.json` with `relevant_files`, `existing_coverage`, `gap`, and
`feasibility`. Then record `plan.json` with `test_path`, `setup`, `assertions`,
`baseline_expectation`, and `mutant_expectation`. Identify the untested
blank-user behavior; the existing happy-path test does not cover it.

After the plan, create exactly one new native JUnit 5 / Quarkus test under
`src/test/java`, using the project's established RestAssured pattern. Assert
all three response properties above. Do not modify production code, existing
tests, build files, or dependencies. Do not run commands or use external
services. If a valid test is infeasible, record the reason instead of claiming
success. The evaluator will run the new test against the original application
and a hidden defective copy.
