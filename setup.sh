#!/bin/bash
# setup.sh - first-time setup for the knowledge bank. Safe to run again: it never overwrites
# a file that already exists.
#
#   bash setup.sh            set up, then run the selftests and the demo
#   bash setup.sh --no-test  set up only
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${KB_PYTHON:-python3}"
CONF="$HOME/.config/knowledge-bank"

"$PY" - <<'PYCHECK'
import sqlite3, sys
if sys.version_info < (3, 9):
    sys.exit("knowledge bank: Python 3.9 or newer is needed, found " + sys.version.split()[0])
con = sqlite3.connect(":memory:")
try:
    con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    print("python ok, SQLite FTS5 available")
except sqlite3.OperationalError:
    print("python ok, SQLite FTS5 missing: retrieval falls back to plain token matching")
PYCHECK

mkdir -p "$CONF"
if [ ! -f "$CONF/env" ]; then
  cp "$HERE/.env.example" "$CONF/env"
  echo "wrote $CONF/env (edit it: KB_SOURCE_REPO, KB_LIVE_ROOT, KB_OWNER_NAMES)"
else
  echo "kept $CONF/env"
fi
for f in blocklist people; do
  if [ ! -f "$CONF/$f.txt" ]; then
    printf '# One entry per line. Lines starting with # are comments. Keep this file private.\n' > "$CONF/$f.txt"
    chmod 600 "$CONF/$f.txt"
    echo "wrote $CONF/$f.txt"
  fi
done
mkdir -p "$HOME/.local/state/knowledge-bank" "$HOME/kb-staging"
chmod +x "$HERE/bin/kb" "$HERE/kb/daily.sh" "$HERE/examples/demo.sh" "$HERE/tests/run_all.sh"

if [ "${1:-}" != "--no-test" ]; then
  bash "$HERE/tests/run_all.sh"
  bash "$HERE/examples/demo.sh" > /dev/null && echo "demo ok (bash examples/demo.sh to see it)"
fi
echo
echo "Next: edit $CONF/env, write pages into your staging folder, then"
echo "  $HERE/bin/kb verify ~/kb-staging && $HERE/bin/kb install --from ~/kb-staging && $HERE/bin/kb scan"
