# FLPATH-5047: Generate Bounded Native Tests with Exact Patch Provenance

Status: Implemented locally (TG-5 - Code)
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

Existing `atg.generation` v1 (already published):
```
required: [contract, version, generation_id, run_id, plan_id, plan_revision, plan_artifact_digest, plan_checkpoint_id, plan_decision_id, base, candidate, status, created_at]
base/candidate: common#/$defs/identity (git-tree)
patch_digest + changed_files required when status in [generated, verified]
artifacts: common#/$defs/artifact[]
model: common#/$defs/identity
status: generated | verified | rejected | inconclusive
```

Tightenings to add (non-breaking, additive `allOf`):
- `patch_digest` and `materialized_test_digest` must be `sha256:` + 64 hex; successful `changed_files` are non-empty, unique, rooted in plan-owned `src/test/` or `tests/`, and cannot traverse
- `plan_artifact_digest` must equal run_state `active_plan_artifact_digest` at generation start

Example already valid: `contracts/examples/generation.json` (generation-001, base=candidate trees, patch_digest, changed_files=[src/test/java/AppTest.java], model=approved-model)

New event linkage:
- `generation_started` already in `run_state.py:EVENT_TYPES` - requires `generation_id`
- `generation_completed` already requires `generation_id + artifact_id + artifact_digest`
- Generation does NOT create its own checkpoint kind; it is gated by `plan_review` checkpoint + `code_review` prerequisite on `completed_generation_*` (see run_state.py:200-204). This matches TG-5 -> TG-6 handoff.

## 4. Run-state integration

Current state machine already handles generation (verified 2026-11-16):

```
planning --plan_submitted--> planning --planning_completed--> planning --checkpoint_created(plan_review)--> plan_review --decision_recorded(approved)--> coding --generation_started--> coding --generation_completed--> coding --checkpoint_created(code_review)--> code_review
```

Relevant guards (run_state.py):
- `generation_started` requires `active_plan_id != None` and matches `event.plan_id` if supplied (line 299-302)
- `generation_started` clears `completed_generation_*` to prevent stale proof (line 330-333)
- `code_review` requires `completed_generation_*` triple present AND `generation_in_progress == false` (line 412-416) - in-progress check runs first
- `plan_submitted` revision monotonic within cycle; `return_to_analyze` clears active plan for new cycle

Implementation rule: `GenerationService.generate()` must:
1. Load `run_state` -> assert `status == coding` (i.e., plan approved, not terminal)
2. Load `plan` via `store.get_plan(plan_id)` -> `manifest.status == approved` + `digest == plan_artifact_digest` + checkpoint `state == approved`
3. Emit `generation_started` (revision-checked), then run bounded tool loop, then emit `generation_completed` with `artifact_digest = sha256(patch_bytes)` and `artifact_id = artifact-{generation_id}`
4. Persist `atg.generation` document atomically with the `generation_completed` event (same pattern as `record_plan`).

Duplicate handling: same `generation_id + request_digest` -> idempotent 200; conflicting digest -> `GenerationConflictError` 409 (parity with planning/feedback).

## 5. Domain rules (treat model output as untrusted)

All deterministic, no model override:

1. **Approved-path gate**: `changed_files` must be subset of `plan.allowed_paths` (derived from `plan.steps[].expected_evidence` + TF-3 `allowed_test_roots`). Reject if any path outside, if path contains `..`, absolute, or `src/main` edits. Tested via `tests/unit/test_generation_gates.py`.

2. **Patch provenance**: `patch_digest = sha256(exact UTF-8 patch bytes)`. Patch headers must exactly match persisted materialized file paths. Patch bytes and materialized file contents are stored as content-addressed artifacts and recomputed on read (row/bundle mismatch -> `ContractValidationError`).

3. **Syntax/format/compile/collection**: delegated to `ValidationPort.validate(candidate)` returning `ValidationResult(collected, syntax_ok, format_ok, compile_ok, errors, limitations)`. The local deterministic adapter parses Python, applies bounded static Java/Kotlin syntax checks, rejects format and compile-error markers, and derives collection from materialized source. It records that these development checks do not prove live framework execution.

4. **Assertion-quality gate** (`src/atg/gates/generation.py`):
   - No `collected` -> `rejected: no tests collected`
   - No `assert*` / `assertThat` / `assertEquals` in added lines -> `rejected: no meaningful assertion`
   - Weakened assertion (e.g., `assertTrue(true)`, removed assertion vs baseline) -> `rejected: weakened assertion`
   - Wrong target (test does not reference `plan.objective_refs` symbols) -> `rejected`

5. **Bounded repair**: `max_attempts = 3` (constant, not model-controlled). Each attempt records input/output digests, model, tool, worker, checks, and exact artifact IDs. Every attempt is committed to `generation_attempts` before requesting the next candidate. Exhaustion -> `status=inconclusive`, not silent pass.

6. **Materialized-test equivalence**: the canonical sorted path-to-content map is digested as `materialized_test_digest` and stored with exact contents as an artifact. TG-6 can verify workspace bytes against this artifact.

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
  @dataclass GenerationRequest(generation_id, plan_id, run_id, expected_revision, created_at, model_ref)
  @dataclass GenerationRecord(request_digest, manifest, artifacts, attempts, duplicate)
  class GenerationPort(Protocol): generate(request, patch_candidate) -> PatchAttempt
  class ValidationPort(Protocol): validate(patch_bytes) -> ValidationResult
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
  body: {generation_id, plan_id, run_id, expected_revision, created_at, patch_text, materialized_files, base, candidate, model, tool, worker, repair_candidates?}
  201 created, 200 duplicate (same digest), 400 BadRequest (missing/invalid plan, bad digest, path traversal), 409 PlanningConflictError (plan not approved/digest mismatch), 409 GenerationConflictError (generation_id bound to different inputs)
  side-effect: emits generation_started + generation_completed + persists atg.generation bundle

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

## 11. Non-breaking change safety

- `contracts/v1/generation.schema.json` is already published; only additive `allOf` constraints allowed (no required-field removal). Use `contracts/overrides/<profile>/` only if pre-consumer destructive experiment needed (per AGENTS.md contract rule).
- `run_state` statuses unchanged; generation uses existing `coding` status + `completed_generation_*` fields.
- SQLite: new table only; no ALTER of existing tables.

## 12. Resolved implementation decisions

- Allowed paths are derived from Analyze's discovered test inventory, persisted on the approved plan, and enforced against both diff headers and materialized file paths.
- Model, tool, and worker identities are required by the local domain and successful-generation schema projection.
- Every repair has an attempt document and two content-addressed artifacts (patch and materialized tests).
- Tekton compatibility: `TESTS_ADDED` + `EVIDENCE_REF` emission is TG-6 (FLPATH-5048) responsibility; 5047 only ensures patch is materializable at that digest.

## 13. Validation commands

```bash
.venv/bin/pip install -e '.[test]'
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/validate_contracts.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests/unit -p 'test_*.py' -v
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests/component -p 'test_*.py' -v
```
