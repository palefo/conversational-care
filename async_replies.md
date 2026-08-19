# Asynchronous WhatsApp / SMS replies

Twilio expects the inbound webhook that delivers a WhatsApp or SMS message to
return a response within a few seconds. Generating a reply, however, can take
much longer than that — it may involve one or more LLM calls, audio
transcription, and text-to-speech. If the webhook does that work inline, Twilio
can time out and mark the delivery as failed (and may retry, causing duplicate
replies).

Conversational Care avoids this with a small, **self-contained** background
worker pool. There is **no Redis, broker, or separate worker process** to run or
operate — everything lives inside the Django web process.

## How it works

1. Twilio POSTs the inbound message to the webhook
   (`whatsapp_webhook` in [`ConvAI/views/chat.py`](Django_CMS/ConvAI/views/chat.py)).
2. When `ASYNC_WHATSAPP_REPLY` is enabled, the view **immediately** returns an
   empty TwiML document (`<Response></Response>`), acknowledging receipt so
   Twilio never times out.
3. Before returning, it hands the actual work to an in-process thread pool via
   [`ConvAI/async_reply.py`](Django_CMS/ConvAI/async_reply.py):
   - `job_reply_text` for text messages, or
   - `job_process_whatsapp_audio` for voice notes.
   Both live in [`ConvAI/tasks.py`](Django_CMS/ConvAI/tasks.py).
4. A worker thread generates the reply and pushes it back to the user with the
   Twilio REST API (`send_whatsapp_text` / `send_sms_text`, or a media message
   for audio replies) — an **outbound** call, independent of the original
   webhook request.

```
Twilio  ──POST──▶  webhook  ──▶  return <Response></Response>  ──▶  Twilio (acked)
                     │
                     └──submit()──▶  thread pool  ──▶  generate reply
                                                      └──REST API──▶  Twilio ──▶ user
```

When `ASYNC_WHATSAPP_REPLY` is **off**, the webhook keeps its original
synchronous behaviour: the reply is generated inline and returned in the TwiML
response.

## Configuration

Both settings are read from the environment (see
[`.env.sample`](Django_CMS/.env.sample)):

| Variable | Default | Description |
| --- | --- | --- |
| `ASYNC_WHATSAPP_REPLY` | `0` | Turn asynchronous replies on (`1`) or off (`0`). |
| `WHATSAPP_WORKERS` | `2` | Number of background worker threads in the reply pool. |

Choose `WHATSAPP_WORKERS` based on how many replies you expect to be generating
at the same time. Each worker handles one reply at a time, so with 2 workers up
to 2 replies are produced concurrently and further messages queue until a worker
is free. Higher values increase throughput at the cost of more concurrent
memory/CPU (and more simultaneous LLM/TTS calls) per web process. A small number
(2–4) is a sensible starting point; raise it if replies are visibly queuing
under load.

## Operational notes

- **No extra services.** Removing Redis/`django-rq` means there is nothing extra
  to deploy, monitor, or connect to. The trade-off is that queued work lives in
  memory: if the web process is restarted, replies that were still queued (not
  yet sent) are lost. For the reply workload — where messages should be answered
  within seconds anyway — this is an acceptable trade-off.
- **Multiple web processes.** If you run several Gunicorn/Uvicorn workers, each
  process has its own pool of `WHATSAPP_WORKERS` threads. Total concurrency is
  `web processes × WHATSAPP_WORKERS`.
- **Database connections.** Each job refreshes and releases its Django database
  connection (`close_old_connections`) so background threads don't leak or reuse
  stale connections.
- **Failures are isolated.** An exception in one job is logged and never crashes
  the pool or the web process.
