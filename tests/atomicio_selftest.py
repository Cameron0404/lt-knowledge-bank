#!/usr/bin/env python3
"""atomicio_selftest.py - the one atomic write, and a guard that nobody hand-rolls another.

    python3 tests/atomicio_selftest.py

Everything is written under a temporary directory; nothing in the vault is touched.

  1. write_text creates a new file, utf-8, mode 0644
  2. an overwrite keeps the existing file's mode (mkstemp's 0600 must not leak through)
  3. the data is fsynced before the rename, and the directory after it (os.fsync is
     recorded, so a regression that drops either call fails here)
  4. a failure mid-write leaves the old file whole and no temp file behind
  5. two writers of the same path never share a temp name (the with_suffix(".tmp") collision)
  6. no temp name is visible to a `*.json` glob while a write is in flight
  7. no module in kb/ pairs a hand-made temp file with os.replace or Path.replace.

Exit 0 when every check passes, 1 otherwise, with the failures named.
"""
from __future__ import annotations

import os
import re
import stat
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
KB = HERE.parent / "kb"
sys.path.insert(0, str(KB))
import atomicio  # noqa: E402

# Files allowed to write through their own temp-and-replace. Empty here: every write in kb/
# goes through atomicio.
HAND_ROLLED_ALLOWED: set = set()

failures = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        p = d / "state.json"

        atomicio.write_text(p, '{"café": 1}\n')
        check("1. new file written as utf-8", p.read_bytes() == '{"café": 1}\n'.encode("utf-8"))
        check("1. new file is 0644", stat.S_IMODE(p.stat().st_mode) == 0o644,
              oct(stat.S_IMODE(p.stat().st_mode)))

        os.chmod(p, 0o600)
        atomicio.write_text(p, "two\n")
        check("2. overwrite keeps mode 0600", stat.S_IMODE(p.stat().st_mode) == 0o600,
              oct(stat.S_IMODE(p.stat().st_mode)))
        os.chmod(p, 0o640)
        atomicio.write_bytes(p, b"three\n")
        check("2. overwrite keeps mode 0640", stat.S_IMODE(p.stat().st_mode) == 0o640)
        check("2. overwrite replaced the content", p.read_text() == "three\n")

        # 3. fsync is called on the data file before the replace, then on the directory.
        calls = []
        real_fsync, real_replace = os.fsync, os.replace

        def spy_fsync(fd):
            calls.append(("fsync", stat.S_ISDIR(os.fstat(fd).st_mode)))
            return real_fsync(fd)

        def spy_replace(a, b):
            calls.append(("replace", None))
            return real_replace(a, b)

        os.fsync, os.replace = spy_fsync, spy_replace
        try:
            atomicio.write_text(p, "four\n")
        finally:
            os.fsync, os.replace = real_fsync, real_replace
        check("3. file fsynced, then replaced, then directory fsynced",
              calls == [("fsync", False), ("replace", None), ("fsync", True)], repr(calls))

        # 4. a failure before the rename: old file intact, no temp left.
        class Boom(Exception):
            pass

        def boom(_a, _b):
            raise Boom()

        os.replace = boom
        try:
            atomicio.write_text(p, "five\n")
            raised = False
        except Boom:
            raised = True
        finally:
            os.replace = real_replace
        check("4. the failure is raised", raised)
        check("4. the old file is whole", p.read_text() == "four\n")
        left = [x.name for x in d.iterdir() if x.name != "state.json"]
        check("4. no temp file left behind", left == [], repr(left))

        # 5 and 6. temp names: unique, dot-prefixed, never matching the target's glob.
        names = []
        real_mkstemp = tempfile.mkstemp

        def spy_mkstemp(*a, **k):
            fd, name = real_mkstemp(*a, **k)
            names.append(os.path.basename(name))
            return fd, name

        tempfile.mkstemp = spy_mkstemp
        try:
            atomicio.write_text(p, "six\n")
            atomicio.write_text(p, "seven\n")
        finally:
            tempfile.mkstemp = real_mkstemp
        check("5. two writes of one path use two temp names", len(set(names)) == 2, repr(names))
        check("6. temp names are hidden and never *.json",
              all(n.startswith(".") and not n.endswith(".json") for n in names), repr(names))
        check("6. a relative path in the cwd works", _relative_ok(d))

    # 7. the guard: a temp file paired with a replace, outside atomicio itself.
    pat = re.compile(r"\btmp\b[^\n]*\.replace\(|os\.replace\(\s*(str\()?tmp")
    offenders = []
    for f in sorted(KB.glob("*.py")):
        if f.name in HAND_ROLLED_ALLOWED or f.name in ("atomicio.py", Path(__file__).name) \
                or f.name.endswith("_selftest.py"):
            continue
        if pat.search(f.read_text(encoding="utf-8", errors="replace")):
            offenders.append(f.name)
    check("7. no hand-rolled temp-and-replace outside the allowlist", offenders == [],
          ", ".join(offenders))
    gone = sorted(n for n in HAND_ROLLED_ALLOWED if (KB / n).exists()
                  and not pat.search((KB / n).read_text(encoding="utf-8", errors="replace")))
    if gone:   # harmless (the list can only let fewer files through), so a note, not a failure
        print("NOTE  moved onto atomicio, delete from HAND_ROLLED_ALLOWED: " + ", ".join(gone))

    print(f"atomicio_selftest: {'FAIL' if failures else 'PASS'} ({len(failures)} failure(s))")
    return 1 if failures else 0


def _relative_ok(d: Path) -> bool:
    cwd = os.getcwd()
    try:
        os.chdir(d)
        atomicio.write_text("rel.txt", "x")
        return (d / "rel.txt").read_text() == "x"
    finally:
        os.chdir(cwd)


if __name__ == "__main__":
    sys.exit(main())
