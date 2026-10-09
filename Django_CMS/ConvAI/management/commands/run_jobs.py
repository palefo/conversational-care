"""Work the background job queue.

    python manage.py run_jobs                  # all kinds, 2 threads
    python manage.py run_jobs --concurrency 1 --kinds transcribe_recording
    python manage.py run_jobs --once           # run what is runnable, then exit

Runs as the ``worker`` compose service. Each thread claims one job at a time
(see ``ConvAI.jobs.queue.claim``), so any number of workers can share a
database. On SIGTERM/SIGINT the worker stops claiming, lets running jobs finish,
and exits; a job cut short by a hard kill is re-queued by the next worker's
stale sweep once its heartbeat goes quiet.
"""
from __future__ import annotations

import logging
import signal
import threading
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from ConvAI.jobs import queue, registry

logger = logging.getLogger("ConvAI.jobs")


class Command(BaseCommand):
    help = "Run background jobs from the database queue."

    def add_arguments(self, parser):
        parser.add_argument("--concurrency", type=int, default=2,
                            help="Jobs run at the same time (threads). Default 2.")
        parser.add_argument("--kinds", default="",
                            help="Comma-separated job kinds to run. Default: all.")
        parser.add_argument("--poll", type=float, default=2.0,
                            help="Seconds to wait when the queue is empty.")
        parser.add_argument("--once", action="store_true",
                            help="Run whatever is runnable now, then exit.")

    def handle(self, *args, **opts):
        registry.autodiscover()
        kinds = [k.strip() for k in (opts["kinds"] or "").split(",") if k.strip()] or None
        concurrency = max(1, int(opts["concurrency"]))
        poll = max(0.2, float(opts["poll"]))
        name = queue.worker_name().rsplit(":", 1)[0]

        if opts["once"]:
            ran = queue.drain(name, kinds, max_jobs=10_000)
            self.stdout.write(f"Ran {ran} job(s).")
            return

        stop = threading.Event()

        def _stop(signum, _frame):
            logger.info("Signal %s received — finishing running jobs, then stopping.", signum)
            stop.set()

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        self.stdout.write(
            f"Job worker {name}: {concurrency} thread(s), kinds: "
            f"{', '.join(kinds) if kinds else 'all'} "
            f"({', '.join(registry.kinds()) or 'no handlers installed'})")

        threads = [threading.Thread(target=self._loop, args=(name, i, kinds, poll, stop),
                                    name=f"job-{i}", daemon=True)
                   for i in range(concurrency)]
        for t in threads:
            t.start()

        # The main thread keeps the books: presence, stale sweep, periodic jobs.
        from ConvAI.models import JobWorker
        last_sweep = 0.0
        try:
            while not stop.is_set():
                close_old_connections()
                try:
                    JobWorker.objects.update_or_create(
                        name=name[:128],
                        defaults={"last_seen_at": timezone.now(),
                                  "kinds": ",".join(kinds or [])[:255]})
                    if time.monotonic() - last_sweep > 30:
                        last_sweep = time.monotonic()
                        n = queue.reclaim_stale()
                        if n:
                            logger.warning("Re-queued %d job(s) whose worker stopped responding.", n)
                        queue.enqueue_periodic()
                except Exception:
                    logger.exception("Job worker housekeeping failed")
                stop.wait(10)
        finally:
            for t in threads:
                t.join(timeout=300)
            try:
                JobWorker.objects.filter(name=name[:128]).delete()
            except Exception:
                pass
            close_old_connections()
            self.stdout.write("Job worker stopped.")

    @staticmethod
    def _loop(name, index, kinds, poll, stop):
        worker = f"{name}:{index}"
        while not stop.is_set():
            close_old_connections()
            try:
                job = queue.claim(worker, kinds)
            except Exception:
                logger.exception("Could not claim a job")
                job = None
            if job is None:
                stop.wait(poll)
                continue
            logger.info("Running job %s (%s)", job.pk, job.kind)
            queue.run(job)
        close_old_connections()
