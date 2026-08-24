"""Ingestion: uploaded file → chunks → vectors, on the background pool.

The job runs off the request thread (``ConvAI.async_reply``), so an upload
returns as soon as the bytes are on disk and the browser is free to go
anywhere. **All** of its state lives in the ``RagDocument`` row — status, stage,
chunks done, error — which is what makes a page reload harmless: the page is
only ever a view onto those rows, never the owner of the job.

That also covers the harder case, a process restart mid-ingest. The row's
``updated_at`` doubles as the worker's heartbeat, so a job whose worker died is
recognisable (``RagDocument.is_stalled``) and gets picked up again by
``resume_orphans`` the next time anybody looks at the knowledge base. Claiming
is a single conditional UPDATE, so two web workers racing to resume the same
document cannot both win.
"""
from __future__ import annotations

import logging
import struct
from datetime import timedelta

from django.db import close_old_connections, transaction
from django.db.models import Q
from django.utils import timezone

from ..models import RagChunk, RagDocument

logger = logging.getLogger(__name__)

# Chunking. ~1000 characters is a paragraph or two: big enough to answer a
# question on its own, small enough that a retrieved chunk is mostly signal.
# The overlap keeps a sentence that straddles a boundary retrievable from both
# sides. Character-based (not token-based) so the behaviour is identical across
# languages — a token-budgeted splitter silently produces much smaller Chinese
# or Korean chunks than English ones.
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150

# Chunks per embedding request. Large enough to keep the round-trips down,
# small enough that the progress bar actually moves on a big document.
EMBED_BATCH = 64

# A document that hasn't been touched for this long while claiming to be
# in-flight has lost its worker.
STALE_AFTER_SECONDS = 300

# The in-flight statuses other than "queued" — i.e. a worker said it was
# working on this. Used to spot the ones whose worker is gone.
_RUNNING_STATUSES = tuple(
    s for s in RagDocument.ACTIVE_STATUSES if s != RagDocument.Status.PENDING
)


# ---------------------------------------------------------------------------
# Vector packing
#
# Vectors are stored as raw little-endian float32 — 4 bytes a dimension, so a
# 1536-dim vector is 6 KB, against ~24 KB as JSON text. They are L2-normalised
# on the way in so cosine similarity at query time is a plain dot product.
# ---------------------------------------------------------------------------
def pack_vector(values) -> bytes:
    """Normalise a float sequence and pack it as little-endian float32 bytes."""
    norm = sum(v * v for v in values) ** 0.5
    if norm > 0:
        values = [v / norm for v in values]
    return struct.pack(f"<{len(values)}f", *values)


def _touch(document: RagDocument, **fields) -> None:
    """Persist progress and refresh the heartbeat in one write."""
    fields["updated_at"] = timezone.now()
    for name, value in fields.items():
        setattr(document, name, value)
    RagDocument.objects.filter(pk=document.pk).update(**fields)


def _fail(document: RagDocument, message: str) -> None:
    logger.warning("RAG ingestion failed for document %s: %s", document.pk, message)
    _touch(document, status=RagDocument.Status.FAILED, error=message[:2000])


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------
def _claim(document_id: int) -> RagDocument | None:
    """Take ownership of a document, or return None if somebody else has it.

    The conditional UPDATE is the whole locking story: it matches only rows
    that are queued, or in-flight but stale (their worker is gone). Whichever
    process's UPDATE touches a row first is the one that ingests it.
    """
    cutoff = timezone.now() - timedelta(seconds=STALE_AFTER_SECONDS)
    claimed = RagDocument.objects.filter(pk=document_id).filter(
        Q(status=RagDocument.Status.PENDING)
        | Q(status__in=_RUNNING_STATUSES, updated_at__lt=cutoff)
    ).update(
        status=RagDocument.Status.EXTRACTING,
        error="",
        chunk_done=0,
        updated_at=timezone.now(),
    )
    if not claimed:
        return None
    return RagDocument.objects.filter(pk=document_id).first()


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------
def job_ingest_document(document_id: int) -> None:
    """Background entry point: extract, chunk and embed one document.

    Never raises — a failure is written to the row as ``status='failed'`` with
    the reason, which is what the knowledge-base page shows next to a Retry
    button.
    """
    close_old_connections()
    try:
        document = _claim(document_id)
        if document is None:
            logger.debug("Document %s already claimed elsewhere; skipping", document_id)
            return
        _ingest(document)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Unexpected failure ingesting document %s", document_id)
        document = RagDocument.objects.filter(pk=document_id).first()
        if document is not None:
            _fail(document, f"Unexpected error: {exc}")
    finally:
        close_old_connections()


def _ingest(document: RagDocument) -> None:
    from .extract import ExtractionError, extract_text

    # --- 1. Extract -------------------------------------------------------
    try:
        path = document.file.path
    except Exception:
        _fail(document, "The uploaded file is no longer available on disk.")
        return

    try:
        text = extract_text(path, document.original_name)
    except ExtractionError as exc:
        _fail(document, str(exc))
        return
    except Exception as exc:
        _fail(document, f"Could not read this file: {exc}")
        return

    if not text.strip():
        _fail(document, "No text could be read from this file. If it is a "
                        "scanned PDF, it needs to be OCR'd before uploading.")
        return

    _touch(document, status=RagDocument.Status.CHUNKING, char_count=len(text))

    # --- 2. Chunk ---------------------------------------------------------
    try:
        chunks = split_text(text)
    except Exception as exc:
        _fail(document, f"Could not split this document: {exc}")
        return
    if not chunks:
        _fail(document, "This document produced no usable text.")
        return

    # Re-ingesting (a retry, or a file that failed halfway) starts clean rather
    # than appending a second copy of every chunk.
    RagChunk.objects.filter(document=document).delete()

    _touch(document, status=RagDocument.Status.EMBEDDING,
           chunk_total=len(chunks), chunk_done=0)

    # --- 3. Embed ---------------------------------------------------------
    from ..llm_factory import embedding_model_name, make_embeddings

    try:
        embeddings = make_embeddings()
    except Exception as exc:
        _fail(document, str(exc))
        return

    model_name = embedding_model_name()
    dimension = 0
    done = 0
    for start in range(0, len(chunks), EMBED_BATCH):
        batch = chunks[start:start + EMBED_BATCH]
        try:
            vectors = embeddings.embed_documents(batch)
        except Exception as exc:
            _fail(document, f"Embedding failed after {done} of {len(chunks)} "
                            f"chunks: {exc}")
            return

        dimension = len(vectors[0]) if vectors else dimension
        RagChunk.objects.bulk_create([
            RagChunk(document=document, ordinal=start + offset,
                     text=chunk_text, embedding=pack_vector(vector))
            for offset, (chunk_text, vector) in enumerate(zip(batch, vectors))
        ])
        done += len(batch)
        # One write per batch: the page polls this, and it is also the
        # heartbeat that stops a live job being treated as orphaned.
        _touch(document, chunk_done=done)

    _touch(document, status=RagDocument.Status.READY, chunk_done=len(chunks),
           embedding_model=model_name, embedding_dim=dimension, error="")
    logger.info("Ingested %r for agent %s: %d chunks (%s)",
                document.original_name, document.agent_id, len(chunks), model_name)


def split_text(text: str) -> list[str]:
    """Split extracted text into overlapping chunks.

    Uses LangChain's recursive splitter (already a dependency) so paragraph and
    sentence boundaries are preferred over arbitrary cuts. Its default
    separators are punctuation-based and work across the platform's languages.
    """
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        length_function=len,
    )
    return [c.strip() for c in splitter.split_text(text) if c.strip()]


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------
def enqueue(document: RagDocument) -> None:
    """Hand one document to the ingestion pool.

    Deliberately *not* the reply pool: embedding a long PDF holds a thread for
    minutes, and a couple of uploads would otherwise queue up in front of every
    waiting WhatsApp reply.
    """
    from ..async_reply import submit_ingest
    # on_commit, not straight to the pool: a worker that starts before the row
    # is committed would find nothing to claim. Outside a transaction (the
    # normal case here — the project runs in autocommit) this fires at once.
    document_id = document.pk
    transaction.on_commit(lambda: submit_ingest(job_ingest_document, document_id))


def resume_orphans(agent) -> int:
    """Re-queue this agent's documents that no worker is on any more.

    Called whenever the knowledge base is looked at. Two cases end up here: a
    document queued by a process that has since restarted, and one that was
    mid-embed when the process went down. Re-queuing is safe because the job
    claims before doing anything, and ingestion always rebuilds a document's
    chunks from scratch.
    """
    cutoff = timezone.now() - timedelta(seconds=STALE_AFTER_SECONDS)
    orphans = RagDocument.objects.filter(agent=agent, updated_at__lt=cutoff,
                                         status__in=RagDocument.ACTIVE_STATUSES)
    count = 0
    for document in orphans:
        enqueue(document)
        count += 1
    if count:
        logger.info("Re-queued %d orphaned ingestion(s) for agent %s", count, agent.pk)
    return count
