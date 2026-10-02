#!/bin/bash
# run_all.sh - every selftest, from the repo root. Exit 0 only when all pass.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.."
PY="${KB_PYTHON:-python3}"
fail=0
for t in atomicio_selftest knowledge_selftest kb_hook_selftest; do
  echo "== $t"
  if "$PY" "tests/$t.py" > "${TMPDIR:-/tmp}/kb-$t.out" 2>&1; then
    tail -1 "${TMPDIR:-/tmp}/kb-$t.out"
  else
    cat "${TMPDIR:-/tmp}/kb-$t.out" | grep -v '^PASS' | tail -40
    fail=1
  fi
done
echo "== example pages"
if "$PY" kb/knowledge.py verify examples/pages --sources examples/sources; then :; else fail=1; fi
if [ "$fail" -eq 0 ]; then echo "ALL SELFTESTS PASS"; else echo "SOME SELFTESTS FAILED"; fi
exit "$fail"
