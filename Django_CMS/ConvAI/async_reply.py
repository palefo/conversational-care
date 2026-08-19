"""Self-contained background execution for Twilio (WhatsApp/SMS) replies.

Twilio expects the inbound webhook to return within a few seconds, but crafting
a reply can take much longer (LLM calls, audio transcription, text-to-speech).
To avoid the webhook timing out, that heavy work is offloaded to a small
in-process thread pool: the webhook returns an empty TwiML immediately and the
reply is delivered afterwards via the Twilio REST API.

This replaces the previous Redis + django-rq setup. No external broker or extra
process is required — everything runs inside the Django worker. The pool size is
controlled by the ``WHATSAPP_WORKERS`` setting (see ``settings_app.py``).
"""
from __future__ import annotations

import atexit
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.db import close_old_connections

logger = logging.getLogger(__name__)

_executor: ThreadPoolExecutor | None = None
_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    """Lazily build (once) the shared reply thread pool."""
    global _executor
    if _executor is None:
        with _lock:
            if _executor is None:
                workers = max(1, int(getattr(settings, "WHATSAPP_WORKERS", 2) or 2))
                _executor = ThreadPoolExecutor(
                    max_workers=workers,
                    thread_name_prefix="wa-reply",
                )
                # Don't block interpreter shutdown on in-flight replies.
                atexit.register(_executor.shutdown, wait=False)
                logger.info("Started WhatsApp/SMS reply pool with %d worker(s)", workers)
    return _executor


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
        logger.exception("Async WhatsApp/SMS reply job failed")
    finally:
        close_old_connections()


def submit(func, *args, **kwargs):
    """Schedule ``func(*args, **kwargs)`` to run on the background reply pool."""
    return _get_executor().submit(_run, func, args, kwargs)
