"""Background jobs, queued in the database.

    from ConvAI.jobs import enqueue
    enqueue("transcribe_recording", {"recording_id": rec.pk},
            ref=f"callrecording:{rec.pk}", dedupe_key=f"transcribe:{rec.pk}")

A job is a ``ConvAI.models.Job`` row. Handlers are plain functions registered
with ``@handler("kind")`` in an app's ``job_handlers`` module, which is imported
only by something that is about to *run* jobs — the ``run_jobs`` worker, or the
web process in eager / thread mode. A web process that only enqueues never
imports them, so a handler's dependencies cost nothing where they are not used.

How jobs get run is ``JOBS_RUNNER``:

* ``worker`` (default) — the ``run_jobs`` management command, as its own
  compose service. Survives web restarts; the only mode that runs periodic jobs.
* ``thread`` — the web process drains the queue on the ingestion pool after
  each enqueue. No extra container, at the price of work stopping when the web
  process does (it is picked up again on the next enqueue or restart).

``JOBS_EAGER=1`` runs a job inline, inside ``enqueue``. Tests use it.

See background_jobs.md.
"""
from .queue import (  # noqa: F401
    PermanentError,
    cancel,
    enqueue,
    latest_for,
    retry,
    worker_seen,
)
from .registry import handler, periodic  # noqa: F401
