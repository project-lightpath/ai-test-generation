# FLPATH-5047: Generate Bounded Native Tests with Exact Patch Provenance

Status: Draft plan (TG-5 - Code)
Parent: FLPATH-5030 AI Test Generation MVP
Depends on: FLPATH-5046 (approved plan + digest-bound checkpoint)
Upstream: FLPATH-4995/5002/5012 (fixture/profile/facts) consumed via neutral ports; NOT owned here
Blocked by: FLPATH-5046 review checkpoint semantics; TF-3 declared capabilities (path allowlist, framework identity)

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
- `patch_digest` must be `sha256:` + 64 hex, `changed_files` non-empty, unique, rooted (`src/test/...`), no `..` traversal
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

2. **Patch provenance**: `patch_digest = sha256(normalized_patch)` where normalized_patch = `lf + strip trailing whitespace + sorted diff headers`. Stored as `payload_digest` on `run_events` + `generation.patch_digest`. Must match recomputed digest on read (row vs bundle mismatch -> `ContractValidationError`).

3. **Syntax/format/compile/collection**: delegated to `ValidationPort.validate(patch)` returning `ValidationResult(collected: int, syntax_ok, compile_ok, errors[])`. For local dev, `FakeValidationAdapter` checks: file parses (python `ast` for py fixture, or `javac --dry` stub), `collected > 0`, else `rejected`.

4. **Assertion-quality gate** (`src/atg/gates/generation.py`):
   - No `collected` -> `rejected: no tests collected`
   - No `assert*` / `assertThat` / `assertEquals` in added lines -> `rejected: no meaningful assertion`
   - Weakened assertion (e.g., `assertTrue(true)`, removed assertion vs baseline) -> `rejected: weakened assertion`
   - Wrong target (test does not reference `plan.objective_refs` symbols) -> `rejected`

5. **Bounded repair**: `max_attempts = 3` (constant, not model-controlled). Each attempt records `Attempt{model_id, tool, input_digest, output_digest, created_at}`. Exhaustion -> `status=inconclusive`, not silent pass. Every attempt persisted before next.

6. **Materialized-test equivalence**: `changed_files` content digested per-file -> `materialized_digest = sha256(sorted(file_digests))` stored in `generation.artifacts[0].digest`. Used by TG-6 to verify workspace == approved artifact.

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
```

Methods:
- `get_generation(generation_id) -> GenerationRecord | None` (validates row metadata vs bundle, like `get_plan`)
- `record_generation(record, started_event, completed_event) -> GenerationRecord` (BEGIN IMMEDIATE, check duplicate generation_id, `_apply_locked` both events, insert, COMMIT; rollback on any failure)
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
- `adapters/generation/fake_validator.py` - local syntax/collection checks, no subprocess
- `adapters/generation/fake_model.py` - deterministic `FakeModelAdapter` returning canned patch for contract tests
- Future: `adapters/generation/maven_validator.py` (executes `mvn -Dtest=...` via approved command from profile, not model output) - out of scope for 5047, stub only

Credentials: no new secrets; model identity is `common#/$defs/identity` (kind=model, id=model-alias, version=model-version). Execution credentials remain separate (TG-6).

## 8. HTTP surface

Reuses `run_state_server.py` patterns (Idempotency-Key, BadRequest vs Conflict):

```
POST /generations
  body: {generation_id, plan_id, run_id, expected_revision, created_at, model?}
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
- `tests/replay/test_generation_replay.py` - replay canned `FakeModel` patches through validator deterministically
- Add failure-register cases: FR-GEN-001 (unapproved path rejected), FR-GEN-002 (no assertion rejected), FR-GEN-003 (bounded repair exhaustion)

## 10. Work breakdown (TG workstream path: domain, application, workflow, gates, provenance)

| Step | Owner | Files | Done when |
|------|-------|-------|-----------|
| 1. Domain model + digest helpers | TG | `src/atg/domain/generation.py`, `tests/unit/test_generation.py` | `GenerationRecord` round-trips, digest recompute rejects tamper |
| 2. Persistence | TG | `sqlite_run_state.py` + migration | `get/generation` row/bundle mismatch raises, atomic 3-row commit |
| 3. Gates (deterministic) | TG | `src/atg/gates/generation.py` | No-collection/no-assert/weakening/wrong-target all -> rejected |
| 4. Application service | TG | `src/atg/application/generation.py` | Missing approval -> 409, bounded attempts persisted, status correct |
| 5. Ports + fakes | TG | `ports/generation.py`, `adapters/generation/` | Contract tests pass with fakes |
| 6. HTTP wiring | TG | `component/run_state_server.py` | 201/200/400/409 as above; BadRequest vs Conflict not conflated (fix prior bug in checkpoint route) |
| 7. Contract integrity + schema tools | TG | `domain/contract_integrity.py`, `scripts/validate_contracts.py` | Generation binding failures explicit |
| 8. Failure-register + docs | TG | `contracts/failure-register/v1/cases.json`, `docs/architecture/flpath-5047-generation.md` | Cases deterministic, replayable without live provider |

## 11. Non-breaking change safety

- `contracts/v1/generation.schema.json` is already published; only additive `allOf` constraints allowed (no required-field removal). Use `contracts/overrides/<profile>/` only if pre-consumer destructive experiment needed (per AGENTS.md contract rule).
- `run_state` statuses unchanged; generation uses existing `coding` status + `completed_generation_*` fields.
- SQLite: new table only; no ALTER of existing tables.

## 12. Open questions (record, do not block)

- Allowed-path source of truth: `plan.steps[].command` vs TF-3 `allowed_test_roots`? Decision: intersect both; log which wins. Needs A&P confirm.
- Model identity attribution depth: `model` field optional in schema; for 5047 make it required in domain but not in contract (add to `allOf` when present). Keep `worker/tool` provenance for TG-6.
- Repair observability: store each attempt as separate `artifact` entry vs `limitations` array? Proposal: `attempts` array in manifest + artifact per attempt for digest verification.
- Tekton compatibility: `TESTS_ADDED` + `EVIDENCE_REF` emission is TG-6 (FLPATH-5048) responsibility; 5047 only ensures patch is materializable at that digest.

## 13. Validation commands

```bash
.venv/bin/pip install -e '.[test]'
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/validate_contracts.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests/unit -p 'test_*.py' -v
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests/component -p 'test_*.py' -v
```
