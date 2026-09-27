"""Give every existing message the owner it would have been stamped with.

From 0089 on, a message's owner — ``patient`` (whose client file), ``account``
(which login it came through) and ``sender_role`` — is fixed when it is written.
This places the messages written before that, using only evidence that does
not move when a phone number does, in this order:

1. **The conversation it is in.** ``Conversation.patient`` / ``.user`` were set
   when the conversation was created and have never been re-derived.
2. **The key it is filed under.** ``reminder-<meeting>``, ``careplan-<client>``
   and ``alert-<alert>`` rows name their owner in their own key.
3. **The login it names.** Link Worker bubble and tester chat rows store a
   username, which is matched to the account exactly.
4. **Its number — only when that number belongs to exactly one client today.**
   This is the one step that trusts a phone number, so it refuses to guess:
   a number shared by two clients is left alone.

Whatever none of those place stays empty (``sender_role`` blank), and those
rows alone are still matched by number when read — see
``message_attribution.legacy_q``. The counts are printed so an installation can
see how many that is.

Reversible as a no-op: 0089's reverse drops the columns.
"""
import uuid
from collections import Counter, defaultdict

from django.db import migrations

BATCH = 1000


def backfill(apps, schema_editor):
    Message = apps.get_model("ConvAI", "Message")
    Conversation = apps.get_model("ConvAI", "Conversation")
    Patient = apps.get_model("ConvAI", "Patient")
    Meeting = apps.get_model("ConvAI", "Meeting")
    Alert = apps.get_model("ConvAI", "Alert")
    User = apps.get_model("ConvAI", "ConvAIUser")

    # Numbers as they stand today, and which side of which client each is.
    by_number = defaultdict(set)
    for p in Patient.objects.select_related("caregiver").all():
        if p.phone_number:
            by_number[str(p.phone_number)].add((p.pk, "client"))
        if p.caregiver_id and p.caregiver.phone_number:
            by_number[str(p.caregiver.phone_number)].add((p.pk, "caregiver"))

    usernames = dict(User.objects.values_list("username", "id"))

    conv_owner = {}
    key_owner = {}

    def owner_of_conversation(key):
        if key not in conv_owner:
            conv_owner[key] = (Conversation.objects.filter(id=key)
                               .values_list("patient_id", "user_id").first()
                               or (None, None))
        return conv_owner[key]

    def owner_of_key(prefix, ref):
        k = (prefix, ref)
        if k not in key_owner:
            pid = None
            if prefix == "reminder":
                pid = Meeting.objects.filter(pk=ref).values_list("patient_id", flat=True).first()
            elif prefix == "careplan":
                pid = ref if Patient.objects.filter(pk=ref).exists() else None
            elif prefix == "alert":
                pid = Alert.objects.filter(pk=ref).values_list("patient_id", flat=True).first()
            key_owner[k] = pid
        return key_owner[k]

    stats = Counter()
    pending = []
    rows = (Message.objects
            .filter(patient__isnull=True, account__isnull=True, sender_role="")
            .only("id", "conversation_id", "user", "user_message")
            .iterator(chunk_size=BATCH))

    for m in rows:
        patient_id = account_id = None
        via = ""
        sender = (m.user or "").strip()
        cid = (m.conversation_id or "").strip()

        # 1. The conversation.
        try:
            key = str(uuid.UUID(cid))
        except (TypeError, ValueError):
            key = None
        if key:
            patient_id, conv_user = owner_of_conversation(key)
            if conv_user:
                account_id = conv_user
            if patient_id or account_id:
                via = "conversation"
        # 2. The key.
        if not via and cid:
            prefix, _sep, ref = cid.partition("-")
            if ref.isdigit():
                patient_id = owner_of_key(prefix, int(ref))
                if patient_id:
                    via = "key"
        # 3. The login.
        login_id = usernames.get(sender)
        if login_id and not account_id:
            account_id = login_id
            via = via or "login"
        # 4. The number, only if exactly one client holds it.
        holders = by_number.get(sender, set())
        if not via and holders:
            pids = {pid for pid, _role in holders}
            if len(pids) == 1:
                patient_id = next(iter(pids))
                via = "number"
            else:
                stats["ambiguous number, left for fallback"] += 1

        if not via:
            stats["unplaced, left for fallback"] += 1
            continue

        # Who wrote in, from the same fixed evidence.
        if not (m.user_message or "").strip():
            role = "platform"
        elif login_id and patient_id:
            # A login typing on a client's file: the tester chat, by
            # construction — no other path wrote a username onto one.
            role = "tester"
        elif account_id and not patient_id:
            role = "api" if via == "conversation" else "staff"
        else:
            sides = {r for pid, r in holders if pid == patient_id}
            role = "caregiver" if sides == {"caregiver"} else "client"

        m.patient_id = patient_id
        m.account_id = account_id
        m.sender_role = role
        pending.append(m)
        stats[f"placed by {via}"] += 1
        if len(pending) >= BATCH:
            Message.objects.bulk_update(pending, ["patient", "account", "sender_role"])
            pending = []

    if pending:
        Message.objects.bulk_update(pending, ["patient", "account", "sender_role"])

    if stats:
        print("\n  Message owners:")
        for label, n in sorted(stats.items()):
            print(f"    {label}: {n}")


class Migration(migrations.Migration):

    dependencies = [
        ("ConvAI", "0089_message_owner_and_run_callbacks"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
