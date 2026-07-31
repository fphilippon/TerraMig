import unittest
from datetime import datetime, timedelta, timezone

from terramig.domain import JobStatus
from terramig.persistence import MemoryPersistence
from terramig.services.jobs import DurableWorker, JobOutcome, JobQueue


class DurableJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.persistence = MemoryPersistence()
        self.queue = JobQueue(self.persistence)

    def test_active_action_is_idempotently_enqueued_once(self) -> None:
        first = self.queue.enqueue("adoption", "generate", "workflow-1")
        second = self.queue.enqueue("adoption", "generate", "workflow-1")
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.queue.list()), 1)

    def test_worker_executes_and_persists_result(self) -> None:
        job = self.queue.enqueue("adoption", "generate", "workflow-1")
        worker = DurableWorker(
            self.persistence,
            {"adoption.generate": lambda claimed: {"workflow": claimed.subject_id}},
            worker_id="worker-1",
        )
        self.assertTrue(worker.execute_once())
        completed = self.queue.get(job.id)
        self.assertEqual(completed.status, JobStatus.SUCCEEDED)
        self.assertEqual(completed.result, {"workflow": "workflow-1"})
        self.assertEqual(completed.attempts, 1)
        self.assertTrue(completed.completed_at)

    def test_streamed_logs_survive_worker_result_completion(self) -> None:
        job = self.queue.enqueue("adoption", "generate", "workflow-1")

        def handler(claimed):
            self.queue.log(claimed.id, "bob stdout: composing")
            self.queue.log(claimed.id, "secret=should-not-be-visible")
            return {"workflow": claimed.subject_id}

        worker = DurableWorker(
            self.persistence,
            {"adoption.generate": handler},
            worker_id="worker-1",
        )
        worker.execute_once()
        completed = self.queue.get(job.id)
        self.assertEqual(completed.result["workflow"], "workflow-1")
        self.assertIn("bob stdout: composing", completed.result["logs"])
        self.assertNotIn("should-not-be-visible", " ".join(completed.result["logs"]))

    def test_expired_lease_is_reclaimed_after_worker_crash(self) -> None:
        job = self.queue.enqueue("adoption", "validate", "workflow-1")
        expired = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        first_claim = self.persistence.claim_job(
            "dead-worker", datetime.now(timezone.utc).isoformat(), expired
        )
        self.assertEqual(first_claim.id, job.id)
        recovered = self.persistence.claim_job(
            "new-worker",
            datetime.now(timezone.utc).isoformat(),
            (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
        )
        self.assertEqual(recovered.id, job.id)
        self.assertEqual(recovered.attempts, 2)
        self.assertEqual(recovered.lease_owner, "new-worker")

    def test_polling_continuation_releases_same_job_without_burning_attempt(self) -> None:
        calls = []

        def handler(_job):
            calls.append(True)
            if len(calls) == 1:
                return JobOutcome({"remote": "planning"}, retry_after_seconds=0.001)
            return JobOutcome({"remote": "applied"})

        job = self.queue.enqueue(
            "adoption", "reconcile", "workflow-1", max_attempts=1000
        )
        worker = DurableWorker(
            self.persistence,
            {"adoption.reconcile": handler},
            worker_id="worker-1",
            poll_seconds=0.001,
        )
        worker.execute_once()
        waiting = self.queue.get(job.id)
        self.assertEqual(waiting.status, JobStatus.QUEUED)
        self.assertEqual(waiting.attempts, 0)
        import time

        time.sleep(0.005)
        worker.execute_once()
        completed = self.queue.get(job.id)
        self.assertEqual(completed.status, JobStatus.SUCCEEDED)
        self.assertEqual(completed.result["remote"], "applied")

    def test_queued_job_can_be_cancelled_and_failed_job_retried(self) -> None:
        job = self.queue.enqueue("state", "prepare", "migration-1")
        cancelled = self.queue.cancel(job.id)
        self.assertEqual(cancelled.status, JobStatus.CANCELLED)
        retried = self.queue.retry(job.id)
        self.assertNotEqual(retried.id, job.id)
        self.assertEqual(retried.status, JobStatus.QUEUED)


if __name__ == "__main__":
    unittest.main()
