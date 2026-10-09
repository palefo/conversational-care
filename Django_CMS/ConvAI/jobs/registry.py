"""Which function runs which kind of job.

Handlers live in each app's ``job_handlers`` module and register themselves
with the decorators below. ``autodiscover()`` imports those modules; it is
called by whatever is about to run jobs, never at web start-up.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Handler:
    kind: str
    func: Callable[[dict], object]
    max_attempts: int = 3
    # Seconds before the first retry; doubled on each attempt after that.
    backoff_s: int = 30


@dataclass(frozen=True)
class Periodic:
    kind: str
    every_s: int
    payload: dict


_HANDLERS: dict[str, Handler] = {}
_PERIODIC: dict[str, Periodic] = {}
_discovered = False
_lock = threading.Lock()


def handler(kind: str, *, max_attempts: int = 3, backoff_s: int = 30):
    """Register ``func(payload) -> result`` as the handler for ``kind``.

    The return value is stored on the job if it is JSON-serialisable. Raise
    ``ConvAI.jobs.PermanentError`` for a failure that retrying cannot fix (the
    file is gone, the row was deleted); anything else is retried with backoff
    until ``max_attempts`` is spent.
    """
    def wrap(func):
        _HANDLERS[kind] = Handler(kind, func, max_attempts, backoff_s)
        return func
    return wrap


def periodic(kind: str, *, every_s: int, payload: dict | None = None):
    """Have the worker enqueue ``kind`` every ``every_s`` seconds.

    Used together with ``@handler`` on the same function. Enqueued with a
    time-bucketed dedupe key, so two workers cannot double it up.
    """
    def wrap(func):
        _PERIODIC[kind] = Periodic(kind, int(every_s), dict(payload or {}))
        return func
    return wrap


def autodiscover() -> None:
    """Import every installed app's ``job_handlers`` module, once."""
    global _discovered
    if _discovered:
        return
    with _lock:
        if _discovered:
            return
        from django.utils.module_loading import autodiscover_modules
        autodiscover_modules("job_handlers")
        _discovered = True
        logger.debug("Job handlers: %s", ", ".join(sorted(_HANDLERS)) or "none")


def get(kind: str) -> Handler | None:
    autodiscover()
    return _HANDLERS.get(kind)


def kinds() -> list[str]:
    autodiscover()
    return sorted(_HANDLERS)


def periodics() -> list[Periodic]:
    autodiscover()
    return list(_PERIODIC.values())
