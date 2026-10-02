#!/bin/bash
# daily.sh - the knowledge bank's daily upkeep. Run it once a morning (cron or launchd).
#
#   bash kb/daily.sh
#
# Steps: an optional pre-scan command (KB_PRE_SCAN_CMD, for example your vault indexer), then
# `knowledge.py scan` (rebuild the index, re-check every quoted line), then, after a good scan,
# `knowledge.py distil-queue` (queue at most one distil brief for tonight).
#
# Exit code: 0 when the pre-scan step and the scan exit 0, else the first non-zero one.
# `=== kb daily done` is written to the log only when both are 0, so a monitor can read the
# last line. distil-queue's own exit never changes the result: a queue problem cannot fail
# the index. The log is KB_DAILY_LOG, else daily.log in the state folder.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${KB_PYTHON:-python3}"
STATE="${KB_STATE_HOME:-$HOME/.local/state/knowledge-bank}"
LOG="${KB_DAILY_LOG:-$STATE/daily.log}"
mkdir -p "$(dirname "$LOG")"
SRC="${KB_SOURCE_REPO:-$PWD}"
rc=0
{
  echo "=== kb daily $(date '+%Y-%m-%d %H:%M:%S %Z') ==="
  vrc=0
  if [ -n "${KB_PRE_SCAN_CMD:-}" ]; then
    bash -c "$KB_PRE_SCAN_CMD" 2>&1 | tail -20
    vrc=${PIPESTATUS[0]}
  fi
  echo "pre-scan exit=$vrc"
  (cd "$SRC" && "$PY" "$HERE/knowledge.py" scan && { "$PY" "$HERE/knowledge.py" distil-queue; true; }) 2>&1 | grep -v '^kb scan exit=' | tail -20
  krc=${PIPESTATUS[0]}
  echo "kb scan exit=$krc"
  if [ "$vrc" -eq 0 ] && [ "$krc" -eq 0 ]; then
    echo "=== kb daily done"
  else
    echo "=== kb daily FAILED"
  fi
  if [ "$vrc" -ne 0 ]; then rc=$vrc; else rc=$krc; fi
} >> "$LOG" 2>&1
tail -300 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
exit "$rc"
