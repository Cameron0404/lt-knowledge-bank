#!/usr/bin/env python3
"""make_examples.py - regenerate the example pages in examples/pages/ from examples/sources/.

    python3 examples/make_examples.py

Every source and page here is an invented example. Claims are written with two placeholders
that this script fills in from the source text, so a changed source line never leaves a stale
hash or quote behind:
    {H:<path>:<line>}   the #p paragraph hash of that line
    {Q:<path>:<line>}   the whole line
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "kb"))
import knowledge  # noqa: E402

SRC = HERE / "sources"
OUT = HERE / "pages"

PAGES = {
    "kb-system-example-backup-routine.md": """\
---
id: kb-system-example-backup-routine
title: "The nightly backup routine and what it keeps"
kind: topic
subject: example-backup-routine
class: system
captured: 2026-10-01
source: "example corpus"
found_by: "example"
---

**Bottom line: the backup runs at 02:15, keeps 14 nightly copies and only reads the source.**

- It runs nightly at 02:15. [src: docs/backup-routine.md#p{H:docs/backup-routine.md:7}:7 | 2026-10-01 | q: "{Q:docs/backup-routine.md:7}" | high]
- A missing drive gives exit code 3 and one report line. [src: docs/backup-routine.md#p{H:docs/backup-routine.md:8}:8 | 2026-10-01 | q: "{Q:docs/backup-routine.md:8}" | high]
- Copies are kept for 14 days. [src: docs/backup-routine.md#p{H:docs/backup-routine.md:12}:12 | 2026-10-01 | q: "{Q:docs/backup-routine.md:12}" | high]
- The source folder is never deleted. [src: docs/backup-routine.md#p{H:docs/backup-routine.md:13}:13 | 2026-10-01 | q: "{Q:docs/backup-routine.md:13}" | high]
""",
    "kb-system-example-morning-report.md": """\
---
id: kb-system-example-morning-report
title: "What the morning report shows and where it goes"
kind: topic
subject: example-morning-report
class: system
captured: 2026-10-01
source: "example corpus"
found_by: "example"
---

**Bottom line: the morning report lists missed and failed jobs, and stays on disk.**

- It lists every job that did not run. [src: docs/morning-report.md#p{H:docs/morning-report.md:7}:7 | 2026-10-01 | q: "{Q:docs/morning-report.md:7}" | high]
- A failed job shows its exit code and log tail. [src: docs/morning-report.md#p{H:docs/morning-report.md:8}:8 | 2026-10-01 | q: "{Q:docs/morning-report.md:8}" | high]
- It is written at 07:00 and never emailed. [src: docs/morning-report.md#p{H:docs/morning-report.md:12}:12 | 2026-10-01 | q: "{Q:docs/morning-report.md:12}" | high]
""",
}


def fill(text: str) -> str:
    def lines(path):
        return (SRC / path).read_text(encoding="utf-8").split("\n")

    def h(m):
        return knowledge.para_hash(lines(m.group(1)), int(m.group(2)))

    def q(m):
        return lines(m.group(1))[int(m.group(2)) - 1].strip()

    text = re.sub(r"\{H:([^:}]+):(\d+)\}", h, text)
    return re.sub(r"\{Q:([^:}]+):(\d+)\}", q, text)


def main() -> int:
    OUT.mkdir(exist_ok=True)
    for name, text in PAGES.items():
        (OUT / name).write_text(fill(text), encoding="utf-8")
        print(f"wrote examples/pages/{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
