"""The database job queue: claiming, retrying, giving up, recovering.

    python3 manage.py test ConvAI.test_jobs --settings=test_settings
"""
from datetime import timedelta
from unittest import mock

from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from ConvAI.jobs import PermanentError, enqueue, latest_for, registry, retry, worker_seen
from ConvAI.jobs import queue
from ConvAI.models import Job, JobWorker

CALLS = []


@registry.handler("test.ok", max_attempts=3, backoff_s=10)
def _ok(payload):
    CALLS.append(("ok", payload))
    return {"echo": payload.get("n")}


@registry.handler("test.flaky", max_attempts=2, backoff_s=10)
def _flaky(payload):
    CALLS.append(("flaky", payload))
    raise RuntimeError("boom")


@registry.handler("test.permanent", max_attempts=5)
def _permanent(payload):
    raise PermanentError("the file is gone")


@registry.handler("test.periodic", max_attempts=1)
@registry.periodic("test.periodic", every_s=60)
def _periodic(payload):
    return {}


class Queueing(TestCase):
    def setUp(self):
        CALLS.clear()

    def test_dedupe_returns_the_live_job(self):
        a = enqueue("test.ok", {"n": 1}, dedupe_key="k1")
        b = enqueue("test.ok", {"n": 2}, dedupe_key="k1")
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(Job.objects.count(), 1)

    def test_dedupe_allows_again_once_finished(self):
        a = enqueue("test.ok", {"n": 1}, dedupe_key="k2")
        job = queue.claim("w", only_pk=a.pk)
        queue.run(job)
        b = enqueue("test.ok", {"n": 1}, dedupe_key="k2")
        self.assertNotEqual(a.pk, b.pk)

    def test_only_one_claimer_wins(self):
        job = enqueue("test.ok", {"n": 1})
        first = queue.claim("w1", only_pk=job.pk)
        second = queue.claim("w2", only_pk=job.pk)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        first.refresh_from_db()
        self.assertEqual(first.status, Job.Status.RUNNING)
        self.assertEqual(first.attempts, 1)
        self.assertEqual(first.locked_by, "w1")

    def test_success_records_the_result(self):
        job = enqueue("test.ok", {"n": 7})
        queue.run(queue.claim("w", only_pk=job.pk))
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.DONE)
        self.assertEqual(job.result, {"echo": 7})
        self.assertIsNotNone(job.finished_at)

    def test_failure_retries_with_backoff_then_gives_up(self):
        job = enqueue("test.flaky", {}, max_attempts=2)
        queue.run(queue.claim("w", only_pk=job.pk))
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.QUEUED)
        self.assertGreater(job.run_after, timezone.now())
        self.assertIn("boom", job.last_error)
        # Not runnable until the backoff has passed.
        self.assertIsNone(queue.claim("w", only_pk=job.pk))
        Job.objects.filter(pk=job.pk).update(run_after=timezone.now() - timedelta(seconds=1))
        queue.run(queue.claim("w", only_pk=job.pk))
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertEqual(job.attempts, 2)

    def test_permanent_error_does_not_retry(self):
        job = enqueue("test.permanent", {})
        queue.run(queue.claim("w", only_pk=job.pk))
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertEqual(job.attempts, 1)
        self.assertEqual(job.last_error, "the file is gone")

    def test_unknown_kind_fails_once(self):
        job = enqueue("test.nobody-handles-this", {})
        queue.run(queue.claim("w", only_pk=job.pk))
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("No handler installed", job.last_error)

    def test_stale_running_job_is_requeued(self):
        job = enqueue("test.ok", {})
        queue.claim("w", only_pk=job.pk)
        Job.objects.filter(pk=job.pk).update(heartbeat_at=timezone.now() - timedelta(minutes=10))
        self.assertEqual(queue.reclaim_stale(), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.QUEUED)
        self.assertIn("stopped responding", job.last_error)

    def test_stale_job_out_of_attempts_fails(self):
        job = enqueue("test.ok", {}, max_attempts=1)
        queue.claim("w", only_pk=job.pk)
        Job.objects.filter(pk=job.pk).update(heartbeat_at=timezone.now() - timedelta(minutes=10))
        queue.reclaim_stale()
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.FAILED)

    def test_retry_queues_a_fresh_job(self):
        job = enqueue("test.permanent", {}, dedupe_key="r1")
        queue.run(queue.claim("w", only_pk=job.pk))
        again = retry(Job.objects.get(pk=job.pk))
        self.assertNotEqual(again.pk, job.pk)
        self.assertEqual(again.status, Job.Status.QUEUED)

    def test_latest_for_ref(self):
        enqueue("test.ok", {}, ref="callrecording:5")
        newest = enqueue("test.ok", {}, ref="callrecording:5")
        self.assertEqual(latest_for("callrecording:5").pk, newest.pk)
        self.assertIsNone(latest_for("callrecording:6"))

    def test_periodic_jobs_are_bucketed(self):
        t = timezone.now()
        first = queue.enqueue_periodic(now=t)
        second = queue.enqueue_periodic(now=t)
        self.assertGreaterEqual(first, 1)
        self.assertEqual(second, 0)

    def test_purge_forgets_old_finished_jobs(self):
        job = enqueue("test.ok", {})
        Job.objects.filter(pk=job.pk).update(status=Job.Status.DONE,
                                             finished_at=timezone.now() - timedelta(days=40))
        self.assertEqual(queue.purge_finished(), 1)


class RunnerModes(TestCase):
    def setUp(self):
        CALLS.clear()

    @override_settings(JOBS_EAGER=True)
    def test_eager_runs_inside_enqueue(self):
        job = enqueue("test.ok", {"n": 3})
        self.assertEqual(job.status, Job.Status.DONE)
        self.assertEqual(CALLS, [("ok", {"n": 3})])

    @override_settings(JOBS_EAGER=False, JOBS_RUNNER="worker")
    def test_worker_presence(self):
        self.assertFalse(worker_seen())
        JobWorker.objects.create(name="host:1")
        self.assertTrue(worker_seen())
        JobWorker.objects.update(last_seen_at=timezone.now() - timedelta(minutes=5))
        self.assertFalse(worker_seen())

    @override_settings(JOBS_EAGER=False, JOBS_RUNNER="thread")
    def test_thread_mode_drains_after_commit(self):
        with mock.patch("ConvAI.async_reply.submit_ingest") as submit, \
             self.captureOnCommitCallbacks(execute=True):
            enqueue("test.ok", {})
        submit.assert_called_once()
        self.assertTrue(worker_seen())

    def test_drain_runs_everything_runnable(self):
        enqueue("test.ok", {"n": 1})
        enqueue("test.ok", {"n": 2})
        self.assertEqual(queue.drain("w"), 2)
        self.assertEqual(Job.objects.filter(status=Job.Status.DONE).count(), 2)

    def test_run_jobs_once_command(self):
        from django.core.management import call_command
        enqueue("test.ok", {"n": 1})
        call_command("run_jobs", "--once", stdout=mock.MagicMock())
        self.assertEqual(Job.objects.get().status, Job.Status.DONE)
