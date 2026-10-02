#!/bin/bash
# demo.sh - the whole knowledge bank cycle on the invented example corpus, in a temp folder.
#
#   bash examples/demo.sh
#
# verify -> install -> scan -> ask -> status line -> the launch hook on a sample brief.
# Everything is written under a fresh temp folder. Your real bank, index and state are untouched.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KB="$HERE/../kb"
PY="${KB_PYTHON:-python3}"
T="$(mktemp -d "${TMPDIR:-/tmp}/kb-demo.XXXXXX")"
export KB_ROOT="$T/bank" KB_INDEX="$T/kb-index.db" KB_STATE_DIR="$T/state" KB_AUDIENCE_DIR="$T/aud"
export KB_RUNS_DIR="$T/runs" KB_SOURCE_REPO="$HERE/.."
mkdir -p "$KB_STATE_DIR" "$KB_AUDIENCE_DIR" "$KB_RUNS_DIR"
SRC="$HERE/sources"

echo "== 1. verify the example pages"
"$PY" "$KB/knowledge.py" verify "$HERE/pages" --sources "$SRC"
echo
echo "== 2. install them into a temp bank"
"$PY" "$KB/knowledge.py" install --from "$HERE/pages" --to "$KB_ROOT" --sources "$SRC"
echo
echo "== 3. scan: rebuild the index and re-check every quoted line"
"$PY" "$KB/knowledge.py" scan --sources "$SRC"
echo
echo "== 4. ask"
"$PY" "$KB/knowledge.py" ask "what happens when the backup drive is missing"
echo
echo "== 5. the status line"
"$PY" "$KB/knowledge.py" status --line || true
echo
echo "== 6. the launch hook on a sample private brief"
cat > "$T/entry.md" <<'ENTRY'
---
title: "Improve the backup routine"
kb_audience: private
---
ENTRY
printf 'Make the nightly backup report a missing drive more clearly in the morning report.\n' > "$T/brief.md"
"$PY" "$KB/kb_hook.py" --entry "$T/entry.md" --brief "$T/brief.md" --run demo-run || true
cat "$T/brief.md"
echo
echo "Demo folder: $T"
