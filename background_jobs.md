# Background jobs

Long work that must survive the request that asked for it — transcribing a
recording, mixing an online meeting down — runs on a **database-backed job
queue**, worked by a separate `worker` process. It sits beside the in-process
thread pools described in [async_replies.md](async_replies.md), which stay right
for short, losable work like a WhatsApp reply.

## Why a queue

Transcription used to run **inside the request**: pressing *Transcribe* held the
navigator's page — and a web thread — for as long as Whisper took on an hour of
audio, and the work was lost if either gave up. Now the button queues a job and
returns at once; the panel shows the job running, failed (with a retry), or that
no worker is picking it up, and fills in the transcript when it is done.

## The pieces

| Piece | Where | What it does |
|---|---|---|
| `Job` | `ConvAI.models` | One unit of work: kind, payload, status, attempts, heartbeat, error, result. |
| `JobWorker` | `ConvAI.models` | A worker as last heard from, so pages can say "nobody is working the queue". |
| `enqueue()` | `ConvAI.jobs` | Queue a job; with a `dedupe_key`, return the job already queued or running instead of a second one. |
| `@handler` / `@periodic` | `ConvAI.jobs` | Register a function for a kind (and optionally run it on a schedule). Handlers live in each app's `job_handlers.py`. |
| `run_jobs` | `manage.py run_jobs` | The worker: claims and runs jobs, heartbeats, re-queues work from crashed workers, enqueues periodic jobs. |

```python
from ConvAI.jobs import enqueue
enqueue("transcribe_recording", {"recording_id": rec.pk},
        ref=f"callrecording:{rec.pk}", dedupe_key=f"transcribe:{rec.pk}")
```

## How it stays correct

- **Claiming is one conditional `UPDATE … WHERE status = 'queued'`.** Of any
  number of workers that pick the same row, exactly one sees a row count of 1.
  It works on Postgres and on the sqlite test database alike.
- **Heartbeats.** A running job writes `heartbeat_at` every 15 s. A job whose
  heartbeat is older than two minutes has lost its worker and is re-queued (or
  failed, if out of attempts) by the next worker's sweep.
- **Retries with backoff.** A failure is retried after `backoff_s × 2^(attempt-1)`
  seconds (plus jitter) until `max_attempts`. Raise `PermanentError` for a
  failure no retry can fix (the file is gone).
- **Unknown kinds fail once.** A job whose handler is not installed — say, from
  an app that has since been removed — fails with "No handler installed", not
  forever.
- **Imported only where jobs run.** Handlers load in the worker (and in the web
  process only in eager/thread mode), so a web process that only queues never
  pays for a handler's dependencies.

## Running it

The `worker` service in `docker-compose.yml` runs
`python manage.py run_jobs --concurrency 2` from the same image and code as
`web` (~768 MB limit, mostly the app's own import cost).

| Setting | Default | Meaning |
|---|---|---|
| `JOBS_RUNNER` | `worker` | `worker`: the `run_jobs` service does the work. `thread`: the web process drains the queue on its ingestion pool after each enqueue — no extra container, but work pauses while web restarts, and periodic jobs do not run. |
| `JOBS_EAGER` | `0` | Run each job inline inside `enqueue()`. Tests and debugging only. |

Useful commands:

```bash
python manage.py run_jobs --once                      # run what is runnable, then exit
python manage.py run_jobs --kinds transcribe_recording # only some kinds
```

Finished jobs are kept for 30 days (periodic housekeeping rows for one) and then
purged by the `purge_jobs` periodic job.

## Jobs that exist

| Kind | Queued by | Does |
|---|---|---|
| `transcribe_recording` | *Transcribe* buttons; an online meeting's mixdown | Whisper → summary → key moments, saved on the `CallRecording`. |
| `meetings.finalize_recording` | Ending an online meeting | Mixes the per-speaker tracks into one recording, then queues its transcription. See [online_meetings.md](online_meetings.md). |
| `meetings.reconcile` | Every 2 minutes | Closes rooms that emptied or ran over time, replaces a lost recorder. Cheap when no meeting is live. |
| `purge_jobs` | Daily | Forgets old finished jobs. |

## Transcription changes that came with it

- Files over Whisper's **25 MB** limit are cut into 10-minute windows (with a 2 s
  overlap so no word is lost on a cut) and stitched back on one clock. This
  fixes Twilio calls over about 26 minutes, which used to fail.
- Segments Whisper itself flags as **probably silence and low-confidence** are
  dropped ("Thanks for watching!" over a minute of nothing).
- **Online recordings are never sent to Twilio** for a WAV twin; they are
  transcribed one clean speaker track at a time, and the transcript names who
  spoke.
- The **spelling hint** comes from the recording's own client first, then from
  phone numbers for older rows.

Tests: `ConvAI/test_jobs.py`, `ConvAI/test_transcribe_tracks.py`.
