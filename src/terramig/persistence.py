from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .domain import (
    DurableJob,
    GenerationBundle,
    GitDelivery,
    HCPImportLifecycle,
    ImportOperation,
    JobStatus,
    ModuleCandidate,
    ModuleVersion,
    Resource,
    Stage,
    VerificationResult,
    Workflow,
)


@dataclass
class UserRecord:
    id: str
    username: str
    password_hash: str
    password_salt: str
    password_iterations: int
    must_change_password: bool
    disabled: bool = False
    failed_attempts: int = 0
    locked_until: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class SessionRecord:
    token_hash: str
    user_id: str
    csrf_hash: str
    created_at: str
    expires_at: str
    last_seen_at: str


@dataclass
class SecretRecord:
    name: str
    ciphertext: str
    created_at: str
    updated_at: str
    verified_at: str = ""


class Persistence(Protocol):
    kind: str

    def initialize(self) -> None: ...

    def healthy(self) -> bool: ...

    def load_settings(self) -> dict[str, Any] | None: ...

    def save_settings(self, payload: dict[str, Any]) -> None: ...

    def get_secret(self, name: str) -> SecretRecord | None: ...

    def save_secret(self, secret: SecretRecord) -> None: ...

    def delete_secret(self, name: str) -> None: ...

    def mark_secret_verified(self, name: str, verified_at: str) -> None: ...

    def list_workflows(self) -> list[Workflow]: ...

    def get_workflow(self, workflow_id: str) -> Workflow | None: ...

    def save_workflow(self, workflow: Workflow) -> None: ...

    def create_user_if_absent(self, user: UserRecord) -> UserRecord: ...

    def get_user_by_username(self, username: str) -> UserRecord | None: ...

    def get_user(self, user_id: str) -> UserRecord | None: ...

    def save_user(self, user: UserRecord) -> None: ...

    def save_session(self, session: SessionRecord) -> None: ...

    def get_session(self, token_hash: str) -> SessionRecord | None: ...

    def touch_session(self, token_hash: str, last_seen_at: str) -> None: ...

    def delete_session(self, token_hash: str) -> None: ...

    def delete_user_sessions(self, user_id: str) -> None: ...

    def delete_expired_sessions(self, now: str) -> None: ...

    def record_audit_event(self, event: dict[str, Any]) -> None: ...

    def list_audit_events(self, limit: int = 100) -> list[dict[str, Any]]: ...

    def create_job(self, job: DurableJob) -> DurableJob: ...

    def get_job(self, job_id: str) -> DurableJob | None: ...

    def list_jobs(self, limit: int = 100) -> list[DurableJob]: ...

    def find_active_job(self, subject_id: str, action: str) -> DurableJob | None: ...

    def claim_job(
        self, worker_id: str, now: str, lease_expires_at: str
    ) -> DurableJob | None: ...

    def heartbeat_job(
        self, job_id: str, worker_id: str, lease_expires_at: str
    ) -> bool: ...

    def request_job_cancel(self, job_id: str, now: str) -> DurableJob | None: ...

    def save_job(self, job: DurableJob) -> None: ...

    def append_job_log(
        self, job_id: str, message: str, updated_at: str, *, max_lines: int = 400
    ) -> DurableJob | None: ...


class MemoryPersistence:
    """Thread-safe test fallback. Production deployments should require PostgreSQL."""

    kind = "memory"

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.settings: dict[str, Any] | None = None
        self.secrets: dict[str, SecretRecord] = {}
        self.workflows: dict[str, dict[str, Any]] = {}
        self.users: dict[str, UserRecord] = {}
        self.sessions: dict[str, SessionRecord] = {}
        self.audit_events: list[dict[str, Any]] = []
        self.jobs: dict[str, DurableJob] = {}

    def initialize(self) -> None:
        return

    def healthy(self) -> bool:
        return True

    def load_settings(self) -> dict[str, Any] | None:
        with self.lock:
            return _copy(self.settings) if self.settings is not None else None

    def save_settings(self, payload: dict[str, Any]) -> None:
        with self.lock:
            self.settings = _copy(payload)

    def get_secret(self, name: str) -> SecretRecord | None:
        with self.lock:
            value = self.secrets.get(name)
            return _copy_record(value) if value else None

    def save_secret(self, secret: SecretRecord) -> None:
        with self.lock:
            existing = self.secrets.get(secret.name)
            if existing:
                secret.created_at = existing.created_at
            self.secrets[secret.name] = _copy_record(secret)

    def delete_secret(self, name: str) -> None:
        with self.lock:
            self.secrets.pop(name, None)

    def mark_secret_verified(self, name: str, verified_at: str) -> None:
        with self.lock:
            if name in self.secrets:
                self.secrets[name].verified_at = verified_at
                self.secrets[name].updated_at = verified_at

    def list_workflows(self) -> list[Workflow]:
        with self.lock:
            return [_workflow_from_dict(_copy(item)) for item in self.workflows.values()]

    def get_workflow(self, workflow_id: str) -> Workflow | None:
        with self.lock:
            value = self.workflows.get(workflow_id)
            return _workflow_from_dict(_copy(value)) if value else None

    def save_workflow(self, workflow: Workflow) -> None:
        with self.lock:
            self.workflows[workflow.id] = _copy(workflow.to_dict())

    def create_user_if_absent(self, user: UserRecord) -> UserRecord:
        with self.lock:
            existing = self.get_user_by_username(user.username)
            if existing:
                return existing
            self.users[user.id] = _copy_record(user)
            return _copy_record(user)

    def get_user_by_username(self, username: str) -> UserRecord | None:
        normalized = username.casefold()
        with self.lock:
            return next(
                (
                    _copy_record(user)
                    for user in self.users.values()
                    if user.username.casefold() == normalized
                ),
                None,
            )

    def get_user(self, user_id: str) -> UserRecord | None:
        with self.lock:
            user = self.users.get(user_id)
            return _copy_record(user) if user else None

    def save_user(self, user: UserRecord) -> None:
        with self.lock:
            self.users[user.id] = _copy_record(user)

    def save_session(self, session: SessionRecord) -> None:
        with self.lock:
            self.sessions[session.token_hash] = _copy_record(session)

    def get_session(self, token_hash: str) -> SessionRecord | None:
        with self.lock:
            session = self.sessions.get(token_hash)
            return _copy_record(session) if session else None

    def touch_session(self, token_hash: str, last_seen_at: str) -> None:
        with self.lock:
            if token_hash in self.sessions:
                self.sessions[token_hash].last_seen_at = last_seen_at

    def delete_session(self, token_hash: str) -> None:
        with self.lock:
            self.sessions.pop(token_hash, None)

    def delete_user_sessions(self, user_id: str) -> None:
        with self.lock:
            self.sessions = {
                token: session
                for token, session in self.sessions.items()
                if session.user_id != user_id
            }

    def delete_expired_sessions(self, now: str) -> None:
        with self.lock:
            self.sessions = {
                token: session
                for token, session in self.sessions.items()
                if session.expires_at > now
            }

    def record_audit_event(self, event: dict[str, Any]) -> None:
        with self.lock:
            self.audit_events.append(_copy(event))
            self.audit_events = self.audit_events[-1000:]

    def list_audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.lock:
            return [_copy(item) for item in reversed(self.audit_events[-limit:])]

    def create_job(self, job: DurableJob) -> DurableJob:
        with self.lock:
            active = self.find_active_job(job.subject_id, job.action)
            if active:
                return active
            self.jobs[job.id] = _job_from_dict(job.to_dict())
            return _job_from_dict(job.to_dict())

    def get_job(self, job_id: str) -> DurableJob | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return _job_from_dict(job.to_dict()) if job else None

    def list_jobs(self, limit: int = 100) -> list[DurableJob]:
        with self.lock:
            jobs = sorted(
                self.jobs.values(), key=lambda item: item.updated_at, reverse=True
            )
            return [_job_from_dict(item.to_dict()) for item in jobs[:limit]]

    def find_active_job(self, subject_id: str, action: str) -> DurableJob | None:
        with self.lock:
            for job in self.jobs.values():
                if (
                    job.subject_id == subject_id
                    and job.action == action
                    and job.status in {JobStatus.QUEUED, JobStatus.RUNNING}
                ):
                    return _job_from_dict(job.to_dict())
            return None

    def claim_job(
        self, worker_id: str, now: str, lease_expires_at: str
    ) -> DurableJob | None:
        with self.lock:
            candidates = sorted(self.jobs.values(), key=lambda item: item.created_at)
            for job in candidates:
                another_active = any(
                    other.id != job.id
                    and other.subject_id == job.subject_id
                    and other.status == JobStatus.RUNNING
                    and other.lease_expires_at > now
                    for other in self.jobs.values()
                )
                if another_active:
                    continue
                reclaimable = (
                    job.status == JobStatus.RUNNING
                    and job.lease_expires_at
                    and job.lease_expires_at <= now
                )
                ready = job.status == JobStatus.QUEUED and job.available_at <= now
                if not (ready or reclaimable) or job.cancel_requested:
                    continue
                if job.attempts >= job.max_attempts:
                    job.status = JobStatus.FAILED
                    job.last_error = "maximum durable job attempts exceeded"
                    job.completed_at = now
                    job.updated_at = now
                    continue
                job.status = JobStatus.RUNNING
                job.attempts += 1
                job.lease_owner = worker_id
                job.lease_expires_at = lease_expires_at
                job.started_at = job.started_at or now
                job.updated_at = now
                return _job_from_dict(job.to_dict())
            return None

    def heartbeat_job(
        self, job_id: str, worker_id: str, lease_expires_at: str
    ) -> bool:
        with self.lock:
            job = self.jobs.get(job_id)
            if (
                not job
                or job.status != JobStatus.RUNNING
                or job.lease_owner != worker_id
            ):
                return False
            job.lease_expires_at = lease_expires_at
            return not job.cancel_requested

    def request_job_cancel(self, job_id: str, now: str) -> DurableJob | None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return None
            if job.status == JobStatus.QUEUED:
                job.status = JobStatus.CANCELLED
                job.completed_at = now
            elif job.status == JobStatus.RUNNING:
                job.cancel_requested = True
            job.updated_at = now
            return _job_from_dict(job.to_dict())

    def save_job(self, job: DurableJob) -> None:
        with self.lock:
            self.jobs[job.id] = _job_from_dict(job.to_dict())

    def append_job_log(
        self, job_id: str, message: str, updated_at: str, *, max_lines: int = 400
    ) -> DurableJob | None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return None
            logs = list(job.result.get("logs", []))
            logs.append(str(message))
            job.result["logs"] = logs[-max(1, max_lines) :]
            job.updated_at = updated_at
            return _job_from_dict(job.to_dict())


SCHEMA_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS terramig_settings (
          singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
          payload jsonb NOT NULL,
          updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS terramig_workflows (
          id uuid PRIMARY KEY,
          stage text NOT NULL,
          payload jsonb NOT NULL,
          created_at timestamptz NOT NULL,
          updated_at timestamptz NOT NULL
        );
        CREATE INDEX IF NOT EXISTS terramig_workflows_updated_idx
          ON terramig_workflows (updated_at DESC);
        CREATE TABLE IF NOT EXISTS terramig_users (
          id uuid PRIMARY KEY,
          username text NOT NULL,
          password_hash text NOT NULL,
          password_salt text NOT NULL,
          password_iterations integer NOT NULL,
          must_change_password boolean NOT NULL DEFAULT true,
          disabled boolean NOT NULL DEFAULT false,
          failed_attempts integer NOT NULL DEFAULT 0,
          locked_until timestamptz,
          created_at timestamptz NOT NULL,
          updated_at timestamptz NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS terramig_users_username_idx
          ON terramig_users (lower(username));
        CREATE TABLE IF NOT EXISTS terramig_sessions (
          token_hash char(64) PRIMARY KEY,
          user_id uuid NOT NULL REFERENCES terramig_users(id) ON DELETE CASCADE,
          csrf_hash char(64) NOT NULL,
          created_at timestamptz NOT NULL,
          expires_at timestamptz NOT NULL,
          last_seen_at timestamptz NOT NULL
        );
        CREATE INDEX IF NOT EXISTS terramig_sessions_expires_idx
          ON terramig_sessions (expires_at);
        CREATE TABLE IF NOT EXISTS terramig_audit_events (
          id bigserial PRIMARY KEY,
          payload jsonb NOT NULL,
          occurred_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS terramig_audit_events_time_idx
          ON terramig_audit_events (occurred_at DESC);
        """,
    ),
    (
        2,
        """
        CREATE TABLE IF NOT EXISTS terramig_jobs (
          id uuid PRIMARY KEY,
          kind text NOT NULL,
          action text NOT NULL,
          subject_id text NOT NULL,
          status text NOT NULL,
          payload jsonb NOT NULL DEFAULT '{}'::jsonb,
          result jsonb NOT NULL DEFAULT '{}'::jsonb,
          attempts integer NOT NULL DEFAULT 0,
          max_attempts integer NOT NULL DEFAULT 3,
          available_at timestamptz NOT NULL,
          lease_owner text,
          lease_expires_at timestamptz,
          cancel_requested boolean NOT NULL DEFAULT false,
          last_error text NOT NULL DEFAULT '',
          created_at timestamptz NOT NULL,
          updated_at timestamptz NOT NULL,
          started_at timestamptz,
          completed_at timestamptz
        );
        CREATE INDEX IF NOT EXISTS terramig_jobs_claim_idx
          ON terramig_jobs (status, available_at, created_at);
        CREATE INDEX IF NOT EXISTS terramig_jobs_subject_idx
          ON terramig_jobs (subject_id, action, updated_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS terramig_jobs_one_active_idx
          ON terramig_jobs (subject_id, action)
          WHERE status IN ('queued', 'running');
        """,
    ),
    (
        3,
        """
        CREATE TABLE IF NOT EXISTS terramig_secrets (
          name text PRIMARY KEY,
          ciphertext text NOT NULL,
          created_at timestamptz NOT NULL,
          updated_at timestamptz NOT NULL,
          verified_at timestamptz
        );
        """,
    ),
)


class PostgresPersistence:
    kind = "postgresql"

    def __init__(
        self,
        database_url: str = "",
        connection_parameters: dict[str, str] | None = None,
    ) -> None:
        if not database_url and not connection_parameters:
            raise ValueError("PostgreSQL connection configuration is required")
        self.database_url = database_url
        self.connection_parameters = connection_parameters or {}

    def _connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as error:
            raise RuntimeError(
                "PostgreSQL persistence requires psycopg[binary]"
            ) from error
        return psycopg.connect(
            self.database_url,
            **self.connection_parameters,
            row_factory=dict_row,
        )

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(847301925)")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS terramig_schema_migrations (
                  version integer PRIMARY KEY,
                  applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            applied = {
                row["version"]
                for row in connection.execute(
                    "SELECT version FROM terramig_schema_migrations"
                ).fetchall()
            }
            for version, statements in SCHEMA_MIGRATIONS:
                if version in applied:
                    continue
                connection.execute(statements)
                connection.execute(
                    "INSERT INTO terramig_schema_migrations (version) VALUES (%s)",
                    (version,),
                )

    def healthy(self) -> bool:
        try:
            with self._connect() as connection:
                return connection.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
        except Exception:
            return False

    @staticmethod
    def _json(payload: dict[str, Any]):
        from psycopg.types.json import Jsonb

        return Jsonb(payload)

    def load_settings(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM terramig_settings WHERE singleton = true"
            ).fetchone()
            return row["payload"] if row else None

    def save_settings(self, payload: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO terramig_settings (singleton, payload)
                VALUES (true, %s)
                ON CONFLICT (singleton) DO UPDATE
                  SET payload = EXCLUDED.payload, updated_at = now()
                """,
                (self._json(payload),),
            )

    def get_secret(self, name: str) -> SecretRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM terramig_secrets WHERE name = %s", (name,)
            ).fetchone()
            return _secret_from_row(row) if row else None

    def save_secret(self, secret: SecretRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO terramig_secrets (
                  name, ciphertext, created_at, updated_at, verified_at
                ) VALUES (%s, %s, %s, %s, NULL)
                ON CONFLICT (name) DO UPDATE SET
                  ciphertext = EXCLUDED.ciphertext,
                  updated_at = EXCLUDED.updated_at,
                  verified_at = NULL
                """,
                (
                    secret.name,
                    secret.ciphertext,
                    secret.created_at,
                    secret.updated_at,
                ),
            )

    def delete_secret(self, name: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM terramig_secrets WHERE name = %s", (name,)
            )

    def mark_secret_verified(self, name: str, verified_at: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE terramig_secrets
                SET verified_at = %s, updated_at = %s
                WHERE name = %s
                """,
                (verified_at, verified_at, name),
            )

    def list_workflows(self) -> list[Workflow]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM terramig_workflows ORDER BY updated_at DESC"
            ).fetchall()
            return [_workflow_from_dict(row["payload"]) for row in rows]

    def get_workflow(self, workflow_id: str) -> Workflow | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM terramig_workflows WHERE id = %s",
                (workflow_id,),
            ).fetchone()
            return _workflow_from_dict(row["payload"]) if row else None

    def save_workflow(self, workflow: Workflow) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO terramig_workflows
                  (id, stage, payload, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                  stage = EXCLUDED.stage,
                  payload = EXCLUDED.payload,
                  updated_at = EXCLUDED.updated_at
                """,
                (
                    workflow.id,
                    workflow.stage.value,
                    self._json(workflow.to_dict()),
                    workflow.created_at,
                    workflow.updated_at,
                ),
            )

    def create_user_if_absent(self, user: UserRecord) -> UserRecord:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO terramig_users (
                  id, username, password_hash, password_salt, password_iterations,
                  must_change_password, disabled, failed_attempts, locked_until,
                  created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    user.id,
                    user.username,
                    user.password_hash,
                    user.password_salt,
                    user.password_iterations,
                    user.must_change_password,
                    user.disabled,
                    user.failed_attempts,
                    user.created_at,
                    user.updated_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM terramig_users WHERE lower(username) = lower(%s)",
                (user.username,),
            ).fetchone()
            return _user_from_row(row)

    def get_user_by_username(self, username: str) -> UserRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM terramig_users WHERE lower(username) = lower(%s)",
                (username,),
            ).fetchone()
            return _user_from_row(row) if row else None

    def get_user(self, user_id: str) -> UserRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM terramig_users WHERE id = %s", (user_id,)
            ).fetchone()
            return _user_from_row(row) if row else None

    def save_user(self, user: UserRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE terramig_users SET
                  password_hash = %s, password_salt = %s, password_iterations = %s,
                  must_change_password = %s, disabled = %s, failed_attempts = %s,
                  locked_until = %s, updated_at = %s
                WHERE id = %s
                """,
                (
                    user.password_hash,
                    user.password_salt,
                    user.password_iterations,
                    user.must_change_password,
                    user.disabled,
                    user.failed_attempts,
                    user.locked_until or None,
                    user.updated_at,
                    user.id,
                ),
            )

    def save_session(self, session: SessionRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO terramig_sessions (
                  token_hash, user_id, csrf_hash, created_at, expires_at, last_seen_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    session.token_hash,
                    session.user_id,
                    session.csrf_hash,
                    session.created_at,
                    session.expires_at,
                    session.last_seen_at,
                ),
            )

    def get_session(self, token_hash: str) -> SessionRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM terramig_sessions WHERE token_hash = %s",
                (token_hash,),
            ).fetchone()
            return _session_from_row(row) if row else None

    def touch_session(self, token_hash: str, last_seen_at: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE terramig_sessions SET last_seen_at = %s WHERE token_hash = %s",
                (last_seen_at, token_hash),
            )

    def delete_session(self, token_hash: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM terramig_sessions WHERE token_hash = %s", (token_hash,)
            )

    def delete_user_sessions(self, user_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM terramig_sessions WHERE user_id = %s", (user_id,)
            )

    def delete_expired_sessions(self, now: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM terramig_sessions WHERE expires_at <= %s", (now,)
            )

    def record_audit_event(self, event: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO terramig_audit_events (payload) VALUES (%s)",
                (self._json(event),),
            )

    def list_audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM terramig_audit_events
                ORDER BY occurred_at DESC, id DESC LIMIT %s
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
            return [row["payload"] for row in rows]

    def create_job(self, job: DurableJob) -> DurableJob:
        with self._connect() as connection:
            lock_key = f"{job.subject_id}:{job.action}"
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,)
            )
            row = connection.execute(
                """
                SELECT * FROM terramig_jobs
                WHERE subject_id = %s AND action = %s
                  AND status IN ('queued', 'running')
                ORDER BY created_at DESC LIMIT 1
                """,
                (job.subject_id, job.action),
            ).fetchone()
            if row:
                return _job_from_row(row)
            connection.execute(
                """
                INSERT INTO terramig_jobs (
                  id, kind, action, subject_id, status, payload, result,
                  attempts, max_attempts, available_at, lease_owner,
                  lease_expires_at, cancel_requested, last_error,
                  created_at, updated_at, started_at, completed_at
                ) VALUES (
                  %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, NULL, NULL, false, '',
                  %s, %s, NULL, NULL
                )
                """,
                (
                    job.id,
                    job.kind,
                    job.action,
                    job.subject_id,
                    job.status.value,
                    self._json(job.payload),
                    self._json(job.result),
                    job.attempts,
                    job.max_attempts,
                    job.available_at,
                    job.created_at,
                    job.updated_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM terramig_jobs WHERE id = %s", (job.id,)
            ).fetchone()
            return _job_from_row(row)

    def get_job(self, job_id: str) -> DurableJob | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM terramig_jobs WHERE id = %s", (job_id,)
            ).fetchone()
            return _job_from_row(row) if row else None

    def list_jobs(self, limit: int = 100) -> list[DurableJob]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM terramig_jobs
                ORDER BY updated_at DESC, created_at DESC LIMIT %s
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
            return [_job_from_row(row) for row in rows]

    def find_active_job(self, subject_id: str, action: str) -> DurableJob | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM terramig_jobs
                WHERE subject_id = %s AND action = %s
                  AND status IN ('queued', 'running')
                ORDER BY created_at DESC LIMIT 1
                """,
                (subject_id, action),
            ).fetchone()
            return _job_from_row(row) if row else None

    def claim_job(
        self, worker_id: str, now: str, lease_expires_at: str
    ) -> DurableJob | None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE terramig_jobs
                SET status = 'cancelled', completed_at = %s, updated_at = %s,
                    lease_owner = NULL, lease_expires_at = NULL
                WHERE cancel_requested = true
                  AND status IN ('queued', 'running')
                  AND (status = 'queued' OR lease_expires_at <= %s)
                """,
                (now, now, now),
            )
            connection.execute(
                """
                UPDATE terramig_jobs
                SET status = 'failed',
                    last_error = 'maximum durable job attempts exceeded',
                    completed_at = %s, updated_at = %s,
                    lease_owner = NULL, lease_expires_at = NULL
                WHERE attempts >= max_attempts
                  AND (
                    (status = 'queued' AND available_at <= %s)
                    OR (status = 'running' AND lease_expires_at <= %s)
                  )
                """,
                (now, now, now, now),
            )
            row = connection.execute(
                """
                WITH candidate AS (
                  SELECT id FROM terramig_jobs
                  WHERE cancel_requested = false
                    AND attempts < max_attempts
                    AND NOT EXISTS (
                      SELECT 1 FROM terramig_jobs AS active
                      WHERE active.id <> terramig_jobs.id
                        AND active.subject_id = terramig_jobs.subject_id
                        AND active.status = 'running'
                        AND active.lease_expires_at > %s
                    )
                    AND (
                      (status = 'queued' AND available_at <= %s)
                      OR (status = 'running' AND lease_expires_at <= %s)
                    )
                  ORDER BY available_at, created_at
                  FOR UPDATE SKIP LOCKED
                  LIMIT 1
                )
                UPDATE terramig_jobs AS job SET
                  status = 'running',
                  attempts = job.attempts + 1,
                  lease_owner = %s,
                  lease_expires_at = %s,
                  started_at = COALESCE(job.started_at, %s),
                  updated_at = %s
                FROM candidate
                WHERE job.id = candidate.id
                RETURNING job.*
                """,
                (now, now, now, worker_id, lease_expires_at, now, now),
            ).fetchone()
            return _job_from_row(row) if row else None

    def heartbeat_job(
        self, job_id: str, worker_id: str, lease_expires_at: str
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                UPDATE terramig_jobs SET lease_expires_at = %s, updated_at = now()
                WHERE id = %s AND status = 'running' AND lease_owner = %s
                RETURNING cancel_requested
                """,
                (lease_expires_at, job_id, worker_id),
            ).fetchone()
            return bool(row is not None and not row["cancel_requested"])

    def request_job_cancel(self, job_id: str, now: str) -> DurableJob | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                UPDATE terramig_jobs SET
                  cancel_requested = CASE
                    WHEN status = 'running' THEN true
                    ELSE cancel_requested
                  END,
                  status = CASE
                    WHEN status = 'queued' THEN 'cancelled'
                    ELSE status
                  END,
                  completed_at = CASE
                    WHEN status = 'queued' THEN %s
                    ELSE completed_at
                  END,
                  updated_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (now, now, job_id),
            ).fetchone()
            return _job_from_row(row) if row else None

    def save_job(self, job: DurableJob) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE terramig_jobs SET
                  status = %s, payload = %s, result = %s,
                  attempts = %s, max_attempts = %s, available_at = %s,
                  lease_owner = %s, lease_expires_at = %s,
                  cancel_requested = %s, last_error = %s, updated_at = %s,
                  started_at = %s, completed_at = %s
                WHERE id = %s
                """,
                (
                    job.status.value,
                    self._json(job.payload),
                    self._json(job.result),
                    job.attempts,
                    job.max_attempts,
                    job.available_at,
                    job.lease_owner or None,
                    job.lease_expires_at or None,
                    job.cancel_requested,
                    job.last_error,
                    job.updated_at,
                    job.started_at or None,
                    job.completed_at or None,
                    job.id,
                ),
            )

    def append_job_log(
        self, job_id: str, message: str, updated_at: str, *, max_lines: int = 400
    ) -> DurableJob | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM terramig_jobs WHERE id = %s FOR UPDATE", (job_id,)
            ).fetchone()
            if not row:
                return None
            result = dict(row["result"] or {})
            logs = list(result.get("logs", []))
            logs.append(str(message))
            result["logs"] = logs[-max(1, max_lines) :]
            connection.execute(
                "UPDATE terramig_jobs SET result = %s, updated_at = %s WHERE id = %s",
                (self._json(result), updated_at, job_id),
            )
            row = connection.execute(
                "SELECT * FROM terramig_jobs WHERE id = %s", (job_id,)
            ).fetchone()
            return _job_from_row(row)


def build_persistence() -> Persistence:
    database_url = os.getenv("TERRAMIG_DATABASE_URL", "").strip()
    database_host = os.getenv("TERRAMIG_DATABASE_HOST", "").strip()
    require_postgres = os.getenv("TERRAMIG_REQUIRE_POSTGRES", "").lower() in {
        "1",
        "true",
        "yes",
    }
    if database_url:
        persistence: Persistence = PostgresPersistence(database_url)
    elif database_host:
        connection_parameters = {
            "host": database_host,
            "port": os.getenv("TERRAMIG_DATABASE_PORT", "5432").strip(),
            "dbname": os.getenv("TERRAMIG_DATABASE_NAME", "terramig").strip(),
            "user": os.getenv("TERRAMIG_DATABASE_USER", "terramig").strip(),
            "password": os.getenv("TERRAMIG_DATABASE_PASSWORD", ""),
        }
        persistence = PostgresPersistence(
            connection_parameters=connection_parameters
        )
    elif require_postgres:
        raise RuntimeError(
            "TERRAMIG_DATABASE_URL or TERRAMIG_DATABASE_HOST is required "
            "when TERRAMIG_REQUIRE_POSTGRES=true"
        )
    else:
        persistence = MemoryPersistence()
    persistence.initialize()
    return persistence


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _copy_record(value):
    return type(value)(**value.__dict__)


def _module_from_dict(value: dict[str, Any]) -> ModuleVersion:
    return ModuleVersion(
        id=value["id"],
        source=value["source"],
        version=value["version"],
        resource_types=tuple(value.get("resource_types", [])),
        inputs=tuple(value.get("inputs", [])),
        outputs=tuple(value.get("outputs", [])),
        managed_resource_address=value.get("managed_resource_address", ""),
        description=value.get("description", ""),
        schema_status=value.get("schema_status", "verified"),
        registry_kind=value.get("registry_kind", "private"),
        verified_publisher=bool(value.get("verified_publisher", False)),
        managed_resource_addresses=tuple(
            value.get("managed_resource_addresses", [])
        ),
    )


def _workflow_from_dict(value: dict[str, Any]) -> Workflow:
    bundle_value = value.get("bundle")
    bundle = None
    if bundle_value:
        bundle = GenerationBundle(
            terraform=bundle_value["terraform"],
            imports=[
                ImportOperation(**item) for item in bundle_value.get("imports", [])
            ],
            decisions=bundle_value.get("decisions", []),
            warnings=bundle_value.get("warnings", []),
            agent=bundle_value.get("agent", ""),
            duration_ms=bundle_value.get("duration_ms", 0),
            composition_log=bundle_value.get("composition_log", []),
            prompt_sha256=bundle_value.get("prompt_sha256", ""),
            response_sha256=bundle_value.get("response_sha256", ""),
            assurance_checks=bundle_value.get("assurance_checks", []),
            redacted_fields=bundle_value.get("redacted_fields", 0),
            backend_tf=bundle_value.get("backend_tf", ""),
            providers_tf=bundle_value.get("providers_tf", ""),
        )
    verification_value = value.get("verification")
    verification = (
        VerificationResult(**verification_value) if verification_value else None
    )
    resources = [
        Resource(
            **{
                **item,
                "dependency_ids": tuple(item.get("dependency_ids", [])),
            }
        )
        for item in value.get("resources", [])
    ]
    candidates = {
        resource_id: [
            ModuleCandidate(
                resource_id=item["resource_id"],
                module=_module_from_dict(item["module"]),
                score=item["score"],
                reasons=tuple(item.get("reasons", [])),
            )
            for item in items
        ]
        for resource_id, items in value.get("candidates", {}).items()
    }
    return Workflow(
        id=value["id"],
        project_id=value["project_id"],
        runtime_origin=value.get("runtime_origin", "legacy"),
        stage=Stage(value.get("stage", Stage.CREATED.value)),
        resources=resources,
        ordered_resource_ids=value.get("ordered_resource_ids", []),
        selected_resource_ids=value.get(
            "selected_resource_ids", value.get("ordered_resource_ids", [])
        ),
        managed_resources=value.get("managed_resources", {}),
        candidates=candidates,
        module_selections=value.get("module_selections", {}),
        public_registry_searched=bool(value.get("public_registry_searched", False)),
        bundle=bundle,
        verification=verification,
        drift_accepted=bool(value.get("drift_accepted", False)),
        drift_accepted_at=value.get("drift_accepted_at", ""),
        import_run_id=value.get("import_run_id"),
        target_protection=value.get("target_protection", {}),
        hcp_import=HCPImportLifecycle(**value.get("hcp_import", {})),
        git_delivery=(
            GitDelivery(**value["git_delivery"])
            if value.get("git_delivery")
            else None
        ),
        events=value.get("events", []),
        created_at=value.get("created_at", ""),
        updated_at=value.get("updated_at", ""),
    )


def _user_from_row(row: dict[str, Any]) -> UserRecord:
    return UserRecord(
        id=str(row["id"]),
        username=row["username"],
        password_hash=row["password_hash"],
        password_salt=row["password_salt"],
        password_iterations=row["password_iterations"],
        must_change_password=row["must_change_password"],
        disabled=row["disabled"],
        failed_attempts=row["failed_attempts"],
        locked_until=_iso(row.get("locked_until")),
        created_at=_iso(row["created_at"]),
        updated_at=_iso(row["updated_at"]),
    )


def _session_from_row(row: dict[str, Any]) -> SessionRecord:
    return SessionRecord(
        token_hash=row["token_hash"].strip(),
        user_id=str(row["user_id"]),
        csrf_hash=row["csrf_hash"].strip(),
        created_at=_iso(row["created_at"]),
        expires_at=_iso(row["expires_at"]),
        last_seen_at=_iso(row["last_seen_at"]),
    )


def _secret_from_row(row: dict[str, Any]) -> SecretRecord:
    return SecretRecord(
        name=row["name"],
        ciphertext=row["ciphertext"],
        created_at=_iso(row["created_at"]),
        updated_at=_iso(row["updated_at"]),
        verified_at=_iso(row.get("verified_at")),
    )


def _job_from_dict(value: dict[str, Any]) -> DurableJob:
    return DurableJob(
        id=value["id"],
        kind=value["kind"],
        action=value["action"],
        subject_id=value["subject_id"],
        payload=value.get("payload", {}),
        status=JobStatus(value.get("status", JobStatus.QUEUED.value)),
        attempts=int(value.get("attempts", 0)),
        max_attempts=int(value.get("max_attempts", 3)),
        available_at=value.get("available_at", ""),
        lease_owner=value.get("lease_owner", ""),
        lease_expires_at=value.get("lease_expires_at", ""),
        cancel_requested=bool(value.get("cancel_requested", False)),
        last_error=value.get("last_error", ""),
        result=value.get("result", {}),
        created_at=value.get("created_at", ""),
        updated_at=value.get("updated_at", ""),
        started_at=value.get("started_at", ""),
        completed_at=value.get("completed_at", ""),
    )


def _job_from_row(row: dict[str, Any]) -> DurableJob:
    return DurableJob(
        id=str(row["id"]),
        kind=row["kind"],
        action=row["action"],
        subject_id=row["subject_id"],
        payload=row.get("payload") or {},
        status=JobStatus(row["status"]),
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        available_at=_iso(row["available_at"]),
        lease_owner=row.get("lease_owner") or "",
        lease_expires_at=_iso(row.get("lease_expires_at")),
        cancel_requested=row["cancel_requested"],
        last_error=row.get("last_error") or "",
        result=row.get("result") or {},
        created_at=_iso(row["created_at"]),
        updated_at=_iso(row["updated_at"]),
        started_at=_iso(row.get("started_at")),
        completed_at=_iso(row.get("completed_at")),
    )


def _iso(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)
