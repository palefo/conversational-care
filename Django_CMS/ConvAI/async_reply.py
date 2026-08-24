"""Self-contained background execution for work that must not block a request.

Twilio expects the inbound webhook to return within a few seconds, but crafting
a reply can take much longer (LLM calls, audio transcription, text-to-speech).
To avoid the webhook timing out, that heavy work is offloaded to a small
in-process thread pool: the webhook returns an empty TwiML immediately and the
reply is delivered afterwards via the Twilio REST API.

This replaces the previous Redis + django-rq setup. No external broker or extra
process is required — everything runs inside the Django worker.

Two **separate** pools, because the two kinds of work have opposite shapes.
Replies are short and latency-critical; ingesting a RAG document reads a whole
PDF and then embeds it batch by batch, which can hold a thread for minutes. On
one shared pool a couple of large uploads would sit in front of every waiting
WhatsApp reply. Sizes come from ``WHATSAPP_WORKERS`` and ``RAG_WORKERS``
(see ``settings_app.py``).

Note that these pools live *inside each web process*. Work survives the browser
(the point of the exercise) but not the process, so anything queued here needs
its state in the database and a way to be picked up again — which is what
``ConvAI.rag.ingest`` does with its heartbeat and claim.
"""
from __future__ import annotations

import atexit
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.db import close_old_connections

logger = logging.getLogger(__name__)

# name -> (executor, settings key, default workers, thread prefix, label)
_POOLS: dict[str, ThreadPoolExecutor] = {}
_POOL_SPECS = {
    "reply": ("WHATSAPP_WORKERS", 2, "wa-reply", "WhatsApp/SMS reply"),
    "ingest": ("RAG_WORKERS", 2, "rag-ingest", "document ingestion"),
}
_lock = threading.Lock()


def _get_executor(pool: str = "reply") -> ThreadPoolExecutor:
    """Lazily build (once) the named background thread pool."""
    executor = _POOLS.get(pool)
    if executor is None:
        with _lock:
            executor = _POOLS.get(pool)
            if executor is None:
                key, default, prefix, label = _POOL_SPECS[pool]
                workers = max(1, int(getattr(settings, key, default) or default))
                executor = ThreadPoolExecutor(max_workers=workers,
                                              thread_name_prefix=prefix)
                # Don't block interpreter shutdown on in-flight jobs.
                atexit.register(executor.shutdown, wait=False)
                _POOLS[pool] = executor
                logger.info("Started %s pool with %d worker(s)", label, workers)
    return executor


def _run(func, args, kwargs) -> None:
    """Execute one job in a worker thread with tidy DB-connection handling.

    Each job runs outside the request/response cycle, so Django won't open or
    close the per-request DB connection for us. We refresh connections on entry
    and release them on exit to avoid leaking or reusing stale connections.
    """
    close_old_connections()
    try:
        func(*args, **kwargs)
    except Exception:  # never let a background failure crash the pool thread
        logger.exception("Background job %r failed", getattr(func, "__name__", func))
    finally:
        close_old_connections()


def submit(func, *args, **kwargs):
    """Schedule ``func(*args, **kwargs)`` on the reply pool (short, latency-critical)."""
    return _get_executor("reply").submit(_run, func, args, kwargs)


def submit_ingest(func, *args, **kwargs):
    """Schedule ``func(*args, **kwargs)`` on the ingestion pool (long-running)."""
    return _get_executor("ingest").submit(_run, func, args, kwargs)
