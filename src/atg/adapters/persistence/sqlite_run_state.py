"""SQLite reference implementation of the ATG run-state persistence port.

This is a local component/reference adapter, not the final Java/Quarkus
service. SQLite gives the tests real transaction, restart, and compare-and-swap
behavior without introducing a service or infrastructure dependency.
"""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Iterator

from atg.application.admission import AdmissionConflictError, AdmissionResult, CheckResult
from atg.application.analysis import AnalysisConflictError, AnalysisRecord
from atg.application.feedback import FeedbackConflictError
from atg.application.planning import PlanRecord, PlanningConflictError, validate_plan_record
from atg.domain.schema_validation import ContractValidationError
from atg.domain.run_state import (
    DuplicateEventError,
    InvalidTransitionError,
    RunEvent,
    RunState,
    StaleEventError,
    TransitionResult,
    event_fingerprint,
    validate_identifier,
    validate_timestamp,
)


class SQLiteRunStateStore:
    """Transactional snapshot plus append-only event journal."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        """Open or create the SQLite snapshot, journal, and admission tables."""
        self._lock = RLock()
        self._connection = sqlite3.connect(
            str(database), isolation_level=None, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                revision INTEGER NOT NULL,
                state_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_events (
                event_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                applied_revision INTEGER NOT NULL,
                payload_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS run_events_by_run
                ON run_events(run_id, applied_revision);
            CREATE TABLE IF NOT EXISTS admissions (
                idempotency_key TEXT PRIMARY KEY,
                request_digest TEXT NOT NULL,
                result_json TEXT NOT NULL,
                terminal_json TEXT
            );
            CREATE TABLE IF NOT EXISTS analyses (
                analysis_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                request_digest TEXT NOT NULL,
                bundle_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS analyses_by_run ON analyses(run_id);
            CREATE TABLE IF NOT EXISTS plans (
                plan_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                request_digest TEXT NOT NULL,
                bundle_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS plans_by_run ON plans(run_id);
            CREATE TABLE IF NOT EXISTS checkpoints (
                checkpoint_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                kind TEXT NOT NULL,
                document_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS checkpoints_by_run ON checkpoints(run_id);
            CREATE TABLE IF NOT EXISTS feedback (
                feedback_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                request_digest TEXT NOT NULL,
                document_json TEXT NOT NULL
            );
            """
        )
        admission_columns = {
            row["name"] for row in self._connection.execute("PRAGMA table_info(admissions)")
        }
        if "terminal_json" not in admission_columns:
            self._connection.execute("ALTER TABLE admissions ADD COLUMN terminal_json TEXT")
        self._connection.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS admissions_by_request
                ON admissions(json_extract(result_json, '$.request_id'));
            CREATE UNIQUE INDEX IF NOT EXISTS admissions_by_run
                ON admissions(json_extract(result_json, '$.run_id'));
            """
        )

    def close(self) -> None:
        """Close the SQLite connection."""
        with self._lock:
            self._connection.close()

    def get_admission(self, idempotency_key: str) -> AdmissionResult | None:
        """Load an admission result by transport idempotency key."""
        with self._lock:
            row = self._connection.execute(
                "SELECT result_json FROM admissions WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        if row is None:
            return None
        return _admission_from_document(json.loads(row["result_json"]))

    def get_terminal(self, run_id: str) -> dict | None:
        """Load the durable terminal record associated with a run."""
        with self._lock:
            row = self._connection.execute(
                "SELECT terminal_json FROM admissions WHERE json_extract(result_json, '$.run_id') = ?",
                (run_id,),
            ).fetchone()
        return json.loads(row["terminal_json"]) if row is not None and row["terminal_json"] else None

    def get_admission_for_run(self, run_id: str) -> AdmissionResult | None:
        """Load the durable admission result associated with a run."""
        with self._lock:
            row = self._connection.execute(
                "SELECT result_json FROM admissions WHERE json_extract(result_json, '$.run_id') = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return _admission_from_document(json.loads(row["result_json"]))

    def get_analysis(self, analysis_id: str) -> AnalysisRecord | None:
        """Load a completed analysis bundle by stable identity."""
        with self._lock:
            row = self._connection.execute(
                "SELECT analysis_id, run_id, request_digest, bundle_json FROM analyses WHERE analysis_id = ?", (analysis_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            record = AnalysisRecord.from_storage_document(json.loads(row["bundle_json"]))
            if (
                row["analysis_id"] != record.manifest.get("analysis_id")
                or row["run_id"] != record.manifest.get("run_id")
                or row["request_digest"] != record.request_digest
            ):
                raise ContractValidationError("persisted analysis row metadata differs from bundle")
            return record
        except ContractValidationError:
            raise
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ContractValidationError("persisted analysis bundle is corrupt") from exc

    def get_plan(self, plan_id: str) -> PlanRecord | None:
        """Load a durable plan bundle by stable identity."""
        with self._lock:
            row = self._connection.execute(
                "SELECT plan_id, run_id, request_digest, bundle_json FROM plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            record = PlanRecord.from_storage_document(json.loads(row["bundle_json"]))
            if row["plan_id"] != record.manifest.get("plan_id") or row["run_id"] != record.manifest.get("run_id") or row["request_digest"] != record.request_digest:
                raise ContractValidationError("persisted plan row metadata differs from bundle")
            return record
        except ContractValidationError:
            raise
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ContractValidationError("persisted plan bundle is corrupt") from exc

    def record_plan(self, record: PlanRecord, submission_event: RunEvent, checkpoint_event: RunEvent) -> PlanRecord:
        """Atomically persist a plan bundle and its submission/checkpoint events."""
        validate_plan_record(record)
        plan_id = str(record.manifest["plan_id"])
        run_id = str(record.manifest["run_id"])
        if submission_event.run_id != run_id or checkpoint_event.run_id != run_id:
            raise ValueError("plan events must belong to the plan run")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute("SELECT request_digest, bundle_json FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
                if existing is not None:
                    if existing["request_digest"] != record.request_digest:
                        raise PlanningConflictError("plan identity is bound to different inputs")
                    self._connection.execute("COMMIT")
                    return PlanRecord.from_storage_document(json.loads(existing["bundle_json"]), duplicate=True)
                self._apply_locked(submission_event)
                self._apply_locked(checkpoint_event)
                self._connection.execute(
                    "INSERT INTO plans(plan_id, run_id, request_digest, bundle_json) VALUES (?, ?, ?, ?)",
                    (plan_id, run_id, record.request_digest, json.dumps(record.to_storage_document(), sort_keys=True)),
                )
                self._connection.execute(
                    "INSERT INTO checkpoints(checkpoint_id, run_id, kind, document_json) VALUES (?, ?, ?, ?)",
                    (record.checkpoint["checkpoint_id"], run_id, record.checkpoint["kind"], json.dumps(record.checkpoint, sort_keys=True)),
                )
                self._connection.execute("COMMIT")
                return record
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def record_feedback_revision(self, feedback, record: PlanRecord, return_event: RunEvent, submission_event: RunEvent, checkpoint_event: RunEvent) -> PlanRecord:
        """Atomically persist feedback, return-to-plan, and a superseding plan."""
        document = feedback.to_document()
        request_digest = feedback.digest()
        validate_plan_record(record)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute("SELECT request_digest FROM feedback WHERE feedback_id = ?", (feedback.feedback_id,)).fetchone()
                if existing is not None:
                    if existing["request_digest"] != request_digest:
                        raise PlanningConflictError("feedback identity is bound to different inputs")
                    stored = self.get_plan(record.manifest["plan_id"])
                    self._connection.execute("COMMIT")
                    return PlanRecord(stored.request_digest, stored.manifest, stored.checkpoint, stored.provenance, stored.artifacts, True)
                predecessor = self._connection.execute(
                    "SELECT bundle_json FROM plans WHERE plan_id = ?", (feedback.plan_id,)
                ).fetchone()
                if predecessor is None or json.loads(predecessor["bundle_json"])["manifest"].get("status") != "approved":
                    raise FeedbackConflictError("feedback competes with an existing plan revision")
                self._apply_locked(return_event)
                self._apply_locked(submission_event)
                self._apply_locked(checkpoint_event)
                old_bundle = json.loads(predecessor["bundle_json"])
                old_bundle["manifest"]["status"] = "superseded"
                old_bundle["checkpoint"] = dict(old_bundle["checkpoint"])
                old_bundle["checkpoint"].update({"state": "superseded", "superseded_by": record.checkpoint["checkpoint_id"]})
                self._connection.execute(
                    "UPDATE plans SET bundle_json = ? WHERE plan_id = ?",
                    (json.dumps(old_bundle, sort_keys=True), feedback.plan_id),
                )
                old_checkpoint = self._connection.execute(
                    "SELECT document_json FROM checkpoints WHERE checkpoint_id = ?", (feedback.checkpoint_id,)
                ).fetchone()
                if old_checkpoint is not None:
                    old_document = json.loads(old_checkpoint["document_json"])
                    old_document.update({"state": "superseded", "superseded_by": record.checkpoint["checkpoint_id"]})
                    self._connection.execute(
                        "UPDATE checkpoints SET document_json = ? WHERE checkpoint_id = ?",
                        (json.dumps(old_document, sort_keys=True), feedback.checkpoint_id),
                    )
                self._connection.execute("INSERT INTO feedback(feedback_id, run_id, request_digest, document_json) VALUES (?, ?, ?, ?)", (feedback.feedback_id, feedback.run_id, request_digest, json.dumps(document, sort_keys=True)))
                document["resolution"] = "applied"
                self._connection.execute(
                    "UPDATE feedback SET document_json = ? WHERE feedback_id = ?",
                    (json.dumps(document, sort_keys=True), feedback.feedback_id),
                )
                self._connection.execute("INSERT INTO plans(plan_id, run_id, request_digest, bundle_json) VALUES (?, ?, ?, ?)", (record.manifest["plan_id"], record.manifest["run_id"], record.request_digest, json.dumps(record.to_storage_document(), sort_keys=True)))
                self._connection.execute("INSERT INTO checkpoints(checkpoint_id, run_id, kind, document_json) VALUES (?, ?, ?, ?)", (record.checkpoint["checkpoint_id"], record.manifest["run_id"], record.checkpoint["kind"], json.dumps(record.checkpoint, sort_keys=True)))
                self._connection.execute("COMMIT")
                return record
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def get_feedback(self, feedback_id: str) -> dict | None:
        """Return a persisted feedback document by ID."""
        with self._lock:
            row = self._connection.execute("SELECT document_json FROM feedback WHERE feedback_id = ?", (feedback_id,)).fetchone()
        return json.loads(row["document_json"]) if row is not None else None

    def record_analysis(
        self,
        record: AnalysisRecord,
        start_event: RunEvent,
        outcome_event: RunEvent,
        checkpoint_event: RunEvent | None,
    ) -> AnalysisRecord:
        """Atomically persist analysis output and its gated lifecycle outcome."""
        analysis_id = str(record.manifest["analysis_id"])
        run_id = str(record.manifest["run_id"])
        _validate_analysis_record(record, start_event, outcome_event, checkpoint_event)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    "SELECT request_digest, bundle_json FROM analyses WHERE analysis_id = ?",
                    (analysis_id,),
                ).fetchone()
                if existing is not None:
                    if existing["request_digest"] != record.request_digest:
                        raise AnalysisConflictError("analysis identity is bound to different inputs")
                    stored_record = AnalysisRecord.from_storage_document(
                        json.loads(existing["bundle_json"]), duplicate=True
                    )
                    self._connection.execute("COMMIT")
                    return stored_record
                self._apply_locked(start_event)
                self._apply_locked(outcome_event)
                if checkpoint_event is not None:
                    self._apply_locked(checkpoint_event)
                    self._connection.execute(
                        "INSERT INTO checkpoints(checkpoint_id, run_id, kind, document_json) VALUES (?, ?, ?, ?)",
                        (record.checkpoint["checkpoint_id"], run_id, record.checkpoint["kind"], json.dumps(record.checkpoint, sort_keys=True)),
                    )
                self._connection.execute(
                    "INSERT INTO analyses(analysis_id, run_id, request_digest, bundle_json) VALUES (?, ?, ?, ?)",
                    (
                        analysis_id,
                        run_id,
                        record.request_digest,
                        json.dumps(record.to_storage_document(), sort_keys=True),
                    ),
                )
                self._connection.execute("COMMIT")
                return record
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def record_admission(self, key: str, request_digest: str, result: AdmissionResult, event: RunEvent) -> AdmissionResult:
        """Atomically persist admission, terminal record, and run transition."""
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    "SELECT request_digest FROM admissions WHERE idempotency_key = ?", (key,)
                ).fetchone()
                if existing is not None:
                    if existing["request_digest"] != request_digest:
                        raise AdmissionConflictError("idempotency key is bound to a different request")
                    stored = self._connection.execute(
                        "SELECT result_json FROM admissions WHERE idempotency_key = ?", (key,)
                    ).fetchone()
                    self._connection.execute("COMMIT")
                    return _admission_from_document(json.loads(stored["result_json"]))
                existing_identity = self._connection.execute(
                    "SELECT request_digest, result_json FROM admissions "
                    "WHERE json_extract(result_json, '$.request_id') = ? "
                    "OR json_extract(result_json, '$.run_id') = ?",
                    (result.request_id, result.run_id),
                ).fetchone()
                if existing_identity is not None:
                    if existing_identity["request_digest"] != request_digest:
                        raise AdmissionConflictError("request or run identity is bound to a different admission")
                    stored_result = _admission_from_document(json.loads(existing_identity["result_json"]))
                    self._connection.execute("COMMIT")
                    return stored_result
                self._connection.execute(
                    "INSERT OR IGNORE INTO runs(run_id, revision, state_json) VALUES (?, ?, ?)",
                    (event.run_id, 0, json.dumps(RunState(run_id=event.run_id, updated_at=event.occurred_at).to_document(), sort_keys=True)),
                )
                self._apply_locked(event, allow_unrecorded_admission=True)
                self._connection.execute(
                    "INSERT INTO admissions(idempotency_key, request_digest, result_json, terminal_json) VALUES (?, ?, ?, ?)",
                    (
                        key,
                        request_digest,
                        json.dumps(result.to_document(), sort_keys=True),
                        json.dumps(result.terminal_document(), sort_keys=True) if result.terminal_document() else None,
                    ),
                )
                self._connection.execute("COMMIT")
                return result
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def create_run(self, run_id: str, created_at: str) -> RunState:
        """Create or retrieve a run snapshot."""
        with self._lock:
            validate_identifier(run_id, "run_id")
            validate_timestamp(created_at, "created_at")
            state = RunState(run_id=run_id, updated_at=created_at)
            document = json.dumps(state.to_document(), sort_keys=True)
            self._connection.execute(
                "INSERT OR IGNORE INTO runs(run_id, revision, state_json) VALUES (?, ?, ?)",
                (run_id, state.revision, document),
            )
            return self.get_run(run_id)

    def get_run(self, run_id: str) -> RunState:
        """Load a run snapshot by ID."""
        with self._lock:
            row = self._connection.execute(
                "SELECT state_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            return RunState.from_document(json.loads(row["state_json"]))

    def apply(self, event: RunEvent) -> TransitionResult:
        """Apply one event in an immediate SQLite transaction."""
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                result = self._apply_locked(event)
                if event.event_type == "decision_recorded":
                    self._sync_checkpoint_decision_locked(event)
                elif event.event_type == "checkpoint_expired":
                    self._sync_checkpoint_state_locked(event.checkpoint_id, "expired")
                self._connection.execute("COMMIT")
                return result
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def get_checkpoint(self, checkpoint_id: str) -> dict | None:
        """Return a durable checkpoint projection by ID."""
        with self._lock:
            row = self._connection.execute(
                "SELECT document_json FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)
            ).fetchone()
        return json.loads(row["document_json"]) if row is not None else None

    def _sync_checkpoint_decision_locked(self, event: RunEvent) -> None:
        """Apply a decision to checkpoint and plan projections in one transaction."""
        row = self._connection.execute(
            "SELECT document_json FROM checkpoints WHERE checkpoint_id = ?", (event.checkpoint_id,)
        ).fetchone()
        if row is None:
            return
        checkpoint = json.loads(row["document_json"])
        checkpoint["state"] = "approved" if event.decision == "approved" else "rejected"
        checkpoint["decision"] = {
            "decision_id": event.decision_id,
            "reviewer": {
                "identity": {"kind": "person", "id": event.actor_id},
                "role": event.actor_role,
                "authorization_ref": event.authorization_ref,
            },
            "decided_at": event.occurred_at,
            "value": event.decision,
            **({"rationale": event.reason} if event.reason else {}),
        }
        self._connection.execute(
            "UPDATE checkpoints SET document_json = ? WHERE checkpoint_id = ?",
            (json.dumps(checkpoint, sort_keys=True), event.checkpoint_id),
        )
        if checkpoint.get("kind") == "plan_review":
            plan_id = checkpoint.get("reviewed_artifact", {}).get("artifact_id", "").removeprefix("artifact-")
            plan_row = self._connection.execute(
                "SELECT bundle_json FROM plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            if plan_row is not None:
                bundle = json.loads(plan_row["bundle_json"])
                bundle["manifest"]["status"] = "approved" if event.decision == "approved" else "rejected"
                if event.decision == "approved":
                    bundle["manifest"]["approved_by"] = event.actor_id
                bundle["checkpoint"] = checkpoint
                self._connection.execute(
                    "UPDATE plans SET bundle_json = ? WHERE plan_id = ?",
                    (json.dumps(bundle, sort_keys=True), plan_id),
                )

    def _sync_checkpoint_state_locked(self, checkpoint_id: str | None, state: str) -> None:
        """Keep persisted checkpoint and plan projections aligned with lifecycle transitions."""
        if checkpoint_id is None:
            return
        row = self._connection.execute(
            "SELECT document_json FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)
        ).fetchone()
        if row is None:
            return
        checkpoint = json.loads(row["document_json"])
        checkpoint["state"] = state
        self._connection.execute(
            "UPDATE checkpoints SET document_json = ? WHERE checkpoint_id = ?",
            (json.dumps(checkpoint, sort_keys=True), checkpoint_id),
        )
        plan_id = checkpoint.get("reviewed_artifact", {}).get("artifact_id", "").removeprefix("artifact-")
        plan_row = self._connection.execute("SELECT bundle_json FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
        if plan_row is not None:
            bundle = json.loads(plan_row["bundle_json"])
            bundle["checkpoint"] = checkpoint
            self._connection.execute(
                "UPDATE plans SET bundle_json = ? WHERE plan_id = ?",
                (json.dumps(bundle, sort_keys=True), plan_id),
            )

    def _apply_locked(self, event: RunEvent, *, allow_unrecorded_admission: bool = False) -> TransitionResult:
        """Apply an event while the caller owns the active SQLite transaction."""
        payload = json.dumps(asdict(event), sort_keys=True, separators=(",", ":"))
        digest = event_fingerprint(event)
        existing = self._connection.execute(
            "SELECT run_id, payload_digest FROM run_events WHERE event_id = ?",
            (event.event_id,),
        ).fetchone()
        if existing is not None:
            if existing["payload_digest"] != digest or existing["run_id"] != event.run_id:
                raise DuplicateEventError(f"event id {event.event_id!r} has a different payload")
            return TransitionResult(self.get_run(event.run_id), duplicate=True)

        state = self.get_run(event.run_id)
        if event.event_type == "admit" and not allow_unrecorded_admission:
            recorded = self._connection.execute(
                "SELECT 1 FROM admissions WHERE json_extract(result_json, '$.run_id') = ?",
                (event.run_id,),
            ).fetchone()
            if recorded is None:
                raise InvalidTransitionError("admission event requires a recorded admission result")
        if event.expected_revision != state.revision:
            raise StaleEventError(
                f"event expects revision {event.expected_revision}, current revision is {state.revision}"
            )
        result = state.apply(event)
        self._connection.execute(
            "INSERT INTO run_events(event_id, run_id, applied_revision, payload_digest, payload_json, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (event.event_id, event.run_id, result.state.revision, digest, payload, event.occurred_at),
        )
        self._connection.execute(
            "UPDATE runs SET revision = ?, state_json = ? WHERE run_id = ? AND revision = ?",
            (
                result.state.revision,
                json.dumps(result.state.to_document(), sort_keys=True),
                event.run_id,
                state.revision,
            ),
        )
        if self._connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise StaleEventError("run revision changed during update")
        return result

    def events(self, run_id: str) -> Iterator[RunEvent]:
        """Yield the append-only event journal for a run."""
        with self._lock:
            exists = self._connection.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if exists is None:
                raise KeyError(run_id)
            rows = list(self._connection.execute(
                "SELECT payload_json FROM run_events WHERE run_id = ? ORDER BY applied_revision",
                (run_id,),
            ))
        for row in rows:
            yield RunEvent(**json.loads(row["payload_json"]))

    def replay(self, run_id: str) -> RunState:
        """Replay and verify the event journal against the stored snapshot."""
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                event_rows = list(self._connection.execute(
                    "SELECT payload_json FROM run_events WHERE run_id = ? ORDER BY applied_revision",
                    (run_id,),
                ))
                snapshot_row = self._connection.execute(
                    "SELECT state_json FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if snapshot_row is None:
                    raise KeyError(run_id)
                snapshot = RunState.from_document(json.loads(snapshot_row["state_json"]))
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

        state = RunState.initial(run_id)
        for row in event_rows:
            event = RunEvent(**json.loads(row["payload_json"]))
            state = state.apply(event).state
        if state.revision != snapshot.revision:
            raise RuntimeError("run snapshot and event journal diverged")
        if state.revision and state.to_document() != snapshot.to_document():
            raise RuntimeError("run snapshot and event journal projection diverged")
        return snapshot


def _admission_from_document(document: dict) -> AdmissionResult:
    """Reconstruct an admission result from its persisted document."""
    return AdmissionResult(
        admission_id=document["admission_id"],
        request_id=document["request_id"],
        run_id=document["run_id"],
        evaluated_at=document["evaluated_at"],
        checks=tuple(CheckResult(
            name=check["name"],
            required_check=check["required_check"],
            status=check["status"],
            category=check.get("category", "self_verification"),
            detail=check.get("detail"),
            evidence_refs=tuple(check.get("evidence_refs", [])),
            capability=check.get("capability"),
            checked_at=check.get("checked_at"),
            component=check.get("component"),
            limitations=tuple(check.get("limitations", [])),
        ) for check in document["checks"]),
        status=document["status"],
        request_digest=document["request_digest"],
        platform_readiness_id=document.get("platform_readiness_id"),
        failure_register_refs=tuple(document.get("failure_register_refs", [])),
        reason=document.get("reason"),
        limitations=tuple(document.get("limitations", [])),
        identities=document.get("identities", {}),
        admitted_capabilities=tuple(document.get("admitted_capabilities", [])),
    )


def _validate_analysis_record(
    record: AnalysisRecord,
    start_event: RunEvent,
    outcome_event: RunEvent,
    checkpoint_event: RunEvent | None,
) -> None:
    """Reject bundles whose manifest, artifact, checkpoint, and events diverge."""
    manifest = record.manifest
    checkpoint = record.checkpoint
    run_id = manifest.get("run_id")
    if start_event.run_id != run_id or outcome_event.run_id != run_id:
        raise ValueError("analysis bundle and lifecycle event must belong to one run")
    if start_event.event_type != "analysis_started":
        raise ValueError("analysis persistence requires an analysis_started event")
    if checkpoint is None:
        expected = "analysis_blocked" if manifest.get("outcome") == "blocked" else "analysis_inconclusive"
        if checkpoint_event is not None or outcome_event.event_type != expected:
            raise ValueError("non-reviewable analysis requires its matching terminal event")
        return
    if checkpoint_event is None or checkpoint_event.run_id != run_id or checkpoint.get("run_id") != run_id:
        raise ValueError("analysis bundle and checkpoint events must belong to one run")
    if outcome_event.event_type != "analysis_completed":
        raise ValueError("reviewable analysis persistence requires an analysis_completed event")
    if checkpoint_event.event_type != "checkpoint_created" or checkpoint_event.checkpoint_kind != "analysis_review":
        raise ValueError("analysis persistence requires an analysis review checkpoint event")
    if checkpoint_event.checkpoint_id != checkpoint.get("checkpoint_id"):
        raise ValueError("checkpoint event and document identities differ")
    reviewed = checkpoint.get("reviewed_artifact")
    if not isinstance(reviewed, dict) or reviewed.get("artifact_id") not in manifest.get("artifact_refs", []):
        raise ValueError("analysis checkpoint must review a referenced analysis artifact")
    matching = [
        artifact.get("artifact")
        for artifact in record.artifacts
        if isinstance(artifact.get("artifact"), dict)
        and artifact["artifact"].get("artifact_id") == reviewed.get("artifact_id")
    ]
    if matching != [reviewed]:
        raise ValueError("analysis checkpoint artifact metadata differs from persisted output")
