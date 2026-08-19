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

exec "$@"
