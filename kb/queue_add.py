#!/usr/bin/env python3
"""queue_add.py - a minimal timed work queue for the knowledge bank's nightly distil brief.

    python3 kb/queue_add.py add --at tonight --title "KB distil 2026-10-02" --file brief.md \
        [--kb-audience outward] [--max-minutes 30] [--model sonnet]
    python3 kb/queue_add.py list

Each entry is one markdown file in <queue>/pending/: frontmatter (title, kind, run_at,
kb_audience, max_minutes, model) and then the brief. The queue folder is the `scheduled-work`
folder beside this script's parent, so with the default layout it is `scheduled-work/` at the
repo root (gitignored).

This file only writes entries. Something else runs them: your own agent runner, a cron job
that hands each due brief to `claude -p`, or the full scheduler in life-tracker-kit
(https://github.com/Cameron0404/life-tracker-kit). A runner moves an entry it has started into
<queue>/done/, which is how `knowledge.py distil-queue` knows a distil ran in the last 20 hours.

--at takes `tonight` (the next 03:30), `HH:MM` (today, or tomorrow if already past),
`+Nm` / `+Nh`, or `YYYY-MM-DDTHH:MM`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import atomicio  # noqa: E402

QUEUE = HERE.parent / "scheduled-work"


def run_at(spec: str, now: dt.datetime) -> dt.datetime:
    spec = spec.strip()
    if spec == "tonight":
        t = now.replace(hour=3, minute=30, second=0, microsecond=0)
        return t if t > now else t + dt.timedelta(days=1)
    m = re.fullmatch(r"\+(\d+)([mh])", spec)
    if m:
        n = int(m.group(1))
        return now + (dt.timedelta(minutes=n) if m.group(2) == "m" else dt.timedelta(hours=n))
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", spec)
    if m:
        t = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        return t if t > now else t + dt.timedelta(days=1)
    return dt.datetime.strptime(spec, "%Y-%m-%dT%H:%M")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:48] or "entry"


def add(a) -> int:
    now = dt.datetime.now()
    when = run_at(a.at, now)
    body = Path(a.file).read_text(encoding="utf-8")
    fm = [f'title: "{a.title}"', "kind: brief", f"run_at: {when.strftime('%Y-%m-%dT%H:%M')}"]
    if a.kb_audience:
        fm.append(f"kb_audience: {a.kb_audience}")
    if a.max_minutes:
        fm.append(f"max_minutes: {a.max_minutes}")
    if a.model:
        fm.append(f"model: {a.model}")
    pending = QUEUE / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    (QUEUE / "done").mkdir(parents=True, exist_ok=True)
    name = f"{when.strftime('%Y-%m-%d-%H%M')}-{slug(a.title)}.md"
    atomicio.write_text(pending / name, "---\n" + "\n".join(fm) + "\n---\n\n" + body)
    print(f"queued {name} for {when.strftime('%Y-%m-%d %H:%M')}")
    return 0


def list_(_a) -> int:
    pending = QUEUE / "pending"
    files = sorted(pending.glob("*.md")) if pending.is_dir() else []
    for f in files:
        print(f.name)
    print(f"{len(files)} pending")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="minimal timed work queue")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add")
    p.add_argument("--at", default="tonight")
    p.add_argument("--title", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--kb-audience", default="")
    p.add_argument("--max-minutes", default="")
    p.add_argument("--model", default="")
    p.add_argument("--reason", default="")
    sub.add_parser("list")
    a = ap.parse_args(argv)
    return add(a) if a.cmd == "add" else list_(a)


if __name__ == "__main__":
    sys.exit(main())
