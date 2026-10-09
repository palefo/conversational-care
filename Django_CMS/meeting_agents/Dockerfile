# The meeting agents: the recorder (cc-scribe) and the voice agents (cc-voice).
# One image, two services — see docker-compose.meetings.yml.
#
# They never import Django: everything they need from the platform comes over
# the internal HTTP API, authorised per run. So this image carries no copy of
# the app's dependencies, and the app's image carries none of these.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /agents

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY cc_agents ./cc_agents

# Run as an unprivileged user that can write the shared recordings volume.
RUN useradd --uid 1000 --create-home agent && mkdir -p /media && chown agent /media
USER agent

# Overridden per service: `python -m cc_agents.scribe start` / `... voice start`.
CMD ["python", "-m", "cc_agents.voice", "start"]
