# FLPATH-5047: Generate Bounded Native Tests with Exact Patch Provenance

Status: Local adapter-neutral Code boundary implemented; integration handoff pending
Parent: FLPATH-5030 AI Test Generation MVP
Depends on: FLPATH-5046 (approved plan + digest-bound checkpoint)
Upstream: FLPATH-4995/5002/5012 (fixture/profile/facts) consumed via neutral ports; NOT owned here
Prerequisite satisfied locally: FLPATH-5046 approved, digest-bound plan checkpoints

## 1. Ticket Intent (paste-ready replacement)

> Generate repository-native test changes only from an approved plan and objective set. Model or tool output is untrusted. Validate generated tests through bounded syntax, format, compile, collection, assertion-quality, and approved-path checks, and preserve exact patch and materialized-test provenance.

Acceptance (verbatim):
- Generation requires an approved, digest-bound plan checkpoint.
- Only approved repository paths may change.
- Repair attempts are bounded and every attempt is attributable to its input, tool, model, worker, and output digest.
- Tests with no collection, no meaningful assertion, weakened assertions, or wrong targets fail quality validation.
- Generated content and patch identity are persisted before execution.

Refs: `flpath-5030-ticket-realignment.md:239-254`, `epic-technical-guidance.md:1967-1976`

## 2. Scope boundaries

Owned here:
- `contracts/v1/generation.schema.json` (already exists, may need tightening), `atg.generation` document
- `src/atg/domain/generation.py` - patch digest, allowed-path, artifact closure rules
- `src/atg/application/generation.py` - `GenerationService` orchestrating approve -> generate -> validate -> persist
- `src/atg/ports/generation.py` - `GenerationPort` (model/tool surface), `ValidationPort`
- `src/atg/adapters/generation/` - deterministic fake validator for local/component tests; NO live model SDK
- `src/atg/gates/generation.py` - collection/assertion/weakening gates (deterministic; model cannot override)
- `src/atg/adapters/persistence/sqlite_run_state.py` - `generations` table + atomic `record_generation`
- `src/atg/component/run_state_server.py` - `POST /generations` + `GET /generations/{id}`
- `contracts/failure-register/v1/cases.json` - new FR-GEN-* cases

Not owned here (explicit non-goals):
- VEX/policy parsing (FLPATH-5017), Java fixture/profile/analyzer producer (FLPATH-4995/5012/5002), execution provider (FLPATH-5048), differential/quality classifier (FLPATH-5049), harness evaluation (FLPATH-5050)
- Live LLM, Tekton, signing, evidence-store - fakes only; real integration is `tests/integration/`
- Production auth - reuse existing `Idempotency-Key` + `validate_identifier` pattern; no new auth model

## 3. Contracts

Existing `atg.generation` v1 (the current repository contract):
```
required: [contract, version, generation_id, run_id, plan_id, plan_revision, plan_artifact_digest, plan_checkpoint_id, plan_decision_id, base, candidate, patch_digest, materialized_test_digest, subject_materializations, model, tool, worker, attempts, artifacts, status, created_at]
base/candidate: common#/$defs/identity (git-tree)
changed_files required for generated/verified outcomes; constrained to approved test roots
each attempt binds patch + materialized artifacts, changed files, checks, model/tool/worker identity, and status
subject_materializations binds the materialized-test digest separately to base and candidate
status: generated | verified | rejected | inconclusive
```

Tightenings to add (non-breaking, additive `allOf`):
- `patch_digest` and `materialized_test_digest` must be `sha256:` + 64 hex; successful `changed_files` are non-empty, unique, rooted in plan-owned `src/test/` or `tests/`, and cannot traverse
- `plan_artifact_digest` must equal run_state `active_plan_artifact_digest` at generation start

Example already valid: `contracts/examples/generation.json` (generation-001, base=candidate trees, patch_digest, changed_files=[src/test/java/AppTest.java], model=approved-model)

New event linkage:
- `generation_started` already in `run_state.py:EVENT_TYPES` - requires `generation_id`
- `generation_completed` requires `generation_id + artifact_id + artifact_digest`; rejected/inconclusive outcomes have distinct terminal events
- Generation does NOT create its own checkpoint kind; it is gated by `plan_review` checkpoint + `code_review` prerequisite on `completed_generation_*` (see run_state.py:200-204). This matches TG-5 -> TG-6 handoff.

## 4. Run-state integration

Current state machine handles generation and recoverable attempts:

```
planning --plan_submitted--> planning --planning_completed--> planning --checkpoint_created(plan_review)--> plan_review --decision_recorded(approved)--> coding --generation_started--> coding --(completed | rejected | inconclusive)--> coding
```

Relevant guards (run_state.py):
- `generation_started` requires `active_plan_id != None` and matches `event.plan_id` if supplied (line 299-302)
- `generation_started` clears `completed_generation_*` to prevent stale proof (line 330-333)
- `code_review` requires `completed_generation_*` triple present AND `generation_in_progress == false` (line 412-416) - in-progress check runs first
- `plan_submitted` revision monotonic within cycle; `return_to_analyze` clears active plan for new cycle

Implementation rule: `GenerationService.generate()`:
1. Load `run_state` -> assert `status == coding` (i.e., plan approved, not terminal)
2. Load `plan` via `store.get_plan(plan_id)` -> `manifest.status == approved` + `digest == plan_artifact_digest` + checkpoint `state == approved`
3. Emit `generation_started` (revision-checked), then run bounded tool loop, then emit `generation_completed` with `artifact_digest = sha256(patch_bytes)` and `artifact_id = artifact-{generation_id}`
4. Persist `atg.generation` with the terminal event, after the start event and every bounded attempt have been committed. A restart with the same request resumes from the stored attempt number.

Duplicate handling: same `generation_id + request_digest` -> idempotent 200; conflicting digest -> `GenerationConflictError` 409 (parity with planning/feedback).

## 5. Domain rules (treat model output as untrusted)

All deterministic, no model override:

1. **Approved-path gate**: `changed_files` must be subset of `plan.allowed_paths` (currently discovered test roots carried into the plan). Reject absolute paths, traversal, unsupported extensions, and paths outside the approved roots. Patch headers, base contents, and materialized contents must agree exactly.

2. **Patch provenance**: `patch_digest = sha256(exact UTF-8 patch bytes)`. Patch headers must exactly match persisted materialized file paths. Patch bytes and materialized file contents are stored as content-addressed artifacts and recomputed on read (row/bundle mismatch -> `ContractValidationError`).

3. **Syntax/format/compile/collection**: delegated to `ValidationPort.validate(candidate)`. The default local adapter can parse and compile Python source and check basic formatting; it does not claim framework collection, Java parsing, or compilation. Unsupported checks are `not_supported`, producing an explicit `inconclusive` result. A profile-backed adapter supplied after the 5050/fixture decisions must provide real format, compile, and collection checks for the target framework; test-runner execution remains 5048.

4. **Assertion-quality gate** (`src/atg/gates/generation.py`):
   - Collector reports zero tests -> `rejected: no tests collected`; no configured collector -> `inconclusive`
   - No `assert*` / `assertThat` / `assertEquals` in added lines -> `rejected: no meaningful assertion`
   - Weakened assertion (e.g., `assertTrue(true)`, removed assertion vs baseline) -> `rejected: weakened assertion`
   - Wrong target (test does not reference `plan.objective_refs` symbols) -> `rejected`

5. **Bounded repair**: `max_attempts = 3` (constant, not model-controlled). Each attempt records input/output digests, model, tool, worker, checks, and exact artifact IDs. Every attempt is committed to `generation_attempts` before requesting the next candidate. Exhaustion -> `status=inconclusive`, not silent pass.

6. **Materialized-test equivalence**: the canonical sorted path-to-content map is digested as `materialized_test_digest` and bound to both immutable subject identities. Exact contents are stored as an artifact. `FilesystemPatchMaterializer` applies only canonical patches into configured workspaces with descriptor-relative, no-follow path handling, base-content checks, atomic writes, and post-write digest verification.

## 6. Persistence

New table (sqlite_run_state.py, mirrors `plans`/`analyses`):
```sql
CREATE TABLE generations (
  generation_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  plan_id TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  bundle_json TEXT NOT NULL
);
CREATE INDEX generations_by_run ON generations(run_id);
CREATE INDEX generations_by_plan ON generations(plan_id);

CREATE TABLE generation_attempts (
  generation_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  request_digest TEXT NOT NULL,
  document_json TEXT NOT NULL,
  artifacts_json TEXT NOT NULL,
  PRIMARY KEY (generation_id, attempt)
);
```

Methods:
- `get_generation(generation_id) -> GenerationRecord | None` (validates row metadata vs bundle, like `get_plan`)
- `record_generation_attempt(...)` commits each attributable attempt before repair
- `record_generation(record, started_event, terminal_event) -> GenerationRecord` atomically applies successful, rejected, or inconclusive lifecycle completion and verifies attempt closure
- Backfill: none needed (new table); `_backfill_checkpoint_projections` unchanged.

## 7. Ports & adapters

```
src/atg/ports/generation.py:
  @dataclass PatchCandidate(patch_text, materialized_files, base_files)
  @dataclass ValidationResult(syntax_ok, format_ok, compile_ok, collected, unsupported)
  class GenerationPort(Protocol): generate(request, attempt, feedback) -> PatchCandidate | None
  class ValidationPort(Protocol): validate(candidate) -> ValidationResult
  class PatchMaterializationPort(Protocol): materialize(candidate, workspace, digests) -> MaterializationResult
```

Adapters:
- `adapters/generation/deterministic.py` - local static validation plus request-backed bounded generation replay; no subprocess or live model
- Future: `adapters/generation/maven_validator.py` (executes `mvn -Dtest=...` via approved command from profile, not model output) - out of scope for 5047, stub only

Credentials: no new secrets; model identity is `common#/$defs/identity` (kind=model, id=model-alias, version=model-version). Execution credentials remain separate (TG-6).

## 8. HTTP surface

Reuses `run_state_server.py` patterns (Idempotency-Key, BadRequest vs Conflict):

```
POST /generations
  header: Idempotency-Key
  body: {generation_id, plan_id, run_id, expected_revision, created_at, patch_text, base_files, materialized_files, base, candidate, model, tool, worker, repair_candidates?}
  201 created, 200 duplicate (same digest), 400 BadRequest (missing/invalid plan, bad digest, path traversal), 409 PlanningConflictError (plan not approved/digest mismatch), 409 GenerationConflictError (generation_id bound to different inputs)
  side-effect: emits generation_started + one terminal event; persists attempts and atg.generation bundle

GET /generations/{generation_id} -> 200 bundle or 404
GET /runs/{run_id}/generations -> list (optional)
```

Checkpoint decision flow unchanged; generation is NOT a checkpoint itself. `code_review` creation will still require `completed_generation_*`.

## 9. Test plan (local, no cluster/model/execution needed)

Unit (`tests/unit/`):
- `test_generation.py` - duplicate idempotency, conflict on different patch, approved-plan guard, digest mismatch, stale revision
- `test_generation_gates.py` - allowed-path, traversal, no-collection, no-assert, weakened assertion, wrong target

Contract (`tests/contract/`):
- `test_schema_documents.py` - validate `generation.json` example, add invalid fixture `generation-bad-path.json` (path traversal) -> 400
- `test_contract_integrity.py` - generation requires approved plan, correct plan_revision/checkpoint/decision bindings, artifact digest equality

Component (`tests/component/test_generation_component.py`):
- Full HTTP round-trip: admit -> analyze (mock) -> plan -> approve -> POST /generations -> verify `generation_completed` state + persisted bundle match
- Negative: unapproved plan -> 409, bad path -> 400, duplicate -> 200, stale expected_revision -> 409 StaleEventError

Replay/evaluation:
- `tests/replay/test_generation_replay.py` - restart SQLite, verify attributed repair artifacts, and replay the lifecycle journal deterministically
- Add failure-register cases: FR-GEN-001 (unapproved path rejected), FR-GEN-002 (no assertion rejected), FR-GEN-003 (bounded repair exhaustion)

## 10. Work breakdown (TG workstream path: domain, application, workflow, gates, provenance)

| Step | Owner | Files | Done when |
|------|-------|-------|-----------|
| 1. Domain model + digest helpers | TG | `src/atg/domain/generation.py`, `tests/unit/test_generation.py` | Complete |
| 2. Persistence | TG | `sqlite_run_state.py` + migration | Complete |
| 3. Gates (deterministic) | TG | `src/atg/gates/generation.py` | Complete |
| 4. Application service | TG | `src/atg/application/generation.py` | Complete |
| 5. Ports + development adapters | TG | `ports/generation.py`, `adapters/generation/` | Complete |
| 6. HTTP wiring | TG | `component/run_state_server.py` | Complete |
| 7. Contract integrity + schema tools | TG | `domain/contract_integrity.py`, `scripts/validate_contracts.py` | Complete |
| 8. Failure-register + docs | TG | `contracts/failure-register/v1/cases.json`, `docs/architecture/flpath-5047-generation.md` | Complete |

## 11. Remaining Local Path and 5050 Handoff

5047 must finish the stable, provider-neutral seam; it must not select an LLM,
agent framework, model endpoint, or Tekton responsibility split. Those are the
decision outputs of FLPATH-5050 and FLPATH-5051. The request-backed generator is
a deterministic test adapter only, not a production generator.

Before treating 5047 as locally complete:

1. **Complete:** generation and validation adapters are injectable through the
   local service composition, so 5050 can exercise a fake or endpoint without
   changing domain/application code.
2. **Complete:** the bounded, symlink-safe materialization adapter applies only
   canonical patches into configured workspaces and verifies patch/base/result
   content digests.
3. **Complete:** `SscTektonCompatibilityResult` is an
   optional adapter output (`TESTS_ADDED`, `PR_URL`, `RUN_REF`, `EVIDENCE_REF`,
   terminal status). Keep canonical ATG records authoritative and do not make
   Tekton mandatory or select it by implementing the projection.
4. **Complete:** generation starts visibly before candidate output; attempts
   persist sequentially and resume after restart; request keys and material
   inputs are bound; per-subject test digests are included.
5. **Complete for the provider-neutral boundary:** isolated workspace,
   restart/replay, path-escape/symlink, digest-equivalence, and adapter-injection
   tests pass. The default validator deliberately returns `inconclusive` for
   unsupported Java/framework compile or collection checks instead of claiming
   success.

The remaining 5047 implementation after the 5050 decision is to add the selected
generator adapter and target-profile validation adapter behind these ports.
5050 can compare harness/model options against the same `GenerationPort`,
`ValidationPort`, approved plan, deterministic fixture, and persisted result.
The evaluation should record candidate name/version/configuration/endpoint,
commands, observations, limitations, security/licensing/ownership findings,
and recommendation; it must not silently make the exercised provider a runtime
dependency. 5048 consumes the versioned generated artifacts and materialization
manifest, while 5049 evaluates execution evidence and quality outcomes.

## 12. Non-breaking change safety

- `contracts/v1/generation.schema.json` is already published; only additive `allOf` constraints allowed (no required-field removal). Use `contracts/overrides/<profile>/` only if pre-consumer destructive experiment needed (per AGENTS.md contract rule).
- `run_state` statuses unchanged; generation uses existing `coding` status + `completed_generation_*` fields.
- SQLite: new table only; no ALTER of existing tables.

## 13. Resolved implementation decisions

- Allowed paths are derived from Analyze's discovered test inventory, persisted on the approved plan, and enforced against both diff headers and materialized file paths.
- Model, tool, and worker identities are required by the local domain and successful-generation schema projection.
- Every repair has an attempt document and two content-addressed artifacts (patch and materialized tests).
- Tekton compatibility: `TESTS_ADDED` + `EVIDENCE_REF` emission is TG-6 (FLPATH-5048) responsibility; 5047 only ensures patch is materializable at that digest.

## 14. Validation commands

```bash
.venv/bin/pip install -e '.[test]'
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/validate_contracts.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests/unit -p 'test_*.py' -v
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests/component -p 'test_*.py' -v
```
