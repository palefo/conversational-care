#!/bin/sh
# Build the Tailwind CSS bundle before starting the app.
# Runs on every container start so the dev bind-mount (.:/app) always has a
# freshly built stylesheet; in production the same file is also baked into the
# image at build time.
set -e

echo "→ Building Tailwind CSS…"
tailwindcss \
  -c /app/tailwind/tailwind.config.js \
  -i /app/tailwind/input.css \
  -o /app/ConvAI/static/app/css/tailwind.css \
  --minify || echo "⚠ Tailwind build failed — continuing with existing CSS (if any)."

# Online meetings: the recorder agent runs as an unprivileged user (UID 1000,
# see meeting_agents/Dockerfile) and writes into the shared media volume. The
# call_recordings folder there is created by this container as root, so the
# meetings subfolder is made here and handed to that user — otherwise the
# recorder fails with "Permission denied" on its first meeting.
REC_DIR="${CALL_RECORDINGS_DIR:-/media/call_recordings}/meetings"
if [ "$(id -u)" = "0" ] && [ -d "$(dirname "$REC_DIR")" -o -d /media ]; then
  mkdir -p "$REC_DIR" && chown "${MEETINGS_AGENT_UID:-1000}:${MEETINGS_AGENT_UID:-1000}" "$REC_DIR" \
    || echo "⚠ Could not prepare $REC_DIR for the meeting recorder."
fi

exec "$@"
