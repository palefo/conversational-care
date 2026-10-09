"""RAG-based prompt agents: the knowledge-base tables and their settings.

A prompt agent gains two knobs (`rag_enabled`, `rag_top_k`) and, when RAG is on,
a set of ``RagDocument`` rows — one per uploaded file — each split into embedded
``RagChunk`` rows. Vectors are stored inline as packed float32 blobs rather than
in a separate vector store, so this migration is all that ingestion needs: no
extension, no service, no volume beyond the existing MEDIA_ROOT.

``SiteConfiguration`` picks up the embedding model / Azure deployment so the
choice can be changed at runtime alongside the other agent settings.
"""
import django.core.validators
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

import ConvAI.models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("ConvAI", "0072_retire_legacy_note_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="agent",
            name="rag_enabled",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Prompt-based agents only: give the agent a searchable "
                    "knowledge base built from documents you upload."
                ),
            ),
        ),
        migrations.AddField(
            model_name="agent",
            name="rag_top_k",
            field=models.PositiveSmallIntegerField(
                default=5,
                help_text="How many document extracts the search tool returns per query.",
                validators=[django.core.validators.MinValueValidator(1)],
            ),
        ),
        migrations.AddField(
            model_name="siteconfiguration",
            name="rag_embedding_model",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="siteconfiguration",
            name="azure_embedding_deployment",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.CreateModel(
            name="RagDocument",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("file", models.FileField(
                    upload_to=ConvAI.models._rag_upload_to,
                    validators=[django.core.validators.FileExtensionValidator(
                        allowed_extensions=["txt", "md", "pdf", "docx"])],
                )),
                ("original_name", models.CharField(max_length=255)),
                ("size_bytes", models.PositiveIntegerField(default=0)),
                ("enabled", models.BooleanField(db_index=True, default=True)),
                ("status", models.CharField(
                    choices=[("pending", "Queued"), ("extracting", "Reading text"),
                             ("chunking", "Splitting into chunks"),
                             ("embedding", "Computing vectors"),
                             ("ready", "Ready"), ("failed", "Failed")],
                    db_index=True, default="pending", max_length=16)),
                ("error", models.TextField(blank=True, default="")),
                ("chunk_total", models.PositiveIntegerField(default=0)),
                ("chunk_done", models.PositiveIntegerField(default=0)),
                ("char_count", models.PositiveIntegerField(default=0)),
                ("embedding_model", models.CharField(blank=True, default="", max_length=120)),
                ("embedding_dim", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("agent", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                            related_name="rag_documents", to="ConvAI.agent")),
                ("uploaded_by", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="RagChunk",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("ordinal", models.PositiveIntegerField(default=0)),
                ("text", models.TextField()),
                ("embedding", models.BinaryField()),
                ("document", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                               related_name="chunks", to="ConvAI.ragdocument")),
            ],
            options={"ordering": ["document_id", "ordinal"]},
        ),
        migrations.AddIndex(
            model_name="ragdocument",
            index=models.Index(fields=["agent", "enabled", "status"],
                               name="rag_doc_agent_enabled_idx"),
        ),
        migrations.AddIndex(
            model_name="ragchunk",
            index=models.Index(fields=["document", "ordinal"],
                               name="rag_chunk_doc_ordinal_idx"),
        ),
    ]
