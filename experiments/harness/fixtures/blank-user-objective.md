# FLPATH-5050 shared candidate task

Repository: project-lightpath/java-analysis-adapter
Revision: 2aac181ae63d938c118bac133d33fb8ae19eeec9
Project: spike/java21-quarkus

Create one new native JUnit 5 / Quarkus test for POST /transfers.
Use either @QuarkusTest or @QuarkusIntegrationTest. The evaluator first
packages the application with tests skipped, then runs only your test class.
Submit JSON with an empty user string and a requestReference different from
the existing fixture test's req-001. Verify that the response has HTTP 200,
preserves the requestReference, and reports accepted=false.

Add only a new file under src/test/java. Do not modify application source,
existing tests, build files, or dependencies. Return the exact new file path
and its content. If you cannot generate a valid test, say why.
