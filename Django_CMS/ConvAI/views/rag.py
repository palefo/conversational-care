"""Knowledge-base pages for RAG-based prompt agents.

The upload endpoint takes **one file per request**. That is what lets the
browser show a real per-file progress bar (XHR upload progress) instead of one
bar for the whole batch, and it means a single bad file fails on its own rather
than taking the drop with it.

Nothing here waits for ingestion. The upload saves the bytes, creates the row
and hands the job to the background pool; the page then polls
``agent_knowledge_status``. Because that status is read straight from the
database, closing the tab, reloading, or coming back on another machine all
show the same progress — the job was never tied to the browser.
"""
from ._base import *  # noqa: F401,F403

import logging

from ..models import RagDocument
from ..rag.extract import MAX_UPLOAD_BYTES, SUPPORTED_EXTENSIONS, is_supported
from ..rag.ingest import enqueue, resume_orphans
from ..rag.retrieve import invalidate

logger = logging.getLogger(__name__)

__all__ = ['agent_knowledge', 'agent_knowledge_upload', 'agent_knowledge_status',
           'agent_knowledge_toggle', 'agent_knowledge_delete', 'agent_knowledge_retry']


def _rag_agent_or_404(pk):
    """A prompt-based agent, which is the only kind that can hold documents.

    The RAG toggle itself is deliberately *not* required: an admin who switches
    it off should still be able to see, manage and delete what was uploaded.
    """
    return get_object_or_404(Agent, pk=pk, kind=Agent.Kind.PROMPT)


def _document_json(document):
    """One document as the page's JS sees it."""
    return {
        'id': document.pk,
        'name': document.original_name,
        'size': document.size_bytes,
        'enabled': document.enabled,
        'status': document.status,
        'status_label': str(document.get_status_display()),
        'progress': document.progress,
        'chunk_total': document.chunk_total,
        'chunk_done': document.chunk_done,
        'char_count': document.char_count,
        'error': document.error,
        'embedding_model': document.embedding_model,
        'stalled': document.is_stalled(),
        'created_at': document.created_at.isoformat(),
    }


@login_required
@admin_required
def agent_knowledge(request, pk):
    """The knowledge-base page: drop zone plus the document list."""
    agent = _rag_agent_or_404(pk)
    # Anything left mid-ingest by a restart gets picked up again here, so the
    # page never shows a bar that will not move.
    resume_orphans(agent)

    from ..llm_factory import embedding_model_name
    return render(request, 'agents/agent_knowledge.html', {
        'active_page': 'agents',
        'agent': agent,
        'documents': list(agent.rag_documents.all()),
        'accept': ','.join(SUPPORTED_EXTENSIONS),
        'max_upload_mb': MAX_UPLOAD_BYTES // (1024 * 1024),
        'embedding_model': embedding_model_name(),
    })


@login_required
@admin_required
@require_GET
def agent_knowledge_status(request, pk):
    """Progress for every document, polled while anything is still working."""
    agent = _rag_agent_or_404(pk)
    documents = list(agent.rag_documents.all())
    # A document can only be stalled if its worker died; re-queue before
    # reporting, so a stuck bar recovers by itself rather than needing Retry.
    if any(d.is_stalled() for d in documents):
        resume_orphans(agent)
        documents = list(agent.rag_documents.all())
    return JsonResponse({
        'documents': [_document_json(d) for d in documents],
        'busy': any(d.status in RagDocument.ACTIVE_STATUSES for d in documents),
    })


@login_required
@admin_required
@require_POST
def agent_knowledge_upload(request, pk):
    """Accept one dropped file, store it, and queue its ingestion."""
    agent = _rag_agent_or_404(pk)

    upload = request.FILES.get('file')
    if upload is None:
        return JsonResponse({'error': str(_("No file was received."))}, status=400)
    if not is_supported(upload.name):
        return JsonResponse({'error': str(_("Unsupported file type. Accepted: "
                                            "%(types)s.") % {'types': ', '.join(SUPPORTED_EXTENSIONS)})},
                            status=400)
    if upload.size > MAX_UPLOAD_BYTES:
        return JsonResponse({'error': str(_("This file is too large (limit "
                                            "%(mb)d MB).") % {'mb': MAX_UPLOAD_BYTES // (1024 * 1024)})},
                            status=400)
    if not upload.size:
        return JsonResponse({'error': str(_("This file is empty."))}, status=400)

    document = RagDocument.objects.create(
        agent=agent,
        file=upload,
        original_name=upload.name[:255],
        size_bytes=upload.size,
        uploaded_by=request.user,
        status=RagDocument.Status.PENDING,
    )
    enqueue(document)
    return JsonResponse({'document': _document_json(document)}, status=201)


@login_required
@admin_required
@require_POST
def agent_knowledge_toggle(request, pk, doc_id):
    """Include or exclude one document from retrieval.

    Only the flag changes — the chunks and their vectors stay exactly as they
    are, so switching a document back on is instant and costs nothing.
    """
    agent = _rag_agent_or_404(pk)
    document = get_object_or_404(RagDocument, pk=doc_id, agent=agent)
    document.enabled = not document.enabled
    # updated_at moves too, and not only for tidiness: retrieval's per-process
    # cache is keyed on (count, max updated_at) of the agent's searchable
    # documents. Without this, switching one document off and another on would
    # leave both numbers unchanged and a second web process serving stale
    # vectors. (Bumping it is harmless here — stall detection only looks at
    # documents that are still being ingested.)
    document.updated_at = timezone.now()
    document.save(update_fields=['enabled', 'updated_at'])
    invalidate(agent.pk)
    return JsonResponse({'document': _document_json(document)})


@login_required
@admin_required
@require_POST
def agent_knowledge_delete(request, pk, doc_id):
    """Remove a document, its chunks (cascade) and the stored file."""
    agent = _rag_agent_or_404(pk)
    document = get_object_or_404(RagDocument, pk=doc_id, agent=agent)
    stored_file = document.file
    document.delete()
    try:
        stored_file.delete(save=False)
    except Exception:  # pragma: no cover - the row is already gone
        logger.warning("Could not delete the stored file for document %s", doc_id)
    invalidate(agent.pk)
    return JsonResponse({'deleted': doc_id})


@login_required
@admin_required
@require_POST
def agent_knowledge_retry(request, pk, doc_id):
    """Re-run ingestion for a document that failed.

    Worth offering because most failures are transient or fixable elsewhere —
    a missing API key, a rate limit part-way through embedding — and the file
    itself is already stored.
    """
    agent = _rag_agent_or_404(pk)
    document = get_object_or_404(RagDocument, pk=doc_id, agent=agent)
    document.status = RagDocument.Status.PENDING
    document.error = ""
    document.chunk_done = 0
    document.updated_at = timezone.now()
    document.save(update_fields=['status', 'error', 'chunk_done', 'updated_at'])
    enqueue(document)
    invalidate(agent.pk)
    return JsonResponse({'document': _document_json(document)})
