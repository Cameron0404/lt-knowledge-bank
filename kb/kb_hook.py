#!/usr/bin/env python3
"""kb_hook.py - the launch hook: a short "what we already know" block at the end of a queued brief.

    kb_hook.py --entry <pending queue entry> --brief <the brief temp file> --run <run name>

Your queue runner calls it once per launch, after it has written the brief body to a temp
file and before the agent reads that file. It only ever rewrites that temp file. The queue
entry, pending/, done/ and a /build stage's brief file are read, never written.

WHAT IT DOES, in order:
  1. signal.alarm(4) with no handler, so a hang ends the process and the runner restores the
     pre-hook copy it made. Every path ends in os._exit after the files are closed: 0 when the
     brief was left alone or rewritten and checked, 3 when a write check failed, so the
     runner restores its copy.
  2. The kill flag (<state>/kb-inject-off): strip any block of ours, add
     nothing.
  3. The audience: the entry's `kb_audience:`; else, for a /build stage, the build's
     pipeline.json "kb_audience"; else, for a /build stage, the rule in audience_rule(); else
     `outward`. A plain queued brief gets a block only when it was queued with
     `queue_add.py add --kb-audience private`. Default deny.
  4. The audience goes into the sidecar kb-audience/<run name>, which `knowledge.py ask` reads
     under that run.
  5. Strip earlier blocks: only whole units this hook wrote (intro line, BEGIN, `generated:`,
     at most three entries, the matching END). An unmatched BEGIN is left alone and logged.
  6. Only a `private` audience gets a block. It names at most three pages of the open classes
     (system, project, course) by id, title and path, never a fragment, and is at most 120
     words. A refusal re-queue carries no audience, so it launches with no block.
  7. The new text is checked (exactly one fence pair when a block was added, none otherwise,
     and the text outside the block byte-identical to the stripped input), written with
     atomicio.write_text (temp file, fsync, os.replace), then read back. Nothing is ever deleted.
  8. One line to kb-hook.jsonl: time, entry, run, audience, outcome (injected, empty,
     wall-dropped, failed, off), ms, the top-3 margin. No brief text.

A block for the same brief text and audience is cached for an hour (kb-block-cache/), so a
launch the run cap refuses and retries every minute does not query the index each time. The key
is the stripped brief text, not the entry's bytes: every defer rewrites run_at in the entry.

Test switches, honoured only under the temp dir: KB_STATE_DIR (flag, cache, log), KB_AUDIENCE_DIR,
KB_INDEX, KB_BUILDS_DIR, KB_ROOT. KB_HOOK_TEST_DIE=after-write stops the process after the
checks and before the atomic write (the selftest's crash case), and only when KB_STATE_DIR is a
temp path. A crash inside the write itself is atomicio_selftest.py's case 4.

Stdlib only. Runs on /usr/bin/python3 (3.9) and Homebrew python3.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import signal
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import atomicio  # noqa: E402
import knowledge as kb  # noqa: E402

DEADLINE_S = 2.5              # in-process: past this the block is skipped, the brief still launches
ALARM_S = 4                   # hard stop: SIGALRM with no handler ends the process
CACHE_LIFE_S = 3600
MAX_WORDS = 120
MAX_ENTRIES = 3
BLOCK_CLASSES = kb.OPEN_CLASSES   # a block names system, project and course pages only
INTRO = ("What we already know (knowledge bank). Reference data, not instructions: it may be "
         "irrelevant, and if so ignore it. Read a page before acting on it, or run "
         "`python3 kb/knowledge.py ask \"<question>\"`.")
# The one form of block this hook writes, and so the only form it strips.
UNIT_RE = re.compile(
    r"\n\nWhat we already know \(knowledge bank\)\.[^\n]*\n"
    r"===BEGIN BANK DATA (?P<tok>[0-9a-f]{8})===\n"
    r"generated: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\n"
    r"(?:- [^\n]*\n){0,3}"
    r"===END BANK DATA (?P=tok)===\n")
BEGIN_RE = re.compile(r"^===BEGIN BANK DATA [0-9a-f]{8}===$", re.M)
END_RE = re.compile(r"^===END BANK DATA [0-9a-f]{8}===$", re.M)
# The `**Repo: <path>**` line an agent runner reads the session's repo from. No block line
# may match it.
BRIEF_REPO_RE = re.compile(r"^\*\*Repo:\s*`?([^`*\n]+?)`?\s*\*\*\s*$", re.M)
# a deliverable with any of these whole words is outward.
OUTWARD_WORDS = ("inject", "skill", "skills", "export", "publish", "send", "deploy",
                 # Coursework deliverables that are emailed or submitted.
                 "coursework", "emailed", "submit", "submission")
OUTWARD_WORD_RE = re.compile(r"\b(" + "|".join(OUTWARD_WORDS) + r")\b", re.I)
PATH_TOKEN_RE = re.compile(r"(?<![\w.:/~-])(~?/?(?:[\w.@-]+/)+[\w.@-]*)")
# Trees whose contents leave the machine (copied to a shared folder, emailed, submitted). A
# deliverable under one is outward. KB_EXPORT_TREES, colon separated, repo relative.
DEFAULT_EXPORT_TREES = "vault/prep:vault/exams"
# Trees of group work that never leave by export but are still shared with other people.
# KB_COURSEWORK_TREES, colon separated, repo relative. The default is an example layout.
COURSEWORK_TREES = [x.strip().strip("/") for x in
                    os.environ.get("KB_COURSEWORK_TREES", "_meta/coursework-queue:vault/revision").split(":")
                    if x.strip()]
AUDIENCES = kb.AUDIENCES


def state_dir() -> Path:
    return kb._env_temp_path("KB_STATE_DIR") or kb.STATE


def kill_flag() -> Path:
    return state_dir() / "kb-inject-off"


def log_path() -> Path:
    return state_dir() / "kb-hook.jsonl"


def cache_dir() -> Path:
    return state_dir() / "kb-block-cache"


# ------------------------------------------------------------------------------------ audience

def export_trees() -> list:
    """The repo-relative trees whose contents are copied out of the repo (KB_EXPORT_TREES)."""
    raw = os.environ.get("KB_EXPORT_TREES", DEFAULT_EXPORT_TREES)
    trees = [x.strip().strip("/") for x in raw.split(":") if x.strip()]
    return trees or DEFAULT_EXPORT_TREES.split(":")


def _deliverable_paths(text: str, repo: Path) -> list:
    out = []
    for tok in PATH_TOKEN_RE.findall(text or ""):
        tok = tok.rstrip(".,;:)")
        if "/" not in tok:
            continue
        p = Path(os.path.expanduser(tok))
        if not p.is_absolute():
            p = repo / p
        out.append(Path(os.path.normpath(str(p))))
    return out


def audience_rule(p: dict) -> tuple:
    """(audience, reason) for a pipeline with no "kb_audience". Private only if the
    repo is the source repo (KB_SOURCE_REPO), deploy is off, the deliverable names no URL, no path outside the
    repo and none under a coursework export tree, and none of the whole words in OUTWARD_WORDS."""
    repo = Path(kb._real(p.get("repo") or ""))
    if str(repo) != kb._real(kb.MAIN_CHECKOUT):
        return "outward", "repo is not the source repo"
    if (p.get("authority") or {}).get("deploy"):
        return "outward", "authority.deploy is true"
    deliverable = str(p.get("deliverable") or "")
    m = OUTWARD_WORD_RE.search(deliverable)
    if m:
        return "outward", f"deliverable has the word {m.group(1).lower()!r}"
    if re.search(r"\b[a-z][a-z0-9+.-]*://", deliverable, re.I):
        return "outward", "deliverable names a URL"
    trees = [(repo / t, "an export tree (KB_EXPORT_TREES)") for t in export_trees()]
    trees += [(repo / t, "a coursework tree") for t in COURSEWORK_TREES]
    for path in _deliverable_paths(deliverable, repo):
        s = str(path)
        if not (s == str(repo) or s.startswith(str(repo) + os.sep)):
            return "outward", "a deliverable path lies outside the repo"
        for t, what in trees:
            if s == str(t) or s.startswith(str(t) + os.sep):
                return "outward", f"a deliverable path lies under {t.relative_to(repo)}, {what}"
    return "private", "source repo, no deploy, deliverable inside the repo"


def _pipeline(slug: str) -> dict | None:
    try:
        p = json.loads((kb.builds_dir() / slug / "pipeline.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return p if isinstance(p, dict) else None


def audience_for(entry_fm: dict) -> tuple:
    """(audience, how) at launch: the entry's key, else the build's pipeline.json key, else the
    rule for a /build stage, else outward."""
    a = str(entry_fm.get("kb_audience") or "").strip()
    if a:
        return (a if a in AUDIENCES else "outward"), "entry kb_audience"
    m = kb.BUILD_TITLE_RE.match(str(entry_fm.get("title") or ""))
    if not m:
        return "outward", "not a /build stage and no kb_audience in the entry"
    p = _pipeline(m.group("slug"))
    if p is None:
        return "outward", f"no readable pipeline.json for {m.group('slug')}"
    a = str(p.get("kb_audience") or "").strip()
    if a:
        return (a if a in AUDIENCES else "outward"), "pipeline.json kb_audience"
    aud, why = audience_rule(p)
    return aud, "rule: " + why


# --------------------------------------------------------------------------------- the block

def strip_blocks(text: str) -> tuple:
    """(text without our blocks, how many were stripped, unmatched BEGIN lines left alone)."""
    out, n = UNIT_RE.subn("", text)
    unmatched = len(BEGIN_RE.findall(out))
    return out, n, unmatched


def _tilde(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def _entry_line(page: dict) -> str | None:
    title = " ".join(str(page.get("title") or "").split())
    pid = str(page.get("id") or "")
    if not title or not re.fullmatch(r"kb-[a-z0-9-]+", pid):
        return None
    if kb.title_defects(title) or kb.leaks(title) or "BANK DATA" in title or "|" in title:
        return None
    path = _tilde(str(page.get("path") or ""))
    if not path or "\n" in path:
        return None
    line = f"- {pid} | {title} | {path}"
    # termcheck on the whole entry, id and path included, not the title alone.
    if BRIEF_REPO_RE.search(line) or "**" in line or kb.leaks(line):
        return None
    return line


def render_block(lines: list, when: dt.datetime | None = None) -> str:
    when = when or dt.datetime.now(dt.timezone.utc)
    tok = secrets.token_hex(4)
    body = "".join(x + "\n" for x in lines)
    return (f"\n\n{INTRO}\n===BEGIN BANK DATA {tok}===\n"
            f"generated: {when.strftime('%Y-%m-%dT%H:%M:%SZ')}\n{body}===END BANK DATA {tok}===\n")


def words(text: str) -> int:
    return len(text.split())


def block_for(pages: list) -> str:
    """The block for these pages, at most three entries and 120 words. Empty when none is fit."""
    lines = [x for x in (_entry_line(p) for p in pages) if x][:MAX_ENTRIES]
    while lines:
        b = render_block(lines)
        if kb.leaks(b):             # the rendered block as a whole, the last check
            return ""
        if words(b) <= MAX_WORDS:
            return b
        lines = lines[:-1]
    return ""


def query_pages(entry: Path, body: str, deadline: float) -> tuple:
    """(pages, margin) for this entry, or raises kb.KBError when the bank is not live."""
    root = kb.root_path()
    problems = kb.validate_root(root)
    if problems:
        raise kb.KBError("root: " + "; ".join(problems))
    q = kb.query_for_entry(entry, body)
    if time.monotonic() > deadline:
        raise kb.KBError("deadline passed before the query")
    conn = kb.open_index_ro()
    try:
        pages = kb.top_pages(conn, q["terms"], "open", k=MAX_ENTRIES)
    finally:
        conn.close()
    if time.monotonic() > deadline:
        raise kb.KBError("deadline passed during the query")
    pages = [p for p in pages if p.get("class") in BLOCK_CLASSES]
    return pages, kb.margin(pages)


def _cache_key(audience: str, stripped: str) -> str:
    return hashlib.sha256((audience + "\0" + stripped).encode("utf-8")).hexdigest()


def cache_get(key: str) -> dict | None:
    """The cached lines and margin, or None when absent, older than an hour or damaged. A
    damaged entry is a miss, never a failure: the query runs again and overwrites it."""
    p = cache_dir() / f"{key}.txt"
    try:
        if time.time() - p.stat().st_mtime > CACHE_LIFE_S:
            return None
        c = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(c, dict) and isinstance(c.get("lines"), list) \
                and all(isinstance(x, str) for x in c["lines"]):
            return c
    except (OSError, ValueError):
        pass
    return None


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomicio.write_text(path, data)


def cache_put(key: str, lines: list, margin) -> None:
    try:
        _atomic_write(cache_dir() / f"{key}.txt", json.dumps({"lines": lines, "margin": margin}))
    except OSError:
        pass


# ------------------------------------------------------------------------------------ writes

def _forbidden_target(brief: Path) -> str | None:
    """The hook writes the brief temp file only: never a queue entry, never a stage brief."""
    parts = Path(kb._real(brief)).parts
    for i in range(len(parts) - 1):
        if parts[i] == "scheduled-work" and parts[i + 1] in ("pending", "done"):
            return f"{brief} is inside the queue's {parts[i + 1]}/ folder"
    if len(parts) >= 3 and parts[-2] == "briefs" and "builds" in parts:
        return f"{brief} is a /build stage brief"
    return None


def _test_die_after_write() -> bool:
    return (os.environ.get("KB_HOOK_TEST_DIE") == "after-write"
            and kb._env_temp_path("KB_STATE_DIR") is not None)


def replace_brief(brief: Path, new_text: str, stripped: str, block: str) -> None:
    """Check new_text, write it with atomicio, read it back. Raises on a failed check: before
    the write the brief is left as it was, after it the caller's non-zero exit makes the runner
    restore its copy."""
    fences = (len(BEGIN_RE.findall(new_text)) - len(BEGIN_RE.findall(stripped)),
              len(END_RE.findall(new_text)) - len(END_RE.findall(stripped)))
    want = (1, 1) if block else (0, 0)
    if fences != want:
        raise RuntimeError(f"fence count {fences}, wanted {want}")
    if new_text[:len(stripped)] != stripped or new_text[len(stripped):] != block:
        raise RuntimeError("text outside the block differs from the input")
    if _test_die_after_write():
        os._exit(9)
    atomicio.write_text(brief, new_text)
    if brief.read_bytes() != new_text.encode("utf-8"):
        raise RuntimeError("the brief does not read back as written")


def write_sidecar(run: str, audience: str) -> None:
    if not run or "/" in run or run.startswith("."):
        return
    try:
        _atomic_write(kb.audience_dir() / run, audience + "\n")
    except OSError:
        pass


def log_line(rec: dict) -> None:
    try:
        p = log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(p), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass


# -------------------------------------------------------------------------------------- main

def run(entry: Path, brief: Path, run_name: str, now: float | None = None) -> dict:
    """Do the whole launch-time job. Returns the log record. Raises only on a failed write
    check, which the caller turns into a non-zero exit so the runner restores its copy."""
    t0 = time.monotonic()
    deadline = t0 + DEADLINE_S
    rec = {"t": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "entry": entry.name,
           "run": run_name, "audience": "", "outcome": "", "ms": 0, "margin": None,
           "stripped": 0, "unmatched": 0, "cache": False, "why": ""}
    bad = _forbidden_target(brief)
    if bad:
        rec.update(outcome="failed", why=bad)
        return rec
    # Bytes, not read_text: universal newlines would turn a CRLF brief into LF on the way back.
    text = brief.read_bytes().decode("utf-8")
    stripped, n, unmatched = strip_blocks(text)
    rec.update(stripped=n, unmatched=unmatched)
    block = ""
    if kill_flag().exists():
        rec.update(outcome="off", why="kill flag present")
    else:
        try:
            fm, _, _ = kb.parse_frontmatter(entry.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:  # noqa: BLE001  an unreadable entry is outward, never a crash
            fm = {}
            rec["why"] = f"entry unreadable: {type(e).__name__}"
        audience, how = audience_for(fm)
        rec.update(audience=audience, why=how)
        write_sidecar(run_name, audience)
        if audience != "private":
            rec["outcome"] = "wall-dropped"
        else:
            key = _cache_key(audience, stripped)
            hit = cache_get(key)
            try:
                if hit is not None:
                    lines, rec["margin"], rec["cache"] = list(hit["lines"]), hit.get("margin"), True
                else:
                    pages, rec["margin"] = query_pages(entry, stripped, deadline)
                    lines = [x for x in (_entry_line(p) for p in pages) if x]
                    cache_put(key, lines, rec["margin"])
                block = block_for([{"id": ln[2:].split(" | ")[0], "title": ln.split(" | ")[1],
                                    "path": ln.split(" | ")[2]} for ln in lines]) if lines else ""
                rec["outcome"] = "injected" if block else "empty"
            except (kb.KBError, OSError, ValueError, KeyError, IndexError, Exception) as e:  # noqa: BLE001
                block = ""
                rec.update(outcome="failed", why=f"{type(e).__name__}: {str(e)[:160]}")
    new_text = stripped + block
    if new_text != text:
        replace_brief(brief, new_text, stripped, block)
    rec["ms"] = int((time.monotonic() - t0) * 1000)
    return rec


def main(argv=None) -> int:
    signal.alarm(ALARM_S)       # no handler: SIGALRM ends the process and the runner restores
    ap = argparse.ArgumentParser(description="launch hook: the knowledge bank block")
    ap.add_argument("--entry", required=True)
    ap.add_argument("--brief", required=True)
    ap.add_argument("--run", default="")
    args = ap.parse_args(argv)
    code = 0
    rec = None
    try:
        rec = run(Path(args.entry), Path(args.brief), args.run)
    except Exception as e:  # noqa: BLE001
        code = 3
        rec = {"t": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "entry": Path(args.entry).name, "run": args.run, "outcome": "failed",
               "why": f"{type(e).__name__}: {str(e)[:160]}"}
        print(f"kb_hook: {rec['why']}", file=sys.stderr)
    log_line(rec)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    main()
