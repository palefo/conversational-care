#!/bin/sh
# Runs a meeting service (LiveKit or an agent) only while online meetings are on.
#
#   meetings-gate.sh <command...>
#
# Asks the platform every MEETINGS_GATE_POLL seconds (default 60) whether the
# meeting services should run — the feature switch from Settings or
# ONLINE_MEETINGS_ENABLED, or a meeting that is still open or still handing in
# its recording. While the answer is no, only this shell and a `sleep` run
# (about 1 MB); the service starts when it turns yes and is stopped once it
# turns no again, so switching the feature off releases the memory without
# touching Docker. See online_meetings.md, "Switching it off".
#
# When the platform cannot be asked (the web app is restarting, or the key is
# wrong) nothing changes: a running service keeps running, a parked one stays
# parked. MEETINGS_GATE=0 skips all of this and runs the command directly.
#
# POSIX sh on purpose: the LiveKit image is Alpine with busybox (wget), the
# agents' image is Debian slim with Python and no wget; this works in both.

set -u

[ "${MEETINGS_GATE:-1}" = "0" ] && exec "$@"

URL="${MEETINGS_INTERNAL_API_URL:-http://web:8000}/meetings/internal/v1/gate/"
POLL="${MEETINGS_GATE_POLL:-60}"
NAME="${MEETINGS_GATE_NAME:-$1}"
child=""

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) meetings-gate[$NAME]: $*" >&2; }

if [ -z "${MEETINGS_SERVICE_KEY:-}" ]; then
  log "MEETINGS_SERVICE_KEY is not set, so the platform cannot be asked; running ungated."
  exec "$@"
fi

# Prints "run", "stop", or nothing when the platform could not be asked.
ask() {
  if command -v wget >/dev/null 2>&1; then
    body=$(wget -q -T 5 -O - --header "X-CC-Service-Key: $MEETINGS_SERVICE_KEY" "$URL" 2>/dev/null) || return 0
  else
    body=$(python3 -c '
import os, sys, urllib.request
req = urllib.request.Request(sys.argv[1], headers={"X-CC-Service-Key": os.environ["MEETINGS_SERVICE_KEY"]})
print(urllib.request.urlopen(req, timeout=5).read().decode())
' "$URL" 2>/dev/null) || return 0
  fi
  case "$body" in
    *'"run": true'*) echo run ;;
    *'"run": false'*) echo stop ;;
  esac
}

stop_child() {
  [ -n "$child" ] || return 0
  kill -TERM "$child" 2>/dev/null
  wait "$child" 2>/dev/null
  child=""
}

on_term() { stop_child; exit 0; }
trap on_term TERM INT

parked_said=""
while :; do
  answer=$(ask)
  if [ -z "$child" ]; then
    if [ "$answer" = "run" ]; then
      log "online meetings are on; starting."
      "$@" &
      child=$!
      parked_said=""
    elif [ -z "$parked_said" ]; then
      if [ "$answer" = "stop" ]; then
        log "online meetings are off; parked (checking every ${POLL}s)."
      else
        log "cannot ask the platform at $URL yet; parked, retrying."
      fi
      parked_said=1
    fi
  elif [ "$answer" = "stop" ]; then
    log "online meetings were switched off and no meeting is open; stopping."
    stop_child
    log "parked (checking every ${POLL}s)."
    parked_said=1
  fi

  # While parked and the platform did not answer (it is still starting, say),
  # ask again soon rather than a whole poll later.
  interval=$POLL
  [ -z "$child" ] && [ -z "$answer" ] && interval=10

  # Sleep in short steps so a crashed service is noticed within seconds (the
  # container then exits and Docker restarts it, as it did before the gate),
  # and in the background so `docker stop` is answered at once.
  waited=0
  while [ "$waited" -lt "$interval" ]; do
    if [ -n "$child" ] && ! kill -0 "$child" 2>/dev/null; then
      wait "$child"
      rc=$?
      log "the service exited ($rc)."
      exit "$rc"
    fi
    sleep 5 &
    wait $!
    waited=$((waited + 5))
  done
done
