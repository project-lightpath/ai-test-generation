# FLPATH-5050 Spring Boot transfer test objective

Use the pinned Spring Boot application at
`lzspangler/spring-boot-example-application@01a18a1b40e151ffcc1d7bef86bf335f87999750`.

A customer should be able to transfer **exactly the full available balance**
from one account to a different account. The source should end at zero, the
target should receive the same amount, and no funds should disappear or appear.

Before writing a test, inspect the supplied production code and existing tests.
Record `analysis.json` with `relevant_files`, `existing_coverage`, `gap`, and
`feasibility`. Then record `plan.json` with `test_path`, `setup`, `assertions`,
`baseline_expectation`, and `mutant_expectation`. State a real coverage gap;
do not claim that tests of smaller transfers or insufficient funds cover the
exact-balance boundary.

Create exactly one new native JUnit test under `src/test/java`. Do not modify
existing tests, production code, build files, or the objective. The test must
assert successful completion and the resulting balances. Avoid external
services, network access, hardcoded credentials, and dependence on seeded
application database IDs. Use the repository's established test libraries.
If the transfer unexpectedly throws, express that as an assertion failure
(for example, with JUnit `assertDoesNotThrow`) instead of letting the exception
escape as an unhandled test error.

The seeded defect is held back from the candidate. Do not infer success from
compilation alone; the shared evaluator will run the generated test against
the original application and the hidden defective copy.
