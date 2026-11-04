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
from atg.domain.run_state import (
    DuplicateEventError,
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
                result_json TEXT NOT NULL
            );
            """
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def get_admission(self, idempotency_key: str) -> AdmissionResult | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT result_json FROM admissions WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        if row is None:
            return None
        return _admission_from_document(json.loads(row["result_json"]))

    def save_admission(self, key: str, request_digest: str, result: AdmissionResult) -> None:
        with self._lock:
            existing = self._connection.execute(
                "SELECT request_digest FROM admissions WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise ValueError("idempotency key is bound to a different request")
                return
            self._connection.execute(
                "INSERT INTO admissions(idempotency_key, request_digest, result_json) VALUES (?, ?, ?)",
                (key, request_digest, json.dumps(result.to_document(), sort_keys=True)),
            )

    def record_admission(self, key: str, request_digest: str, result: AdmissionResult, event: RunEvent) -> AdmissionResult:
        """Atomically persist the admission result and its run transition."""
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
                self._connection.execute(
                    "INSERT OR IGNORE INTO runs(run_id, revision, state_json) VALUES (?, ?, ?)",
                    (event.run_id, 0, json.dumps(RunState(run_id=event.run_id, updated_at=event.occurred_at).to_document(), sort_keys=True)),
                )
                self._apply_locked(event)
                self._connection.execute(
                    "INSERT INTO admissions(idempotency_key, request_digest, result_json) VALUES (?, ?, ?)",
                    (key, request_digest, json.dumps(result.to_document(), sort_keys=True)),
                )
                self._connection.execute("COMMIT")
                return result
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def create_run(self, run_id: str, created_at: str) -> RunState:
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
        with self._lock:
            row = self._connection.execute(
                "SELECT state_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            return RunState.from_document(json.loads(row["state_json"]))

    def apply(self, event: RunEvent) -> TransitionResult:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                result = self._apply_locked(event)
                self._connection.execute("COMMIT")
                return result
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def _apply_locked(self, event: RunEvent) -> TransitionResult:
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
        ) for check in document["checks"]),
        status=document["status"],
        request_digest=document["request_digest"],
        platform_readiness_id=document.get("platform_readiness_id"),
        failure_register_refs=tuple(document.get("failure_register_refs", [])),
        reason=document.get("reason"),
        limitations=tuple(document.get("limitations", [])),
        identities=document.get("identities", {}),
    )
