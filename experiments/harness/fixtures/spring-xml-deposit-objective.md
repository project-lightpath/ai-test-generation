# FLPATH-5050 Spring Boot XML statement test objective

Use the pinned Spring Boot application at
`lzspangler/spring-boot-example-application@01a18a1b40e151ffcc1d7bef86bf335f87999750`.

When a customer imports an XML statement, the transaction type `deposit` in
lower case should be treated as a deposit. Importing one such transaction for
100.00 into an account with 1000.00 should import one transaction, credit the
account to 1100.00, and record a deposit rather than a withdrawal.

Before writing a test, inspect the supplied production code and existing tests.
Record `analysis.json` with `relevant_files`, `existing_coverage`, `gap`, and
`feasibility`. Then record `plan.json` with `test_path`, `setup`, `assertions`,
`baseline_expectation`, and `mutant_expectation`. State the uncovered casing
behavior; uppercase-only import tests do not cover it.

Create exactly one native JUnit test under `src/test/java`. Do not modify
existing tests, production code, build files, or the objective. Assert the
import count, resulting balance, and saved transaction type. Avoid external
services, network access, credentials, and dependence on seeded database IDs.
Use the repository's established test libraries.

The seeded defect is held back from the candidate. The shared evaluator will
run the generated test against the original application and a hidden defective
copy. A compiling or passing test alone is insufficient evidence.
