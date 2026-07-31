from __future__ import annotations

import os
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import uuid4

from ..domain import DurableJob, JobStatus
from ..persistence import Persistence
from .assurance import sanitize_untrusted


@dataclass
class JobOutcome:
    result: dict = field(default_factory=dict)
    retry_after_seconds: float = 0


class NonRetryableJobError(RuntimeError):
    pass


class JobQueue:
    def __init__(self, persistence: Persistence) -> None:
        self.persistence = persistence

    def enqueue(
        self,
        kind: str,
        action: str,
        subject_id: str,
        payload: dict | None = None,
        *,
        max_attempts: int = 3,
        delay_seconds: float = 0,
    ) -> DurableJob:
        now = _now()
        job = DurableJob(
            id=str(uuid4()),
            kind=kind,
            action=action,
            subject_id=subject_id,
            payload=payload or {},
            max_attempts=max_attempts,
            available_at=_after(delay_seconds),
            created_at=now,
            updated_at=now,
        )
        return self.persistence.create_job(job)

    def get(self, job_id: str) -> DurableJob:
        job = self.persistence.get_job(job_id)
        if not job:
            raise KeyError(f"unknown job {job_id}")
        return job

    def list(self, limit: int = 100) -> list[DurableJob]:
        return self.persistence.list_jobs(limit)

    def cancel(self, job_id: str) -> DurableJob:
        job = self.persistence.request_job_cancel(job_id, _now())
        if not job:
            raise KeyError(f"unknown job {job_id}")
        return job

    def retry(self, job_id: str) -> DurableJob:
        failed = self.get(job_id)
        if failed.status not in {JobStatus.FAILED, JobStatus.CANCELLED}:
            raise ValueError("only failed or cancelled jobs can be retried")
        return self.enqueue(
            failed.kind,
            failed.action,
            failed.subject_id,
            failed.payload,
            max_attempts=failed.max_attempts,
        )

    def log(self, job_id: str, message: str) -> None:
        safe, _ = sanitize_untrusted(str(message), max_string=1200)
        safe = re.sub(
            r"(?i)\b(secret|token|api[_-]?key|password|private[_-]?key)\s*[:=]\s*\S+",
            r"\1=[REDACTED]",
            safe,
        )
        self.persistence.append_job_log(job_id, safe, _now())


class DurableWorker:
    """At-least-once worker with expiring leases and crash recovery."""

    def __init__(
        self,
        persistence: Persistence,
        handlers: dict[str, Callable[[DurableJob], JobOutcome | dict | None]],
        *,
        worker_id: str = "",
        lease_seconds: int = 90,
        poll_seconds: float = 0.5,
    ) -> None:
        self.persistence = persistence
        self.handlers = handlers
        self.worker_id = worker_id or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
        )
        self.lease_seconds = max(15, lease_seconds)
        self.poll_seconds = max(0.05, poll_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self.run_forever,
            name=f"terramig-worker-{self.worker_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def run_forever(self) -> None:
        while not self._stop.is_set():
            if not self.execute_once():
                self._stop.wait(self.poll_seconds)

    def execute_once(self) -> bool:
        job = self.persistence.claim_job(
            self.worker_id, _now(), _after(self.lease_seconds)
        )
        if not job:
            return False
        handler = self.handlers.get(f"{job.kind}.{job.action}")
        if not handler:
            self._finish_failed(job, f"no handler for {job.kind}.{job.action}")
            return True

        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat,
            args=(job.id, heartbeat_stop),
            name=f"terramig-heartbeat-{job.id}",
            daemon=True,
        )
        heartbeat.start()
        try:
            raw_outcome = handler(job)
            outcome = (
                raw_outcome
                if isinstance(raw_outcome, JobOutcome)
                else JobOutcome(result=raw_outcome or {})
            )
            current = self.persistence.get_job(job.id) or job
            if current.cancel_requested:
                current.status = JobStatus.CANCELLED
                current.completed_at = _now()
                current.updated_at = current.completed_at
                current.lease_owner = ""
                current.lease_expires_at = ""
                self.persistence.save_job(current)
            elif outcome.retry_after_seconds > 0:
                current.status = JobStatus.QUEUED
                current.available_at = _after(outcome.retry_after_seconds)
                current.result = {**current.result, **outcome.result}
                current.updated_at = _now()
                current.lease_owner = ""
                current.lease_expires_at = ""
                # Reconciliation polling is a continuation, not a failed attempt.
                current.attempts = max(0, current.attempts - 1)
                self.persistence.save_job(current)
            else:
                current.status = JobStatus.SUCCEEDED
                current.result = {**current.result, **outcome.result}
                current.completed_at = _now()
                current.updated_at = current.completed_at
                current.lease_owner = ""
                current.lease_expires_at = ""
                current.last_error = ""
                self.persistence.save_job(current)
        except NonRetryableJobError as error:
            current = self.persistence.get_job(job.id) or job
            self._finish_failed(current, _safe_error(error))
        except Exception as error:
            current = self.persistence.get_job(job.id) or job
            if current.cancel_requested:
                current.status = JobStatus.CANCELLED
                current.completed_at = _now()
                current.updated_at = current.completed_at
                current.lease_owner = ""
                current.lease_expires_at = ""
                self.persistence.save_job(current)
            elif current.attempts < current.max_attempts:
                current.status = JobStatus.QUEUED
                current.available_at = _after(min(30, 2 ** current.attempts))
                current.last_error = _safe_error(error)
                current.updated_at = _now()
                current.lease_owner = ""
                current.lease_expires_at = ""
                self.persistence.save_job(current)
            else:
                self._finish_failed(current, _safe_error(error))
        finally:
            heartbeat_stop.set()
            heartbeat.join(1)
        return True

    def _heartbeat(self, job_id: str, stop: threading.Event) -> None:
        interval = max(5, self.lease_seconds / 3)
        while not stop.wait(interval):
            keep_running = self.persistence.heartbeat_job(
                job_id, self.worker_id, _after(self.lease_seconds)
            )
            if not keep_running:
                return

    def _finish_failed(self, job: DurableJob, message: str) -> None:
        job.status = JobStatus.FAILED
        job.last_error = message
        job.completed_at = _now()
        job.updated_at = job.completed_at
        job.lease_owner = ""
        job.lease_expires_at = ""
        self.persistence.save_job(job)


def _safe_error(error: Exception) -> str:
    message = str(error).strip() or type(error).__name__
    return message[-4000:]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _after(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
