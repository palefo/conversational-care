from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import ConvAIUser, Message, CallRecording, Caregiver, Patient, Meeting, Protocol, Question, Answer, Agent, Conversation, SelfRegistration, Alert, RagDocument
from django.db.models import Q
from django.utils.html import format_html, escape
from django.utils.safestring import mark_safe
from django.urls import reverse

@admin.register(ConvAIUser)
class ConvAIUserAdmin(UserAdmin):
    list_display = ("username", "email", "is_staff", "phone_number", "agent")
    list_filter  = ("is_staff", "is_superuser", "is_active", "groups")
    search_fields = ("username", "email", "phone_number")

    fieldsets = UserAdmin.fieldsets + (
        ("Settings", {"fields": ("phone_number", "agent")}),
    )

    add_fieldsets = UserAdmin.add_fieldsets + (
        ("Settings", {"fields": ("phone_number", "agent")}),
    )


class HasInputAudioFilter(admin.SimpleListFilter):
    title = "Input audio"
    parameter_name = "has_input_audio"

    def lookups(self, request, model_admin):
        return (("1", "Yes"), ("0", "No"))

    def queryset(self, request, queryset):
        if self.value() == "1":
            return queryset.exclude(Q(input_audio_file="") | Q(input_audio_file__isnull=True))
        if self.value() == "0":
            return queryset.filter(Q(input_audio_file="") | Q(input_audio_file__isnull=True))
        return queryset


class HasResponseAudioFilter(admin.SimpleListFilter):
    title = "Response audio"
    parameter_name = "has_response_audio"

    def lookups(self, request, model_admin):
        return (("1", "Yes"), ("0", "No"))

    def queryset(self, request, queryset):
        if self.value() == "1":
            return queryset.exclude(Q(response_audio_file="") | Q(response_audio_file__isnull=True))
        if self.value() == "0":
            return queryset.filter(Q(response_audio_file="") | Q(response_audio_file__isnull=True))
        return queryset


# --- Admin ------------------------------------------------------------------

@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = (
        "timestamp",
        "user_short",
        "conv_short",
        "preview",
        "liked",
        "disliked",
        "warning",
        "dangerous",
        "has_in_audio",
        "has_out_audio",
        "audio_links",
    )
    list_filter = (
        ("timestamp", admin.DateFieldListFilter),
        "liked",
        "disliked",
        "warning",
        "dangerous",
        HasInputAudioFilter,
        HasResponseAudioFilter,
    )
    search_fields = (
        "user",
        "conversation_id",
        "user_message",
        "response_message",
    )
    date_hierarchy = "timestamp"
    ordering = ("-timestamp",)
    list_select_related = ()
    list_per_page = 50
    actions_on_top = True
    actions_on_bottom = True

    fieldsets = (
        ("Meta", {
            "fields": ("timestamp", "user", "conversation_id"),
        }),
        ("Content", {
            "fields": ("user_message", "response_message"),
        }),
        ("Audio", {
            "classes": ("collapse",),
            "fields": ("input_audio_file", "response_audio_file"),
        }),
        ("Flags", {
            "fields": ("liked", "disliked", "warning", "dangerous"),
        }),
    )
    readonly_fields = ("timestamp",)

    actions = [
        "mark_liked",
        "unmark_liked",
        "mark_disliked",
        "unmark_disliked",
        "mark_warning",
        "unmark_warning",
        "mark_dangerous",
        "unmark_dangerous",
        "clear_all_flags",
    ]

    # ---- Columns ----
    @admin.display(description="User", ordering="user")
    def user_short(self, obj):
        u = (obj.user or "").strip()
        return u if len(u) <= 20 else f"{u[:17]}…"

    @admin.display(description="Conv", ordering="conversation_id")
    def conv_short(self, obj):
        c = (obj.conversation_id or "").strip()
        return c if len(c) <= 12 else f"{c[:8]}…"

    @admin.display(description="Preview")
    def preview(self, obj):
        txt = (obj.user_message or obj.response_message or "").strip().replace("\n", " ")
        return txt if len(txt) <= 120 else f"{txt[:117]}…"

    @admin.display(boolean=True, description="In audio")
    def has_in_audio(self, obj):
        return bool(obj.input_audio_file)

    @admin.display(boolean=True, description="Out audio")
    def has_out_audio(self, obj):
        return bool(obj.response_audio_file)

    @admin.display(description="Audio")
    def audio_links(self, obj):
        links = []
        if obj.input_audio_file:
            url = reverse("serve_audio_file", args=[obj.id, "input"])
            links.append(f'<a href="{url}" target="_blank">Input</a>')
        if obj.response_audio_file:
            url = reverse("serve_audio_file", args=[obj.id, "output"])
            links.append(f'<a href="{url}" target="_blank">Output</a>')
        return mark_safe(" | ".join(links) or "—")

    # ---- Bulk actions ----
    @admin.action(description="Mark liked")
    def mark_liked(self, request, qs):
        qs.update(liked=True, disliked=False)

    @admin.action(description="Unmark liked")
    def unmark_liked(self, request, qs):
        qs.update(liked=False)

    @admin.action(description="Mark disliked")
    def mark_disliked(self, request, qs):
        qs.update(disliked=True, liked=False)

    @admin.action(description="Unmark disliked")
    def unmark_disliked(self, request, qs):
        qs.update(disliked=False)

    @admin.action(description="Mark warning")
    def mark_warning(self, request, qs):
        qs.update(warning=True)

    @admin.action(description="Unmark warning")
    def unmark_warning(self, request, qs):
        qs.update(warning=False)

    @admin.action(description="Mark dangerous")
    def mark_dangerous(self, request, qs):
        qs.update(dangerous=True)

    @admin.action(description="Unmark dangerous")
    def unmark_dangerous(self, request, qs):
        qs.update(dangerous=False)

    @admin.action(description="Clear all flags")
    def clear_all_flags(self, request, qs):
        qs.update(liked=False, disliked=False, warning=False, dangerous=False)

admin.site.register(CallRecording)
admin.site.register(Caregiver)
admin.site.register(Patient)
admin.site.register(Meeting)
admin.site.register(SelfRegistration)


@admin.register(Protocol)
class ProtocolAdmin(admin.ModelAdmin):
    list_display = ("number", "title")
    ordering     = ("number",)

@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ("protocol", "order")
    list_filter  = ("protocol",)

@admin.register(Answer)
class AnswerAdmin(admin.ModelAdmin):
    list_display = ("meeting", "question", "short_resp")
    list_filter  = ("question__protocol", "meeting__patient")

    def short_resp(self, obj):
        return obj.response[:60] + ("…" if len(obj.response) > 60 else "")

# ConvAI/admin.py
from django.contrib import admin
from django import forms
from django.utils.html import format_html, escape
from django.utils.safestring import mark_safe

from .models import Agent


class DetectorsKeyValueWidget(forms.Widget):
    """
    Renders Agent.detectors (JSON) as a dynamic table:
      Label | Instruction | [remove]
    Adds + button to append rows. On submit, pairs are reconstructed into a dict.
    """

    def render(self, name, value, attrs=None, renderer=None):
        value = value or {}
        if not isinstance(value, dict):
            # tolerate bad/legacy data
            value = {}

        # rows html
        rows_html = []
        idx = 0
        for label, instr in value.items():
            rows_html.append(self._row_html(name, idx, label, instr))
            idx += 1

        # if empty, render one blank starter row
        if not rows_html:
            rows_html.append(self._row_html(name, 0, "", ""))

        table = f"""
        <div class="det-kv" id="det-kv-{escape(name)}">
          <table class="det-kv__table">
            <thead>
              <tr>
                <th style="width:28%;">Label</th>
                <th>Instruction (how to detect)</th>
                <th style="width:40px;"></th>
              </tr>
            </thead>
            <tbody id="{escape(name)}-tbody">
              {''.join(rows_html)}
            </tbody>
          </table>
          <button type="button" class="button det-kv__add" data-target="{escape(name)}-tbody">+ Add detector</button>
        </div>
        {self._script_block(name, idx)}
        {self._style_block()}
        """
        return mark_safe(table)

    def value_from_datadict(self, data, files, name):
        """
        Collect all inputs like:
          {name}_key_<id>, {name}_val_<id>
        and build a dict. Blank labels are ignored.
        """
        prefix_key = f"{name}_key_"
        prefix_val = f"{name}_val_"
        out = {}
        # iterate over keys; pick those with our prefix
        for k in list(data.keys()):
            if not k.startswith(prefix_key):
                continue
            suffix = k[len(prefix_key):]
            label = (data.get(k, "") or "").strip()
            instr = (data.get(f"{prefix_val}{suffix}", "") or "").strip()
            if label:
                out[label] = instr
        return out

    # ---- helpers ----
    def _row_html(self, name, idx, label, instr):
        return f"""
          <tr class="det-kv__row" data-row="{idx}">
            <td>
              <input type="text"
                     name="{escape(name)}_key_{idx}"
                     value="{escape(label)}"
                     class="vTextField det-kv__label"
                     placeholder="e.g. Medication confusion"/>
            </td>
            <td>
              <textarea name="{escape(name)}_val_{idx}"
                        rows="2"
                        class="vLargeTextField det-kv__instr"
                        placeholder="Instruction: when should this be true?">{escape(instr)}</textarea>
            </td>
            <td class="det-kv__actions">
              <button type="button" class="button det-kv__remove" title="Remove">–</button>
            </td>
          </tr>
        """

    def _script_block(self, name, start_idx):
        # small inline JS to add/remove rows
        return f"""
<script>
(function() {{
  const tbodyId = "{escape(name)}-tbody";
  let nextIdx = {int(start_idx) + 1};

  function mkRowHTML(i) {{
    return `
      <tr class="det-kv__row" data-row="${{i}}">
        <td>
          <input type="text" name="{escape(name)}_key_${{i}}" class="vTextField det-kv__label" placeholder="e.g. Medication confusion"/>
        </td>
        <td>
          <textarea name="{escape(name)}_val_${{i}}" rows="2" class="vLargeTextField det-kv__instr" placeholder="Instruction: when should this be true?"></textarea>
        </td>
        <td class="det-kv__actions">
          <button type="button" class="button det-kv__remove" title="Remove">–</button>
        </td>
      </tr>`;
  }}

  document.addEventListener('click', function(ev) {{
    const t = ev.target;

    // Add row
    if (t.classList.contains('det-kv__add')) {{
      const targetId = t.getAttribute('data-target');
      const tbody = document.getElementById(targetId);
      tbody.insertAdjacentHTML('beforeend', mkRowHTML(nextIdx++));
      ev.preventDefault();
      return;
    }}

    // Remove row
    if (t.classList.contains('det-kv__remove')) {{
      const row = t.closest('.det-kv__row');
      if (!row) return;
      const tbody = row.parentElement;
      // If it's the only row, clear inputs instead of removing to keep one blank row
      if (tbody.querySelectorAll('.det-kv__row').length <= 1) {{
        row.querySelector('.det-kv__label').value = '';
        row.querySelector('.det-kv__instr').value = '';
      }} else {{
        row.remove();
      }}
      ev.preventDefault();
      return;
    }}
  }});
}})();
</script>
        """

    def _style_block(self):
        return """
<style>
  .det-kv__table { width:100%; border-collapse: collapse; margin-bottom: .5rem; }
  .det-kv__table th, .det-kv__table td { border-bottom: 1px solid #eee; padding: .4rem .5rem; vertical-align: top; }
  .det-kv__label { width: 100%; }
  .det-kv__instr { width: 100%; min-height: 2.5rem; }
  .det-kv__actions { text-align: center; }
  .det-kv__add { margin-top: .25rem; }
</style>
        """


class AgentForm(forms.ModelForm):
    detectors = forms.Field(widget=DetectorsKeyValueWidget(), required=False)

    class Meta:
        model = Agent
        fields = (
            "name", "kind", "native_key", "system_prompt",
            "langgraph_name", "host", "port", "tts_voice_id",
            "classification_role", "abstract_instruction",
            "detectors",
        )


@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    form = AgentForm
    list_display = ("name", "kind", "native_key", "host", "port")
    list_filter = ("kind",)
    search_fields = ("name",)
    fieldsets = (
        (None, {"fields": ("name", "kind", "native_key", "tts_voice_id")}),
        ("Prompt-based", {
            "description": "Only used when kind = Prompt-based.",
            "fields": ("system_prompt", "rag_enabled", "rag_top_k"),
        }),
        ("Remote connection", {
            "description": "Only used when kind = Remote.",
            "fields": ("langgraph_name", "host", "port"),
        }),
        ("Classification", {
            "fields": (
                "classification_role",
                "abstract_instruction",
                "detectors",
            )
        }),
    )

@admin.register(RagDocument)
class RagDocumentAdmin(admin.ModelAdmin):
    """Read-only view of RAG agents' documents.

    Uploading and deleting belong on the agent's Knowledge base page, which
    runs the ingestion; creating a row here would leave a document with no
    chunks and no job behind it. Everything is editable through that page —
    this is for looking at what a knowledge base actually contains.
    """
    list_display = ("original_name", "agent", "status", "enabled",
                    "chunk_total", "embedding_model", "updated_at")
    list_filter = ("status", "enabled", "agent")
    search_fields = ("original_name",)
    readonly_fields = ("agent", "file", "original_name", "size_bytes", "status",
                       "error", "chunk_total", "chunk_done", "char_count",
                       "embedding_model", "embedding_dim", "uploaded_by",
                       "created_at", "updated_at")

    def has_add_permission(self, request):
        return False


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = (
        "id_short",
        "topic",
        "is_important",
        "visited",
        "rating",
        "analyzed",
        "last_message_at",
        "analyzed_at",
        "message_count"
    )
    list_display_links = ("id_short", "topic")
    list_filter = (
        "is_important",
        "visited",
        "analyzed",
        "rating",
        ("last_message_at", admin.DateFieldListFilter),
        ("analyzed_at", admin.DateFieldListFilter),
    )
    search_fields = (
        "id",          # UUID search (partial works on PostgreSQL)
        "topic",
        "summary",
        "feedback",
    )
    ordering = ("-last_message_at",)
    date_hierarchy = "last_message_at"
    list_per_page = 50

    readonly_fields = ("id", "started_at", "last_message_at", "analyzed_at")
    fieldsets = (
        ("Identity & Timestamps", {
            "fields": ("id", "started_at", "last_message_at"),
        }),
        ("Feedback", {
            "fields": ("rating", "feedback", "human_flags"),
        }),
        ("Analysis", {
            "fields": (
                "summary",
                "topic",
                "is_important",
                "visited",
                "analyzed",
                "analyzed_at",
                "auto_flags"
            )
        }),
    )

    actions = [
        "action_mark_visited",
        "action_mark_unvisited",
        "action_mark_important",
        "action_mark_not_important",
        "action_clear_analysis",
    ]

    @admin.display(description="ID", ordering="id")
    def id_short(self, obj: Conversation) -> str:
        return str(obj.id)[:8]

    @admin.display(description="Msgs")
    def message_count(self, obj: Conversation) -> int:
        # No FK, so we count by conversation_id string
        return Message.objects.filter(conversation_id=str(obj.id)).count()

    # --- Bulk actions ---

    @admin.action(description="Mark as visited")
    def action_mark_visited(self, request, queryset):
        queryset.update(visited=True)

    @admin.action(description="Mark as NOT visited")
    def action_mark_unvisited(self, request, queryset):
        queryset.update(visited=False)

    @admin.action(description="Mark as important")
    def action_mark_important(self, request, queryset):
        queryset.update(is_important=True)

    @admin.action(description="Mark as NOT important")
    def action_mark_not_important(self, request, queryset):
        queryset.update(is_important=False)

    @admin.action(description="Clear analysis (summary/topic/flags) and mark unanalyzed")
    def action_clear_analysis(self, request, queryset):
        queryset.update(
            summary="",
            topic="",
            is_important=False,
            analyzed=False,
            analyzed_at=None,
        )

@admin.action(description="Mark selected alerts as Resolved")
def mark_resolved(modeladmin, request, queryset):
    queryset.update(status=Alert.AlertStatus.RESOLVED)

@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "alert_type", "priority", "status", "user", "created_at")
    list_filter = ("alert_type", "priority", "status", "user")
    search_fields = ("title", "description", "data")
    actions = [mark_resolved]