"""Putting jobs on the queue, taking them off, and running one.

Claiming is a single conditional ``UPDATE ... WHERE status = 'queued'``: of any
number of workers that pick the same row, exactly one sees a row count of 1.
That is the same idea as ``rag.ingest._claim`` and works on every database the
project runs on (Postgres in production, sqlite under test), which a
``SELECT ... FOR UPDATE SKIP LOCKED`` would not — sqlite ignores it. At the
volume this queue sees (a handful of jobs a minute) the extra round trip is
not worth optimising away.
"""
from __future__ import annotations

import logging
import os
import random
import socket
import threading
import traceback
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, close_old_connections, transaction
from django.db.models import F
from django.utils import timezone

from . import registry

logger = logging.getLogger(__name__)

HEARTBEAT_EVERY_S = 15
# A running job whose heartbeat is older than this has lost its worker.
STALE_AFTER_S = 120
# How long a worker counts as present after its last heartbeat.
WORKER_PRESENT_S = 60


class PermanentError(Exception):
    """A failure retrying cannot fix. The job fails at once, with this message."""


def _Job():
    from ..models import Job
    return Job


def _runner() -> str:
    return (getattr(settings, "JOBS_RUNNER", "") or "worker").strip().lower()


def _eager() -> bool:
    return bool(getattr(settings, "JOBS_EAGER", False))


def worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"


# ── enqueue ────────────────────────────────────────────────────────────────

def enqueue(kind: str, payload: dict | None = None, *, ref: str = "",
            dedupe_key: str | None = None, run_after=None, priority: int = 0,
            max_attempts: int | None = None, created_by=None):
    """Queue ``kind`` and return its Job.

    With a ``dedupe_key`` that already names a queued or running job, that job
    is returned instead and nothing new is queued.
    """
    Job = _Job()
    if dedupe_key:
        live = Job.objects.filter(dedupe_key=dedupe_key,
                                  status__in=[Job.Status.QUEUED, Job.Status.RUNNING]).first()
        if live:
            return live

    fields = dict(
        kind=kind, payload=payload or {}, ref=ref or "", dedupe_key=dedupe_key,
        run_after=run_after or timezone.now(), priority=priority,
        created_by=created_by if getattr(created_by, "pk", None) else None,
    )
    if max_attempts is not None:
        fields["max_attempts"] = max_attempts
    else:
        # A handler is only looked up here when it is cheap to: in eager and
        # thread mode the web process is about to run the job anyway. In worker
        # mode the default stands and the worker applies the handler's own.
        if _eager() or _runner() == "thread":
            h = registry.get(kind)
            if h:
                fields["max_attempts"] = h.max_attempts

    try:
        with transaction.atomic():
            job = Job.objects.create(**fields)
    except IntegrityError:
        # Lost a race on the dedupe key: someone queued the same work between
        # the check above and the insert. Theirs is the job.
        live = Job.objects.filter(dedupe_key=dedupe_key,
                                  status__in=[Job.Status.QUEUED, Job.Status.RUNNING]).first()
        if live:
            return live
        raise

    _kick(job)
    return job


def _kick(job) -> None:
    """Get ``job`` run in whichever way this installation runs jobs."""
    if _eager():
        claimed = claim(worker_name(), only_pk=job.pk)
        if claimed:
            run(claimed)
        job.refresh_from_db()
        return
    if _runner() == "thread":
        transaction.on_commit(lambda: _submit_drain())


def _submit_drain() -> None:
    from ..async_reply import submit_ingest
    submit_ingest(drain, worker_name())


# ── claim / run ────────────────────────────────────────────────────────────

def claim(worker: str, kinds=None, only_pk=None):
    """Take one runnable job, or return None."""
    Job = _Job()
    now = timezone.now()
    qs = Job.objects.filter(status=Job.Status.QUEUED, run_after__lte=now)
    if kinds:
        qs = qs.filter(kind__in=list(kinds))
    if only_pk is not None:
        qs = qs.filter(pk=only_pk)
    for pk in qs.order_by("-priority", "run_after", "pk").values_list("pk", flat=True)[:10]:
        won = Job.objects.filter(pk=pk, status=Job.Status.QUEUED).update(
            status=Job.Status.RUNNING, locked_by=worker[:128], locked_at=now,
            heartbeat_at=now, started_at=now, attempts=F("attempts") + 1,
        )
        if won:
            return Job.objects.get(pk=pk)
    return None


def _heartbeat_loop(pk: int, stop: threading.Event) -> None:
    Job = _Job()
    while not stop.wait(HEARTBEAT_EVERY_S):
        try:
            Job.objects.filter(pk=pk, status=Job.Status.RUNNING).update(
                heartbeat_at=timezone.now())
        except Exception:  # a missed beat is not worth killing the job over
            logger.debug("Heartbeat failed for job %s", pk, exc_info=True)
        finally:
            close_old_connections()


def run(job) -> None:
    """Run a claimed job to completion and record the outcome."""
    Job = _Job()
    h = registry.get(job.kind)
    if h is None:
        # Typically an app that queued it has since been removed. Retrying
        # cannot install it, so this fails once rather than forever.
        _finish(job, Job.Status.FAILED, error=f'No handler installed for "{job.kind}".')
        return

    stop = threading.Event()
    beat = None
    if not _eager():
        beat = threading.Thread(target=_heartbeat_loop, args=(job.pk, stop),
                                name=f"job-beat-{job.pk}", daemon=True)
        beat.start()
    try:
        result = h.func(dict(job.payload or {}))
    except PermanentError as exc:
        _finish(job, Job.Status.FAILED, error=str(exc) or exc.__class__.__name__)
    except Exception as exc:  # noqa: BLE001 — every failure is recorded
        logger.exception("Job %s (%s) failed", job.pk, job.kind)
        detail = f"{exc.__class__.__name__}: {exc}".strip()
        if job.attempts < max(job.max_attempts, 1):
            delay = h.backoff_s * (2 ** max(job.attempts - 1, 0))
            delay = delay + random.uniform(0, delay * 0.2)
            run_after = timezone.now() + timedelta(seconds=delay)
            Job.objects.filter(pk=job.pk).update(
                status=Job.Status.QUEUED, run_after=run_after, locked_by="",
                locked_at=None, heartbeat_at=None, last_error=detail[:4000],
            )
            if _runner() == "thread" and not _eager():
                t = threading.Timer(delay + 1, _submit_drain)
                t.daemon = True
                t.start()
        else:
            _finish(job, Job.Status.FAILED,
                    error=detail + "\n\n" + traceback.format_exc(limit=6))
    else:
        _finish(job, Job.Status.DONE, result=_jsonable(result))
    finally:
        stop.set()
        if beat:
            beat.join(timeout=1)


def _jsonable(value):
    import json
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return {"repr": repr(value)[:500]}


def _finish(job, status, *, error: str = "", result=None) -> None:
    Job = _Job()
    Job.objects.filter(pk=job.pk).update(
        status=status, finished_at=timezone.now(), heartbeat_at=None,
        last_error=(error or "")[:8000], result=result,
    )


def drain(worker: str, kinds=None, max_jobs: int = 50) -> int:
    """Run jobs until none is runnable (thread mode). Returns how many ran."""
    ran = 0
    while ran < max_jobs:
        close_old_connections()
        job = claim(worker, kinds)
        if job is None:
            break
        run(job)
        ran += 1
    return ran


# ── maintenance ────────────────────────────────────────────────────────────

def reclaim_stale(stale_after_s: int = STALE_AFTER_S) -> int:
    """Re-queue running jobs whose worker stopped beating. Returns the count."""
    Job = _Job()
    cutoff = timezone.now() - timedelta(seconds=stale_after_s)
    stale = Job.objects.filter(status=Job.Status.RUNNING, heartbeat_at__lt=cutoff)
    n = 0
    for job in stale:
        if job.attempts < max(job.max_attempts, 1):
            n += Job.objects.filter(pk=job.pk, status=Job.Status.RUNNING).update(
                status=Job.Status.QUEUED, run_after=timezone.now(), locked_by="",
                locked_at=None, heartbeat_at=None,
                last_error="The worker running this job stopped responding.",
            )
        else:
            n += Job.objects.filter(pk=job.pk, status=Job.Status.RUNNING).update(
                status=Job.Status.FAILED, finished_at=timezone.now(), heartbeat_at=None,
                last_error="The worker running this job stopped responding.",
            )
    return n


def retry(job, *, by=None):
    """Queue a finished job again with a fresh set of attempts."""
    Job = _Job()
    if job.is_live:
        return job
    if job.dedupe_key:
        live = Job.objects.filter(dedupe_key=job.dedupe_key,
                                  status__in=[Job.Status.QUEUED, Job.Status.RUNNING]).first()
        if live:
            return live
    return enqueue(job.kind, job.payload, ref=job.ref, dedupe_key=job.dedupe_key,
                   priority=job.priority, max_attempts=job.max_attempts, created_by=by)


def cancel(job) -> bool:
    """Cancel a job that has not started. Running jobs cannot be interrupted."""
    Job = _Job()
    return bool(Job.objects.filter(pk=job.pk, status=Job.Status.QUEUED).update(
        status=Job.Status.CANCELLED, finished_at=timezone.now()))


def latest_for(ref: str, kind: str | None = None):
    """The newest job about ``ref`` (e.g. ``"callrecording:12"``), or None."""
    Job = _Job()
    qs = Job.objects.filter(ref=ref)
    if kind:
        qs = qs.filter(kind=kind)
    return qs.order_by("-created_at", "-pk").first()


def worker_seen() -> bool:
    """Is anything going to run a queued job?

    True in eager and thread mode, where the web process runs them itself, and
    in worker mode when a ``run_jobs`` process has checked in recently.
    """
    if _eager() or _runner() == "thread":
        return True
    from ..models import JobWorker
    cutoff = timezone.now() - timedelta(seconds=WORKER_PRESENT_S)
    return JobWorker.objects.filter(last_seen_at__gte=cutoff).exists()


def enqueue_periodic(now=None) -> int:
    """Queue each periodic job whose bucket has not been queued yet."""
    now = now or timezone.now()
    n = 0
    stamp = int(now.timestamp())
    for p in registry.periodics():
        bucket = stamp // max(p.every_s, 1)
        key = f"periodic:{p.kind}:{bucket}"
        Job = _Job()
        if Job.objects.filter(dedupe_key=key).exists():
            continue
        enqueue(p.kind, p.payload, dedupe_key=key, max_attempts=1)
        n += 1
    return n


def purge_finished(older_than_days: int = 30) -> int:
    """Delete finished job rows older than the window. Returns the count."""
    Job = _Job()
    cutoff = timezone.now() - timedelta(days=older_than_days)
    finished = [Job.Status.DONE, Job.Status.CANCELLED, Job.Status.FAILED]
    deleted, _ = Job.objects.filter(status__in=finished, finished_at__lt=cutoff).delete()
    # Periodic housekeeping leaves a row each run; a day of them is plenty.
    day = timezone.now() - timedelta(days=1)
    more, _ = Job.objects.filter(status__in=finished, finished_at__lt=day,
                                 dedupe_key__startswith="periodic:").delete()
    return deleted + more
