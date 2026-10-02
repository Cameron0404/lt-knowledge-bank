#!/usr/bin/env python3
"""knowledge.py - the knowledge bank: distilled, cited pages in KB_LIVE_ROOT (~/vault/wiki/knowledge/).

    python3 kb/knowledge.py verify <dir>                 lint pages, exit 0 or 1 (one line per defect)
    python3 kb/knowledge.py install --from <dir> [--to <root>]   verify, then copy into the bank
    python3 kb/knowledge.py status [--json | --line]     root check, pages by class, newest source
    python3 kb/knowledge.py scan [--if-due]              verify every claim, rebuild the index
    python3 kb/knowledge.py ask "<question>" [--audience A] [--brief F] [--k 3] [--json]
    python3 kb/knowledge.py pointers --to <dir> [--sources D]   mechanical pointer pages
    python3 kb/knowledge.py distil-queue [--dry-run]     queue at most one KB distil brief
    python3 kb/knowledge.py purge --source <path or id>  take a source out of the bank (by hand)

Configuration is by environment variable (see .env.example and the README). The defaults
fit a git repo laid out like the Life Tracker (docs/, _meta/, vault/) and a notes vault at
~/vault.

WHAT A PAGE IS. One markdown file, frontmatter (id, title, kind, subject, class, captured,
source, found_by), a bold bottom line, and bullets. Every bullet is one claim and carries its
citation in brackets at the end of the line:

    - <claim> [src: <path>@<commit>:<line> | <source date> | q: "<the whole source line>" | high]
    - <claim> [src: <path>#p<hash8>:<a-b> | <source date> | q: "<the whole source line>" | inferred]
    - <claim> [src: <path> | <source date> | pointer]

@<commit> is for files git tracks, #p<hash8> (sha256 of the whitespace-normalised paragraph)
for everything else. A pointer claim carries no quoted text: decision sources (goals,
decisions.md, target.md) and paths of unknown volatility may only be pointed at.

THE WALL. A page's class is the most restrictive class among its cited sources, its
`source:` and its `found_by:`. The class table is an allow-list: a source not on it
fails verify. Some sources are excluded outright (group chats, the group roster, raw lecture
and call transcripts, the bank's own outputs). A page that names a listed person, or gives a
role label to fewer than three people, or attributes speech to a role, fails verify: what
people said is attributed to the meeting. The names list is built in memory on every run and
never written anywhere.

WRITES. Only `install` writes, only into a validated root, and never deletes: an overwritten
page's previous text goes to _history/. The live root is KB_LIVE_ROOT, a real
folder. KB_ROOT is honoured only under the system temp dir or KB_STAGING.
Under a managed run (LT_RUN_ID set) install is refused unless the run is the knowledge-bank
ship stage or a KB distil run.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import atomicio  # noqa: E402

HOME = Path.home()


def _cfg(var: str, default) -> Path:
    """A path from the environment (see .env.example), else the default."""
    v = os.environ.get(var)
    return Path(os.path.abspath(os.path.expanduser(v))) if v else Path(default)


# The git repo whose files the pages cite (docs/, CLAUDE.md, reports). Defaults to the folder
# the command runs in. Claims on tracked files are checked against KB_MAIN_BRANCH there.
MAIN_CHECKOUT = _cfg("KB_SOURCE_REPO", Path.cwd())
REPO = MAIN_CHECKOUT
MAIN_BRANCH = os.environ.get("KB_MAIN_BRANCH", "main")
VAULT = _cfg("KB_VAULT", HOME / "vault")
LIVE_ROOT = _cfg("KB_LIVE_ROOT", VAULT / "wiki" / "knowledge")
STAGING = _cfg("KB_STAGING", HOME / "kb-staging")
STATE = _cfg("KB_STATE_HOME", HOME / ".local" / "state" / "knowledge-bank")
FIXTURES = HERE / "fixtures" / "knowledge"
MANIFEST = "_kb-manifest.json"
HISTORY = "_history"

CLASSES = ["system", "project", "course", "club", "career", "dictation", "private"]
RANK = {c: i for i, c in enumerate(CLASSES)}
KINDS = {"topic", "project", "decision", "pointer"}
REQUIRED = ["id", "title", "kind", "subject", "class", "captured", "source", "found_by"]
VOLATILITIES = ("decision", "state", "procedure", "reference", "unknown")
POINTER_ONLY = {"decision", "unknown"}


class KBError(Exception):
    pass


# ---------------------------------------------------------------------------------------------
# Paths that tests may redirect, and only to places that cannot be the live bank

def _real(p) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(str(p))))


def _temp_anchors() -> list:
    out = []
    for a in (tempfile.gettempdir(), "/tmp"):
        for form in (os.path.abspath(a), _real(a)):
            if form not in out:
                out.append(form)
    return out


def _under(path, anchors) -> str | None:
    """The anchor `path` lies strictly below, or None. Compared on both the raw and the
    resolved form, because /tmp and /var/folders are symlinks on macOS."""
    ap, rp = os.path.abspath(os.path.expanduser(str(path))), _real(path)
    for a in anchors:
        for form in (ap, rp):
            if form.startswith(a.rstrip(os.sep) + os.sep):
                return a
    return None


def under_temp(path) -> bool:
    return _under(path, _temp_anchors()) is not None


def root_path() -> Path:
    env = os.environ.get("KB_ROOT")
    if not env:
        return LIVE_ROOT
    if under_temp(env) or _under(env, [str(STAGING), _real(STAGING)]) or _real(env) == _real(STAGING):
        return Path(os.path.abspath(os.path.expanduser(env)))
    raise KBError(f"KB_ROOT={env} refused: honoured only under the temp dir or {STAGING}")


def _env_temp_path(var: str) -> Path | None:
    v = os.environ.get(var)
    if v and under_temp(v):
        return Path(v)
    if v:
        print(f"knowledge: {var} ignored, it is honoured only under the temp dir", file=sys.stderr)
    return None


def index_path() -> Path:
    return _env_temp_path("KB_INDEX") or STATE / "kb-index.db"


def runs_dir() -> Path:
    return _env_temp_path("KB_RUNS_DIR") or MAIN_CHECKOUT / "_meta" / "agent-runs"


# ---------------------------------------------------------------------------------------------
# Page parsing

def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def parse_frontmatter(text: str) -> tuple[dict, str, int]:
    """(fields, body, the line number the body starts on). Flat `key: value` only, with a
    trailing `# comment` dropped from unquoted values."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text, 1
    fm = {}
    for i in range(1, len(lines)):
        line = lines[i]
        if line.strip() == "---":
            return fm, "\n".join(lines[i + 1:]), i + 2
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if not m:
            continue
        v = m.group(2).strip()
        if v and v[0] in "\"'":
            end = v.find(v[0], 1)
            v = v[1:end] if end > 0 else v[1:]
        else:
            v = re.sub(r"\s+#.*$", "", v).strip()
        fm[m.group(1)] = v
    return {}, text, 1


CLAIM_RE = re.compile(
    r"^- (?P<text>.+?) \[src: (?P<loc>[^|\]]+?) \| (?P<date>\d{4}-\d{2}-\d{2}) \| "
    r"(?:q: \"(?P<frag>.*)\" \| (?P<fid>high|inferred)|(?P<ptr>pointer))\]\s*$")
LOC_RE = re.compile(
    r"^(?P<path>[^@#:\s][^@#:]*?)(?:@(?P<commit>[0-9a-f]{7,40})|#p(?P<ph>[0-9a-f]{8}))?"
    r"(?::(?P<a>\d+)(?:-(?P<b>\d+))?)?$")


def parse_claim(line: str) -> dict | None:
    m = CLAIM_RE.match(line.rstrip())
    if not m:
        return None
    loc = LOC_RE.match(m.group("loc").strip())
    if not loc:
        return None
    a = int(loc.group("a")) if loc.group("a") else None
    b = int(loc.group("b")) if loc.group("b") else a
    return {"text": m.group("text").strip(), "path": loc.group("path").strip(),
            "commit": loc.group("commit"), "phash": loc.group("ph"), "a": a, "b": b,
            "date": m.group("date"), "fragment": m.group("frag"),
            "fidelity": m.group("fid") or "", "pointer": bool(m.group("ptr"))}


def load_page(path) -> dict:
    """Frontmatter, body and claims of one page. Bullets that do not parse as claims are kept
    in `bad_bullets` with their line numbers: an uncited bullet is an uncited claim."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    fm, body, start = parse_frontmatter(text)
    claims, bad = [], []
    for i, line in enumerate(body.split("\n")):
        if line.startswith("- ") or line.startswith("* "):
            c = parse_claim(line)
            if c is None:
                bad.append((start + i, line))
            else:
                c["line_no"] = start + i
                claims.append(c)
    return {"path": path, "file": path.name, "text": text, "fm": fm, "body": body,
            "claims": claims, "bad_bullets": bad}


# ---------------------------------------------------------------------------------------------
# Sources: where a cited path lives, its volatility, its class, and exclusions

class Sources:
    """Resolves the path a claim cites. The live resolver maps repo-relative paths to the repo,
    `~/` to the home folder and `../career/` to the sibling repo. A fixture resolver maps the
    same logical paths under one folder (`home/` for `~/`, `career/` for `../career/`), so the
    class and volatility rules, which work on the logical path, are the same in both."""

    def __init__(self, root: Path | None = None, repo: Path | None = None, git_dir: Path | None = None):
        self.root = root
        self.repo = Path(repo) if repo is not None else REPO
        self.git_dir = Path(git_dir) if git_dir is not None else MAIN_CHECKOUT
        self._tracked = None
        self._cache = {}
        self._main = {}
        self.git_show_calls = 0

    def lines_main(self, logical: str) -> list | None:
        """The file as it stands on main, from one cached `git show main:<path>` per path per
        scan, run in the main checkout. None when git cannot show it."""
        if logical not in self._main:
            self.git_show_calls += 1
            try:
                r = subprocess.run(["git", "-C", str(self.git_dir), "show", f"{MAIN_BRANCH}:{logical}"],
                                   capture_output=True, timeout=20)
                self._main[logical] = (r.stdout.decode("utf-8", "replace").split("\n")
                                       if r.returncode == 0 else None)
            except (OSError, subprocess.SubprocessError):
                self._main[logical] = None
        return self._main[logical]

    def commit_date(self, logical: str) -> str | None:
        """The date of the last commit on main that touched the path, cached per scan."""
        key = "date:" + logical
        if key not in self._main:
            try:
                r = subprocess.run(["git", "-C", str(self.git_dir), "log", "-1", "--format=%cs",
                                    MAIN_BRANCH, "--", logical], capture_output=True, timeout=20)
                v = r.stdout.decode("utf-8", "replace").strip()
                self._main[key] = v if r.returncode == 0 and _date_ok(v) else None
            except (OSError, subprocess.SubprocessError):
                self._main[key] = None
        return self._main[key]

    def resolve(self, logical: str) -> Path:
        if self.root is not None:
            if logical.startswith("~/"):
                return self.root / "home" / logical[2:]
            if logical.startswith("../career/"):
                return self.root / "career" / logical[len("../career/"):]
            return self.root / logical
        if logical.startswith("~/"):
            return HOME / logical[2:]
        if logical.startswith("../"):
            return self.repo.parent / logical[3:]
        if self.repo == REPO and main_only(logical):
            return MAIN_CHECKOUT / logical
        return self.repo / logical

    def lines(self, logical: str) -> list | None:
        if logical not in self._cache:
            try:
                self._cache[logical] = self.resolve(logical).read_text(
                    encoding="utf-8", errors="replace").split("\n")
            except OSError:
                self._cache[logical] = None
        return self._cache[logical]

    def tracked(self, logical: str) -> bool | None:
        """True or False for a repo-relative path in the live resolver, None when it does not
        apply (fixtures, paths outside the repo, git unreadable)."""
        if self.root is not None or logical.startswith(("~/", "../", "/")):
            return None
        if self._tracked is None:
            try:
                out = subprocess.run(["git", "ls-files", "-z"], cwd=str(self.repo), capture_output=True,
                                     timeout=20).stdout.decode("utf-8", "replace")
                self._tracked = set(x for x in out.split("\0") if x)
            except (OSError, subprocess.SubprocessError):
                self._tracked = set()
                return None
        return logical in self._tracked


# Gitignored folders (.gitignore `_inbox/`, `_meta/agent-runs/`, `_meta/builds/`): their files
# exist only in the main checkout, so a build worktree reads them from there, as runs_dir() and
# builds_dir() already do. Without this a seed or pointer run in a worktree sees no dictation,
# report or build output, and a page that cites one fails verify there but not on main.
MAIN_ONLY = ("_inbox/", "_meta/agent-runs/", "_meta/builds/")


def main_only(logical: str) -> bool:
    p = _norm_path(logical)
    return p == "_inbox" or p.startswith(MAIN_ONLY)


def fixture_sources_for(d) -> Path | None:
    """The committed fixture pages cite the committed fixture sources."""
    try:
        Path(d).resolve().relative_to(FIXTURES.resolve())
    except ValueError:
        return None
    return FIXTURES / "sources"


def _norm_path(p: str) -> str:
    p = p.strip()
    home = str(HOME) + "/"
    if p.startswith(home):
        p = "~/" + p[len(home):]
    while p.startswith("./"):
        p = p[2:]
    return p


def volatility(path: str) -> str:
    """decision | state | procedure | reference | unknown, from the logical path alone.
    Dictation files count as reference: they are the owner's own dated notes, and their lines
    may be quoted through `ask`, which pointer-only would forbid."""
    p = _norm_path(path)
    if (p.startswith("_meta/goals/") or p == "_meta/decisions.md"
            or re.match(r"^_meta/builds/[^/]+/target\.md$", p) or p.endswith("/target.md")):
        return "decision"
    if (p.startswith("_meta/agent-runs/") or re.match(r"^_meta/builds/[^/]+/out/", p)
            or p == "_meta/session-ledger.md"):
        return "state"
    if p.startswith("docs/") or p == "CLAUDE.md" or re.match(r"^_meta/scripts/[^/]+\.md$", p):
        return "procedure"
    if (p.startswith("vault/") or p.startswith("~/vault/wiki/")
            or p.startswith(CLUB_MEETINGS + "/")
            or re.match(r"^_inbox/candidate-dictation-", p)
            or p.startswith("~/vault/raw/inbox/dictations/")):
        return "reference"
    return "unknown"


# THE EXAMPLE LAYOUT. The path rules below (volatility, EXCLUDED, source_class) describe one
# example layout: a git repo with docs/, _meta/ (goals, decisions, run reports, build outputs,
# group chat captures, a group roster), _sources/transcripts/, _inbox/ for dictations, vault/
# for course notes and interview prep, a club's minutes in ~/Documents/Club/meetings/, and a
# notes vault at ~/vault. Edit these three functions to fit your own layout; the selftests
# show what each rule is meant to hold.
CLUB_MEETINGS = "~/Documents/Club/meetings"


def _tilde_logical(p: Path) -> str:
    """A configured folder in the logical form claims use (~/... under the home folder)."""
    return _norm_path(str(p))


def _extra_excludes() -> list:
    """KB_EXCLUDE: colon-separated logical path prefixes that are never a source, for example
    call recordings or video frames. The default is an example of such folders."""
    raw = os.environ.get("KB_EXCLUDE", "~/Documents/Recordings:_meta/video")
    return [x.strip().rstrip("/") for x in raw.split(":") if x.strip()]


def _excluded_rules() -> list:
    """(regex on the logical path, reason) pairs; the reason is what verify prints. Group chats,
    rosters and transcripts are other people's words. The bank's own build, runs, folder,
    staging and state are excluded so the bank never distils itself."""
    rules = [
        (r"^_meta/study(/|$)", "group chat captures (_meta/study/)"),
        (r"^_meta/group-info\.md$", "the group roster (_meta/group-info.md)"),
        (r"^_sources/transcripts/lectures(/|$)", "raw lecture transcript"),
        (r"^" + re.escape(CLUB_MEETINGS) + r"/inbox(/|$)", "call transcript (club inbox)"),
        (r"^_sources/transcripts(/|$)", "raw transcript"),
        (r"^_meta/agent-runs/_reports/[^/]*knowledge-ban", "a knowledge-bank build report"),
        (r"^_meta/agent-runs/[^/]*knowledge-ban", "a knowledge-bank build run"),
        (r"^_meta/agent-runs/[^/]*kb-distil", "a KB distil run"),
        (r"^_meta/builds/knowledge-bank(/|$)", "the knowledge-bank build itself"),
    ]
    for d in _extra_excludes():
        rules.append((r"^" + re.escape(_norm_path(d)) + r"(/|$)", f"excluded by KB_EXCLUDE ({d})"))
    for folder, why in ((LIVE_ROOT, "the bank itself"), (STAGING, "the bank's staging folder"),
                        (STATE, "the bank's own state")):
        rules.append((r"^" + re.escape(_tilde_logical(folder)) + r"(/|$)", why))
    return rules


EXCLUDED = _excluded_rules()
FENCE_RE = re.compile(r"===(BEGIN|END) BANK DATA")


def excluded(path: str, lines: list | None = None) -> str | None:
    """Why this source may not be cited, or None. A file holding a bank fence is a brief the
    hook already wrote into: citing it would make the bank distil itself."""
    p = _norm_path(path)
    for rx, why in _excluded_rules():
        if re.search(rx, p):
            return why
    if lines is not None and any(FENCE_RE.search(x) for x in lines):
        return "holds a BANK DATA fence (the bank's own output)"
    return None


def source_class(path: str) -> str | None:
    """The class of one cited path from the allow-list, or None when the path is
    not on it. Reports and build outputs return "report": the caller decides system or private
    by reading them."""
    p = _norm_path(path)
    if p.startswith("docs/") or p == "CLAUDE.md" or re.match(r"^_meta/scripts/[^/]+\.md$", p):
        return "system"
    if p.startswith("_meta/agent-runs/_reports/") or re.match(r"^_meta/builds/[^/]+/out/", p):
        return "report"
    if (p.startswith("_meta/goals/") or p == "_meta/decisions.md"
            or re.match(r"^_meta/builds/[^/]+/target\.md$", p)):
        return "project"
    if p.startswith("vault/prep/careers/"):
        return "career"
    if p.startswith("vault/exams/") or p.startswith("vault/prep/"):
        return "course"
    if p.startswith(CLUB_MEETINGS + "/"):
        return "club"
    if re.match(r"^_inbox/candidate-dictation-", p) or p.startswith("~/vault/raw/inbox/dictations/"):
        return "dictation"
    if p.startswith("vault/interview-prep/") or p.startswith("../career/") or p.startswith("obligations/co-"):
        return "career"
    return None


def most_restrictive(classes) -> str:
    cs = [c for c in classes if c in RANK]
    return max(cs, key=lambda c: RANK[c]) if cs else "system"


# Free-text `source:` and `found_by:` values: a word that says where the text came from lifts
# the page's class, so an author cannot label a career page "system".
PROVENANCE_WORDS = [
    (r"\b(dictation|dictated|wispr|voice note)s?\b", "dictation"),
    (r"\b(career|careers|interview|recruit\w*)s?\b", "career"),
    (r"\bclub\b", "club"),
    (r"\b(whatsapp|mail|gmail|outlook|inbox)\b", "private"),
]


def provenance_class(text: str) -> str | None:
    out = []
    for token in re.findall(r"(?:~/|\.\./|[\w.-]+/)[\w./ -]*[\w.-]", text or ""):
        c = source_class(token)
        if c and c != "report":
            out.append(c)
    for rx, c in PROVENANCE_WORDS:
        if re.search(rx, text or "", re.I):
            out.append(c)
    return most_restrictive(out) if out else None


# ---------------------------------------------------------------------------------------------
# Personal-pipeline terms (termcheck) and people

_LEAK = None


def _leak_checker():
    """(blocklist, find_term) from termcheck.py, or None when it cannot be loaded, in
    which case every report counts as private (fail closed)."""
    global _LEAK
    if _LEAK is None:
        try:
            import termcheck as dc  # noqa: E402
            _LEAK = (dc.blocklist(), dc.find_term, dc.squash)
        except Exception as exc:  # noqa: BLE001
            print(f"knowledge: termcheck unavailable ({exc}); reports count as private",
                  file=sys.stderr)
            _LEAK = False
    return _LEAK or None


def leaks(text: str) -> bool:
    """True when the text names a personal-pipeline employer, vendor or requisition id."""
    lc = _leak_checker()
    if lc is None:
        return True
    terms, find_term, squash = lc
    low = text.lower()
    sq = squash(low)
    return any(find_term(low, sq, t) is not None for t in terms)


# The owner's own name parts, which are never treated as another person (KB_OWNER_NAMES,
# space separated, for example "ada lovelace").
OWNER = set(os.environ.get("KB_OWNER_NAMES", "").lower().split())
# Common English words that happen to be names too, never treated as a name term.
NOT_NAMES = {"the", "and", "all", "team", "everyone", "unknown", "tbd", "previous", "lt", "none",
             "group", "member", "members", "new", "will", "may", "mark", "grace", "rose", "art",
             "bill", "sam"}


def _name_tokens(full: str) -> list:
    full = " ".join(full.split())
    if not full or full.upper() == "UNKNOWN":
        return []
    out = [full.lower()] if " " in full else []
    for tok in re.split(r"[\s,]+", full):
        t = tok.strip(".'’()").lower()
        if len(t) >= 3 and t.isalpha() and t not in OWNER and t not in NOT_NAMES:
            out.append(t)
    if " " in full and all(t in OWNER for t in full.lower().split()):
        return []
    return out


def _names_from_sources() -> list:
    """Other people's names, from KB_PEOPLE_FILE (one name per line, kept outside any repo),
    the group roster in the source repo, and the `attendees`, `absent` and task `owner` fields
    of every JSON file in the folders KB_ATTENDEE_DIRS names (colon separated: meeting minutes
    imports, recorder metadata). Held in memory only."""
    raw = []
    pf = os.environ.get("KB_PEOPLE_FILE")
    if pf:
        try:
            raw += [x for x in Path(os.path.expanduser(pf)).read_text(encoding="utf-8").split("\n")
                    if x.strip() and not x.lstrip().startswith("#")]
        except OSError:
            pass
    gi = REPO / "_meta" / "group-info.md"
    try:
        for line in gi.read_text(encoding="utf-8").split("\n"):
            m = re.match(r"^members_\w+:\s*([^#]*)", line)
            if m:
                raw += [x for x in m.group(1).split(",") if x.strip()]
    except OSError:
        pass

    def people(v):
        if isinstance(v, str):
            return [v]
        if isinstance(v, dict):
            return [str(v.get("name") or "")]
        if isinstance(v, list):
            return [n for x in v for n in people(x)]
        return []

    dirs = [Path(os.path.expanduser(x.strip())) for x in os.environ.get("KB_ATTENDEE_DIRS", "").split(":")
            if x.strip()]
    for d in dirs:
        for f in sorted(d.glob("*.json")) if d.is_dir() else []:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            m = data.get("meeting") if isinstance(data.get("meeting"), dict) else data
            raw += people(m.get("attendees")) + people(m.get("absent"))
            raw += [n for tk in (data.get("tasks") or []) if isinstance(tk, dict) for n in people(tk.get("owner"))]
    return raw


def names(fixture: bool = False) -> set:
    """Lower-case name terms (full names and their distinctive parts). KB_NAMES_FILE, under the
    temp dir only, replaces the real sources so tests are deterministic. Verifying the
    committed fixtures adds the fixture names file, whose names are invented."""
    f = _env_temp_path("KB_NAMES_FILE")
    raw = []
    if f is not None:
        raw = f.read_text(encoding="utf-8").split("\n")
    else:
        raw = _names_from_sources()
        if fixture:
            raw += (FIXTURES / "names.txt").read_text(encoding="utf-8").split("\n")
    out = set()
    for r in raw:
        if r.strip() and not r.lstrip().startswith("#"):
            out.update(_name_tokens(r))
    return out


def names_in(text: str, name_terms) -> bool:
    low = text.lower()
    lc = _leak_checker()
    if lc is not None:
        sq = lc[2](low)
        return any(lc[1](low, sq, n) is not None for n in name_terms)
    return any(re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", low) for n in name_terms)


# A role label that points at one or two people is a name by another route. Speech is
# attributed to "the meeting", never to a role.
SMALL_ROLE_RE = re.compile(
    r"\b(?:the|our|their|his|her|my)\s+(?:vice[- ]president|president|vp|treasurer|secretary|"
    r"chair(?:person)?|head of [a-z]+|group lead|team lead|lead|leader|director|manager|"
    r"coordinator|co-?founder|founder|ceo|cto|cfo|coo|professor|lecturer|tutor|recruiter|"
    r"interviewer|client|boss|flatmate|partner|other member|other one)\b"
    r"|\b(?:both|the two|two of the|one of the two|either of the)\s+"
    r"(?:members|teammates|people|of them|leads|presidents|students|colleagues)\b", re.I)
ROLE_NOUN = (r"(?:member|members|lead|leader|president|treasurer|secretary|chair|director|"
             r"manager|teammate|teammates|colleague|colleagues|professor|someone|person|student|"
             r"classmate|recruiter|interviewer|founder)")
SPEECH_RE = re.compile(r"\b" + ROLE_NOUN + r"\b(?:\s+\w+){0,3}?\s+(?:said|says|argued|suggested|"
                       r"proposed|told|asked|thinks|thought|complained|claimed|insisted|wants|"
                       r"wanted|disagreed|objected)\b", re.I)

IMPERATIVES = {
    "ignore", "disregard", "forget", "override", "run", "execute", "delete", "remove", "send",
    "push", "merge", "deploy", "install", "write", "create", "stop", "do", "don't", "dont",
    "never", "always", "must", "you", "please", "set", "add", "update", "change", "use", "call",
    "open", "click", "reply", "email", "publish", "commit", "reset", "kill", "follow", "obey",
    "act", "make", "read", "print", "tell", "skip", "copy", "move", "rename", "edit", "fix",
}


def title_defects(title: str) -> list:
    out = []
    words = title.split()
    if not words:
        return ["empty title"]
    if len(words) > 12:
        out.append(f"title has {len(words)} words, at most 12")
    first = words[0].strip("\"'`*_([{:,.;!?").lower()
    if first in IMPERATIVES:
        out.append(f"title starts with an imperative ({first})")
    if re.search(r"\*\*\s*repo\s*:", title, re.I) or re.match(r"^\s*repo\s*:", title, re.I):
        out.append("title carries a **Repo: line")
    if FENCE_RE.search(title) or "===" in title or re.search(r"bank data", title, re.I):
        out.append("title carries a fence marker")
    if "\n" in title or "`" in title:
        out.append("title carries a newline or backtick")
    return out


# ---------------------------------------------------------------------------------------------
# Fragments and paragraph hashes

def _ws(s: str) -> str:
    return " ".join((s or "").split())


def _candidates(lines: list, i: int) -> list:
    """What a fragment for 0-based line i may equal: the whole line, or, for a line under 20
    characters, the line joined with the next one."""
    cur = _ws(lines[i])
    out = [cur]
    if len(cur) < 20 and i + 1 < len(lines):
        out.append(_ws(cur + " " + lines[i + 1]))
    return out


def fragment_ok(lines: list, a: int, b: int, fragment: str) -> bool:
    """The fragment is a whole cited source line (whitespace-normalised) found within 3 lines of
    the cited line or range. A part of a line does not count."""
    frag = _ws(fragment)
    if not frag or not lines:
        return False
    lo, hi = max(1, a - 3), min(len(lines), b + 3)
    for n in range(lo, hi + 1):
        if frag in _candidates(lines, n - 1):
            return True
    return False


def para_hash(lines: list, line_no: int) -> str | None:
    """First 8 hex of sha256 of the whitespace-normalised paragraph holding 1-based line_no."""
    i = line_no - 1
    if i < 0 or i >= len(lines) or not lines[i].strip():
        return None
    s = i
    while s > 0 and lines[s - 1].strip():
        s -= 1
    e = i
    while e + 1 < len(lines) and lines[e + 1].strip():
        e += 1
    return hashlib.sha256(_ws(" ".join(lines[s:e + 1])).encode("utf-8")).hexdigest()[:8]


# ---------------------------------------------------------------------------------------------
# Class and verification

def page_class(page: dict, sources: Sources, name_terms=None) -> str:
    """The most restrictive class among the page's cited sources, its source: and found_by:.
    A report or build output is system only if it passes termcheck and names no listed
    person, else private. A page whose own text trips termcheck is private."""
    if name_terms is None:
        name_terms = names(fixture=sources.root is not None)
    cs = []
    for c in page["claims"]:
        sc = source_class(c["path"])
        if sc == "report":
            lines = sources.lines(c["path"])
            text = "\n".join(lines) if lines is not None else None
            sc = "system" if text is not None and not leaks(text) and not names_in(text, name_terms) else "private"
        if sc:
            cs.append(sc)
    for key in ("source", "found_by"):
        pc = provenance_class(page["fm"].get(key, ""))
        if pc:
            cs.append(pc)
    if leaks(page["text"]):
        cs.append("private")
    return most_restrictive(cs)


def _date_ok(s: str) -> bool:
    try:
        dt.date.fromisoformat(s)
        return True
    except (TypeError, ValueError):
        return False


def page_defects(page: dict, sources: Sources, name_terms) -> list:
    fm, out = page["fm"], []
    if not fm:
        return ["no frontmatter"]
    for k in REQUIRED:
        if not fm.get(k):
            out.append(f"frontmatter lacks {k}")
    stem = page["file"][:-3] if page["file"].endswith(".md") else page["file"]
    pid = fm.get("id", "")
    m = re.match(r"^kb-([a-z]+)-[a-z0-9]+(?:-[a-z0-9]+)*$", pid)
    if pid and not m:
        out.append("id must be kb-<class>-<slug> in lower case")
    if pid and pid != stem:
        out.append(f"id {pid} differs from the file name")
    if fm.get("kind") and fm["kind"] not in KINDS:
        out.append(f"kind {fm['kind']} is not one of {', '.join(sorted(KINDS))}")
    if fm.get("captured") and not _date_ok(fm["captured"]):
        out.append("captured is not a YYYY-MM-DD date")
    out += title_defects(fm.get("title", "")) if fm.get("title") else []

    # People. The file name, title and body are all checked; the name itself is never printed.
    if names_in(stem.replace("-", " "), name_terms):
        out.append("file name names a listed person")
    if names_in(fm.get("title", ""), name_terms):
        out.append("title names a listed person")
    for c in page["claims"]:
        if names_in(c["text"] + " " + (c["fragment"] or ""), name_terms):
            out.append(f"claim at line {c['line_no']} names a listed person")
    rest = re.sub(r"\[src: [^\]]*\]", "", page["body"])
    if names_in(rest, name_terms) and not any("names a listed person" in d for d in out):
        out.append("body names a listed person")
    scan = fm.get("title", "") + "\n" + page["body"]
    if SMALL_ROLE_RE.search(scan):
        out.append(f"role label for fewer than three people ({SMALL_ROLE_RE.search(scan).group(0)!r})")
    if SPEECH_RE.search(scan):
        out.append("speech attributed to a role; attribute it to the meeting")

    # Claims.
    for ln, line in page["bad_bullets"]:
        out.append(f"line {ln}: bullet is not a claim with a [src: ...] citation")
    if not page["claims"]:
        out.append("no claims")
    for c in page["claims"]:
        where = f"claim at line {c['line_no']}"
        lines = sources.lines(c["path"])
        why = excluded(c["path"], lines)
        if why:
            out.append(f"{where} cites an excluded source: {why}")
            continue
        if source_class(c["path"]) is None:
            out.append(f"{where} cites {c['path']}, which is not on the source allow-list")
            continue
        if not _date_ok(c["date"]):
            out.append(f"{where} source date {c['date']} is not a date")
        if lines is None:
            out.append(f"{where} source {c['path']} cannot be read")
            continue
        vol = volatility(c["path"])
        if c["pointer"]:
            continue
        if vol in POINTER_ONLY:
            out.append(f"{where} quotes a {vol} source; it may only be a pointer claim")
            continue
        if c["a"] is None:
            out.append(f"{where} has no line number")
            continue
        if c["b"] < c["a"] or c["b"] > len(lines):
            out.append(f"{where} line {c['a']}-{c['b']} is outside the source")
            continue
        tr = sources.tracked(c["path"])
        if tr is True and not c["commit"]:
            out.append(f"{where} cites a tracked file without @<commit>")
        if tr is False and not c["phash"]:
            out.append(f"{where} cites an untracked file without #p<hash8>")
        if not c["commit"] and not c["phash"] and tr is None:
            out.append(f"{where} carries neither @<commit> nor #p<hash8>")
        if c["phash"] and para_hash(lines, c["a"]) != c["phash"]:
            out.append(f"{where} paragraph hash #p{c['phash']} does not match the source")
        if not fragment_ok(lines, c["a"], c["b"], c["fragment"] or ""):
            out.append(f"{where} q: is not a whole source line within 3 lines of the cited line")

    # The id's class must agree with the class the sources give, so an id never looks more
    # open than the page is.
    if m and m.group(1) in RANK and not out:
        cls = page_class(page, sources, name_terms)
        if m.group(1) != cls:
            out.append(f"id says class {m.group(1)} but the sources make it {cls}")
    return out


def verify_files(files, sources: Sources, name_terms) -> list:
    """[(file name, defect)] for these pages, including duplicate ids and subjects across them."""
    out, subjects, ids = [], {}, {}
    for f in files:
        try:
            page = load_page(f)
        except (OSError, UnicodeDecodeError) as e:
            out.append((Path(f).name, f"unreadable: {e}"))
            continue
        for d in page_defects(page, sources, name_terms):
            out.append((page["file"], d))
        s, i = page["fm"].get("subject"), page["fm"].get("id")
        if s:
            subjects.setdefault(s, []).append(page["file"])
        if i:
            ids.setdefault(i, []).append(page["file"])
    for s, fs in sorted(subjects.items()):
        if len(fs) > 1:
            out += [(f, f"subject {s} is also used by {', '.join(x for x in fs if x != f)}") for f in fs]
    for i, fs in sorted(ids.items()):
        if len(fs) > 1:
            out += [(f, f"id {i} is also used by {', '.join(x for x in fs if x != f)}") for f in fs]
    return out


def page_files(d) -> list:
    return sorted(p for p in Path(d).glob("*.md") if not p.name.startswith("_"))


def verify_dir(d, source_root=None, name_terms=None) -> list:
    fx = fixture_sources_for(d)
    src = Sources(source_root if source_root is not None else fx)
    if name_terms is None:
        name_terms = names(fixture=fx is not None)
    return verify_files(page_files(d), src, name_terms)


# ---------------------------------------------------------------------------------------------
# Root validation, manifest, install

def validate_root(root) -> list:
    """Problems that make the bank NOT LIVE: a symlink anywhere on the path (below the temp
    anchor for a test root), or a page the manifest does not list."""
    root = Path(os.path.abspath(str(root)))
    out = []
    anchor = _under(root, _temp_anchors()) or _under(root, [str(STAGING)])
    if anchor is None:
        if _real(root) != str(root):
            out.append(f"{root} resolves elsewhere ({_real(root)})")
        for p in [root] + list(root.parents):
            if os.path.islink(str(p)):
                out.append(f"{p} is a symlink")
    else:
        ap = str(root) if str(root).startswith(anchor.rstrip(os.sep) + os.sep) else _real(root)
        rel = ap[len(anchor.rstrip(os.sep)) + 1:]
        cur = anchor
        for part in rel.split(os.sep):
            cur = os.path.join(cur, part)
            if os.path.islink(cur):
                out.append(f"{cur} is a symlink")
        if _real(root) != os.path.join(_real(anchor), rel):
            out.append(f"{root} resolves elsewhere ({_real(root)})")
    if root.is_dir():
        listed = set((read_manifest(root).get("pages") or {}).keys())
        for p in page_files(root):
            if p.name not in listed:
                out.append(f"{p.name} is in the bank but not in the manifest")
    return out


def read_manifest(root) -> dict:
    try:
        d = json.loads((Path(root) / MANIFEST).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def sha256_file(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def live_pages(root) -> tuple[list, list]:
    """(pages whose bytes match the manifest hash, [file names that do not]). A page whose
    hash does not match is ignored, never served."""
    root = Path(root)
    ok, bad = [], []
    for name, entry in sorted((read_manifest(root).get("pages") or {}).items()):
        p = root / name
        if not p.is_file():
            bad.append(name)
        elif sha256_file(p) != entry.get("sha256"):
            bad.append(name)
        else:
            ok.append(p)
    return ok, bad


def install_allowed(root=None) -> str | None:
    """None when this process may install into root, else why not. Outside a managed run anyone
    may. In one, only the knowledge-bank ship stage and a KB distil run may, except
    into a root under the temp dir: that is a test bank, never the live one or staging, and
    acceptance tests install fixtures there from a managed run."""
    rid = os.environ.get("LT_RUN_ID")
    if not rid:
        return None
    if root is not None and under_temp(root) and not os.path.islink(str(root)):
        return None
    if "/" in rid or rid.startswith("."):
        return f"LT_RUN_ID {rid!r} is not a run id"
    brief = runs_dir() / rid / "brief.md"
    try:
        text = brief.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return f"managed run {rid} has no readable brief.md, so it cannot show it may install"
    if re.search(r"^# Build `knowledge-bank`, stage `ship`", text, re.M):
        return None
    if re.search(r"^# KB distil \d{4}-\d{2}-\d{2}", text, re.M) and "<!-- kb-distil -->" in text:
        return None
    return f"managed run {rid} is neither the knowledge-bank ship stage nor a KB distil run"


def _set_class(text: str, cls: str) -> str:
    return re.sub(r"(?m)^class:[^\n]*$", f"class: {cls}", text, count=1)


def install(src_dir, root=None, source_root=None) -> tuple[int, list]:
    """(exit code, messages). 0 installed, 1 verify failed, 2 refused. Nothing is written
    unless every check passes first. source_root resolves cited paths under one folder, as
    `verify --sources` does; the committed fixture pages find their sources on their own."""
    msgs = []
    try:
        root = Path(root) if root is not None else root_path()
    except KBError as e:
        return 2, [f"refused: {e}"]
    why = install_allowed(root)
    if why:
        return 2, [f"refused: {why}"]
    src_dir = Path(src_dir)
    if not src_dir.is_dir():
        return 2, [f"refused: {src_dir} is not a folder"]
    if _real(src_dir) == _real(root):
        return 2, ["refused: --from is the bank itself"]
    if root.resolve() == LIVE_ROOT.resolve() or str(root) == str(LIVE_ROOT):
        if not (VAULT / "wiki").is_dir():
            return 2, [f"refused: {VAULT / 'wiki'} does not exist"]
    problems = validate_root(root)
    if (VAULT / "wiki").is_symlink() and str(root).startswith(str(VAULT)):
        problems.append(f"{VAULT / 'wiki'} is a symlink")
    if problems:
        return 2, [f"refused: {p}" for p in problems]
    fx = Path(source_root) if source_root is not None else fixture_sources_for(src_dir)
    sources = Sources(fx)
    name_terms = names(fixture=fx is not None)
    defects = verify_files(page_files(src_dir), sources, name_terms)
    if defects:
        return 1, [f"{f}: {d}" for f, d in defects]
    files = page_files(src_dir)
    if not files:
        return 1, ["no pages to install"]

    root.mkdir(parents=False, exist_ok=True)
    manifest = read_manifest(root)
    pages = dict(manifest.get("pages") or {})
    # Subjects must stay unique across the whole bank, not only across this batch.
    incoming = {load_page(f)["fm"].get("subject"): f.name for f in files}
    for name, e in pages.items():
        s = e.get("subject")
        if s in incoming and incoming[s] != name:
            return 1, [f"{incoming[s]}: subject {s} is already used by {name} in the bank"]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    for f in files:
        page = load_page(f)
        cls = page_class(page, sources, name_terms)
        text = page["text"]
        if page["fm"].get("class") != cls:
            text = _set_class(text, cls)
            msgs.append(f"{f.name}: class rewritten to {cls}")
        dest = root / f.name
        if dest.exists():
            old = dest.read_bytes()
            if old == text.encode("utf-8"):
                pages[f.name] = {"sha256": sha256_file(dest), "id": page["fm"]["id"], "class": cls,
                                 "subject": page["fm"]["subject"]}
                continue
            hist = root / HISTORY
            hist.mkdir(exist_ok=True)
            target, n = hist / f"{dest.stem}.{stamp}.md.prev", 2
            while target.exists():
                target, n = hist / f"{dest.stem}.{stamp}-{n}.md.prev", n + 1
            atomicio.write_bytes(target, old)
            msgs.append(f"{f.name}: previous version kept as {HISTORY}/{target.name}")
        atomicio.write_text(dest, text)
        pages[f.name] = {"sha256": sha256_file(dest), "id": page["fm"]["id"], "class": cls,
                         "subject": page["fm"]["subject"]}
        msgs.append(f"{f.name}: installed ({cls})")
    manifest = {"schema": 1, "written_by": "knowledge.py install",
                "installed": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "pages": dict(sorted(pages.items()))}
    # A test or staging root whose pages cite a folder of sources (the fixtures, a seed with
    # --sources) records that folder, so scan verifies against the same text. Never for the
    # live bank, whose sources are always the real files.
    if fx is not None and _real(root) != _real(LIVE_ROOT):
        manifest["source_root"] = str(Path(fx).resolve())
    atomicio.write_text(root / MANIFEST, json.dumps(manifest, indent=1) + "\n")
    return 0, msgs


# ---------------------------------------------------------------------------------------------
# Scan: verify every claim once, then build the index
#
# All verification happens here, never at query time: each claim gets `valid` and
# `valid_until`, and a page is live when its bytes match the manifest, it passes the page-level
# rules (names, title, roles) and at least one of its claims is valid. The reader only reads.

STOP_WORDS = set("""
a about above after again against all also am an and any are as at be because been before being
below between both but by can could did do does doing done down during each either else etc few
for from further get gets got had has have having he her here hers him his how however i if in
into is it its itself just let may me might more most much must my no nor not now of off on once
one only or other others our ours out over own per same she should so some such than that the
their theirs them then there these they this those through thus to too under until up upon us
use used using very via was we were what when where whether which while who whom whose why will
with within without would yes yet you your yours
""".split())
FTS_WEIGHTS = (3.0, 2.0, 1.0)          # title, subject, claims (fixed before relevance was labelled)
QUERY_CAP = 60
TOP_K = 3
STATE_LIFE = dt.timedelta(hours=24)


def index_location_problem(p) -> str | None:
    """Why the index may not live at p, or None. It must stay outside the repo, the main
    checkout and ~/vault, so neither git, Obsidian Sync nor the backup mirror carries it
   ."""
    rp = _real(p)
    for place in (REPO, MAIN_CHECKOUT, VAULT):
        rq = _real(place)
        if rp == rq or rp.startswith(rq.rstrip(os.sep) + os.sep):
            return f"{p} lies inside {place}"
    return None


def _terms(text: str) -> list:
    """Lower-cased word tokens, stop words and one-character tokens removed, in order."""
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(t) > 1 and t not in STOP_WORDS]


def query_terms(text: str, cap: int = QUERY_CAP) -> list:
    """The first `cap` distinct terms of the text."""
    out, seen = [], set()
    for t in _terms(text):
        if t not in seen:
            seen.add(t)
            out.append(t)
            if len(out) >= cap:
                break
    return out


def fts_query(terms) -> str:
    """Terms joined with OR for FTS5, each quoted so no term is read as an operator."""
    return " OR ".join('"' + t.replace('"', '') + '"' for t in terms if t)


def _fts5_available() -> bool:
    if os.environ.get("KB_NO_FTS5") == "1":      # the selftest's switch for the fallback path
        return False
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
        c.close()
        return True
    except sqlite3.Error:
        return False


def _source_date(sources: Sources, logical: str) -> str | None:
    """A source's own date: a `captured:` or `date:` line in its first 30 lines,
    else a YYYY-MM-DD in its file name, else its modification date. Used only to say a source
    is newer than the pages citing it."""
    for line in (sources.lines(logical) or [])[:30]:
        m = re.match(r"^(?:captured|date):\s*[\"']?(\d{4}-\d{2}-\d{2})", line)
        if m:
            return m.group(1)
    m = re.search(r"(\d{4}-\d{2}-\d{2})", Path(logical).name)
    if m and _date_ok(m.group(1)):
        return m.group(1)
    if sources.tracked(logical):
        d = sources.commit_date(logical)
        if d:
            return d
    try:
        st = sources.resolve(logical).stat()
    except OSError:
        return None
    return dt.datetime.fromtimestamp(st.st_mtime).date().isoformat()


def claim_check(c: dict, sources: Sources, now: dt.datetime) -> tuple:
    """(valid 0 or 1, valid_until ISO or None, why) for one claim, per class:
    tracked files against one cached `git show main:<path>`, untracked files by paragraph hash,
    vault notes outside the repo by the file existing and the fragment being present. State
    claims live 24 h from 00:00 UTC of their source date."""
    path = c["path"]
    vol = volatility(path)
    lines = sources.lines(path)
    why = excluded(path, lines)
    if why:
        return 0, None, f"excluded source: {why}"
    if source_class(path) is None:
        return 0, None, "source is not on the allow-list"
    if c["pointer"]:
        return (1, None, "pointer") if lines is not None else (0, None, "source cannot be read")
    if vol in POINTER_ONLY:
        return 0, None, f"a {vol} source may only be pointed at"
    if c["a"] is None:
        return 0, None, "no line number"
    if c["commit"] and sources.root is None:
        lines = sources.lines_main(path)
        if lines is None:
            return 0, None, "not on main"
    elif c["phash"]:
        if lines is None or para_hash(lines, c["a"]) != c["phash"]:
            return 0, None, "paragraph hash no longer matches"
    elif sources.tracked(path) is not None:
        return 0, None, "neither @<commit> nor #p<hash8>"
    if lines is None:
        return 0, None, "source cannot be read"
    if c["b"] < c["a"] or c["b"] > len(lines) or not fragment_ok(lines, c["a"], c["b"], c["fragment"] or ""):
        return 0, None, "fragment is no longer a whole line near the cited line"
    if vol == "state":
        if not _date_ok(c["date"]):
            return 0, None, "undated state claim"
        until = dt.datetime.combine(dt.date.fromisoformat(c["date"]), dt.time(0), dt.timezone.utc) + STATE_LIFE
        vu = until.strftime("%Y-%m-%dT%H:%M:%SZ")
        if now >= until:
            return 0, vu, "state claim expired"
        return 1, vu, "ok"
    return 1, None, "ok"


def _page_gate(page: dict, sources: Sources, name_terms) -> list:
    """Page-level defects that take a whole page out of service at scan: everything verify
    says except claim-level drift, which scan records per claim instead. A claim that names a
    listed person still takes the page out."""
    return [d for d in page_defects(page, sources, name_terms)
            if not d.startswith("claim at line") or "names a listed person" in d]


def _scan_sources(root: Path, manifest: dict, source_root=None) -> Sources:
    sr = source_root
    if sr is None and _real(root) != _real(LIVE_ROOT) and manifest.get("source_root"):
        sr = manifest["source_root"]
    return Sources(Path(sr)) if sr else Sources()


SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE pages(n INTEGER PRIMARY KEY, id TEXT, file TEXT, path TEXT, title TEXT, subject TEXT,
                   kind TEXT, class TEXT, captured TEXT, sha_ok INTEGER, live INTEGER, why TEXT);
CREATE TABLE claims(page_id TEXT, text TEXT, path TEXT, line INTEGER, line_end INTEGER, date TEXT,
                    volatility TEXT, fragment TEXT, fidelity TEXT, pointer INTEGER, valid INTEGER,
                    valid_until TEXT, why TEXT);
"""


def _truncate(p: Path) -> None:
    """Empty a file in place (creating it if missing). Never a delete: code here never deletes
    files, and truncation leaves nothing for SQLite to mistake for a hot journal."""
    with open(str(p), "wb"):
        pass


def scan(root=None, index=None, source_root=None, now=None, emit=print) -> int:
    """Build the index. 0 built (or another scan holds the lock), 1 NOT LIVE, 2 refused."""
    try:
        root = Path(root) if root is not None else root_path()
    except KBError as e:
        emit(f"scan: refused: {e}")
        return 2
    idx = Path(index) if index is not None else index_path()
    if not under_temp(idx) and _real(os.getcwd()) != _real(MAIN_CHECKOUT):
        emit(f"scan: refused: the live index is written only from {MAIN_CHECKOUT} "
             f"(cwd is {os.getcwd()}); set KB_INDEX under the temp dir to test")
        return 2
    why = index_location_problem(idx)
    if why:
        emit(f"scan: refused: {why}")
        return 2
    if not root.is_dir():
        emit(f"Knowledge bank: NOT LIVE ({root} does not exist (install pending))")
        return 1
    problems = validate_root(root)
    if problems:
        emit(f"Knowledge bank: NOT LIVE ({problems[0]})")
        return 1
    manifest = read_manifest(root)
    if not manifest:
        emit(f"Knowledge bank: NOT LIVE (no {MANIFEST} in {root})")
        return 1
    idx.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(idx.with_suffix(".lock")), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            emit("scan already running")
            return 0
        return _scan_locked(root, idx, manifest, source_root, now, emit)
    finally:
        os.close(fd)


def _scan_locked(root: Path, idx: Path, manifest: dict, source_root, now, emit) -> int:
    now = now or dt.datetime.now(dt.timezone.utc)
    sources = _scan_sources(root, manifest, source_root)
    name_terms = names(fixture=sources.root is not None)
    building = Path(str(idx) + ".tmp")
    _truncate(building)
    journal = Path(str(building) + "-journal")
    if journal.exists():
        _truncate(journal)
    engine = "fts5" if _fts5_available() else "tokens"

    conn = sqlite3.connect(str(building))
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript(SCHEMA)
    if engine == "fts5":
        conn.execute("CREATE VIRTUAL TABLE pages_fts USING fts5(title, subject, claims, "
                     "tokenize='porter unicode61')")
    else:
        conn.execute("CREATE TABLE pages_tok(n INTEGER, field INTEGER, term TEXT, tf INTEGER)")
        conn.execute("CREATE INDEX pages_tok_term ON pages_tok(term)")

    entries = manifest.get("pages") or {}
    ok_files, bad = live_pages(root)
    ok_names = {p.name for p in ok_files}
    cited, drifted = {}, set()
    counts = {"pages": 0, "live": 0, "claims": 0, "valid": 0, "hash_mismatch": len(bad)}
    for n, (name, entry) in enumerate(sorted(entries.items()), 1):
        counts["pages"] += 1
        path = root / name
        if name not in ok_names:
            conn.execute("INSERT INTO pages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (n, entry.get("id", ""), name, str(path), "", entry.get("subject", ""), "",
                          entry.get("class", "private"), "", 0, 0, "bytes differ from the manifest hash"))
            continue
        page = load_page(path)
        fm = page["fm"]
        gate = _page_gate(page, sources, name_terms)
        cls = most_restrictive([entry.get("class", "private"), fm.get("class", "private"),
                                page_class(page, sources, name_terms)])
        checked = [claim_check(c, sources, now) for c in page["claims"]]
        newest_proc = max([c["date"] for c, r in zip(page["claims"], checked)
                           if r[0] and volatility(c["path"]) == "procedure"] or [""])
        for i, c in enumerate(page["claims"]):
            if checked[i][0] and volatility(c["path"]) == "state" and newest_proc > c["date"]:
                checked[i] = (0, checked[i][1], "a newer procedure claim on this subject supersedes it")
        live = int(not gate and any(r[0] for r in checked))
        why = gate[0] if gate else ("ok" if live else "no valid claim")
        conn.execute("INSERT INTO pages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (n, fm.get("id", ""), name, str(path), fm.get("title", ""), fm.get("subject", ""),
                      fm.get("kind", ""), cls, fm.get("captured", ""), 1, live, why))
        for c, (valid, vu, cwhy) in zip(page["claims"], checked):
            conn.execute("INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (fm.get("id", ""), c["text"], c["path"], c["a"], c["b"], c["date"],
                          volatility(c["path"]), c["fragment"], c["fidelity"], int(c["pointer"]),
                          valid, vu, cwhy))
            counts["claims"] += 1
            counts["valid"] += valid
            cap = fm.get("captured", "")
            cited[c["path"]] = max(cited.get(c["path"], ""), cap)
            if cwhy in ("paragraph hash no longer matches", "not on main",
                        "fragment is no longer a whole line near the cited line"):
                drifted.add(c["path"])
        counts["live"] += live
        bottom = " ".join(x for x in page["body"].split("\n") if x.startswith("**"))
        body = " ".join([bottom] + [c["text"] + " " + (c["fragment"] or "") for c in page["claims"]])
        fields = (fm.get("title", ""), fm.get("subject", "").replace("-", " "), body)
        if engine == "fts5":
            conn.execute("INSERT INTO pages_fts(rowid, title, subject, claims) VALUES (?,?,?,?)", (n,) + fields)
        else:
            for fi, text in enumerate(fields):
                tf = {}
                for t in _terms(text):
                    tf[t] = tf.get(t, 0) + 1
                conn.executemany("INSERT INTO pages_tok VALUES (?,?,?,?)",
                                 [(n, fi, t, k) for t, k in tf.items()])
    conn.executemany("INSERT INTO meta VALUES (?,?)", [
        ("engine", engine), ("built", now.strftime("%Y-%m-%dT%H:%M:%SZ")), ("root", str(root)),
        ("schema", "1"), ("pages", str(counts["pages"]))])
    conn.commit()
    ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
    rows = conn.execute("SELECT count(*) FROM pages").fetchone()[0]
    ft = (conn.execute("SELECT count(*) FROM pages_fts").fetchone()[0] if engine == "fts5"
          else conn.execute("SELECT count(DISTINCT n) FROM pages_tok").fetchone()[0])
    conn.close()
    want_ft = len(ok_names & set(entries))
    if ic != "ok" or rows != len(entries) or (engine == "fts5" and ft != want_ft) or ft > want_ft:
        emit(f"scan: the new index failed its checks (integrity {ic}, {rows} page rows for "
             f"{len(entries)} manifest entries, {ft} text rows); the old index is kept")
        emit("kb scan exit=1")
        return 1
    os.replace(str(building), str(idx))

    stale = stale_sources(sources, cited, drifted)
    atomicio.write_text(idx.parent / "kb-stale.json", json.dumps(
        {"written": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "sources": stale}, indent=1) + "\n")
    for name in bad:
        emit(f"{name}: ignored, its bytes differ from the manifest hash")
    emit(f"scan: {counts['pages']} pages, {counts['live']} live, {counts['valid']} of "
         f"{counts['claims']} claims valid, engine {engine}, {len(stale)} stale sources")
    emit("kb scan exit=0")
    return 0


DICTATION_DIRS = (("_inbox", "candidate-dictation-*.md"), ("~/vault/raw/inbox/dictations", "*.md"))


def stale_sources(sources: Sources, cited: dict, drifted=()) -> list:
    """For distil-queue: sources dated after every page citing them was written,
    sources whose cited text has changed, and dictation files no page cites yet. Excluded
    sources never appear."""
    out = []
    for logical, newest_page in sorted(cited.items()):
        if excluded(logical):
            continue
        d = _source_date(sources, logical)
        if logical in drifted:
            out.append({"path": logical, "source_date": d, "newest_page": newest_page,
                        "why": "the cited text has changed"})
        elif d and newest_page and d > newest_page:
            out.append({"path": logical, "source_date": d, "newest_page": newest_page,
                        "why": "dated after every page citing it"})
    for folder, pattern in DICTATION_DIRS:
        base = sources.resolve(folder)
        for f in sorted(base.glob(pattern)) if base.is_dir() else []:
            logical = f"{folder}/{f.name}"
            if logical in cited or excluded(logical):
                continue
            out.append({"path": logical, "source_date": _source_date(sources, logical),
                        "newest_page": None, "why": "dictation cited by no page"})
    return out


# ---------------------------------------------------------------------------------------------
# The reader, the audiences and the query. kb_hook.py imports open_index_ro(),
# query_for_entry() and top_pages().

# Which page classes each audience may reach through `ask`. Anything not named here
# (club, career, outward, missing, unknown) gets OPEN_CLASSES.
OPEN_CLASSES = ("system", "project", "course")
ASK_CLASSES = {
    "private": tuple(CLASSES),
    "coursework": ("course",),
    "open": OPEN_CLASSES,
}
AUDIENCES = ("private", "coursework", "open", "club", "career", "outward")


def ask_classes(audience: str) -> tuple:
    return ASK_CLASSES.get(audience, OPEN_CLASSES)


def open_index_ro(path=None) -> sqlite3.Connection:
    """The index, read-only. A missing or empty index is an error (NOT LIVE), never a new
    0-byte file: `mode=ro` cannot create one, and the file is checked before connecting."""
    p = Path(path) if path is not None else index_path()
    try:
        size = p.stat().st_size
    except OSError:
        raise KBError(f"index {p} does not exist (no scan yet)")
    if size == 0:
        raise KBError(f"index {p} is empty")
    uri = "file:" + urllib.parse.quote(str(p.resolve())) + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=0.5)
        conn.execute("SELECT value FROM meta WHERE key='engine'").fetchone()
    except sqlite3.Error as e:
        raise KBError(f"index {p} cannot be read ({e})")
    return conn


def _engine(conn) -> str:
    r = conn.execute("SELECT value FROM meta WHERE key='engine'").fetchone()
    return r[0] if r else "fts5"


def _ranked(conn, terms) -> list:
    """[(page n, score)] best first, lower score is better (bm25 convention)."""
    if not terms:
        return []
    if _engine(conn) == "fts5":
        w = FTS_WEIGHTS
        rows = conn.execute(
            f"SELECT rowid, bm25(pages_fts, {w[0]}, {w[1]}, {w[2]}) AS s FROM pages_fts "
            "WHERE pages_fts MATCH ? ORDER BY s", (fts_query(terms),)).fetchall()
        return [(r[0], r[1]) for r in rows]
    # Token-table fallback: the same field weights, BM25-shaped term saturation and idf.
    total = conn.execute("SELECT count(DISTINCT n) FROM pages_tok").fetchone()[0] or 1
    scores = {}
    for t in terms:
        rows = conn.execute("SELECT n, field, tf FROM pages_tok WHERE term=?", (t,)).fetchall()
        df = len(set(r[0] for r in rows))
        if not df:
            continue
        idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
        for n, field, tf in rows:
            scores[n] = scores.get(n, 0.0) - FTS_WEIGHTS[field] * idf * (tf * 2.2) / (tf + 1.2)
    return sorted(scores.items(), key=lambda x: (x[1], x[0]))


def top_pages(conn, query, audience: str = "open", k: int = TOP_K, fragments: bool = False) -> list:
    """The k best live pages the audience may reach, best first. `query` is text or a list of
    terms. Each result: id, title, path, class, kind, score, and with fragments=True the valid
    non-pointer claims as whole source lines."""
    terms = query if isinstance(query, list) else query_terms(query)
    allowed = set(ask_classes(audience))
    out = []
    for n, score in _ranked(conn, terms):
        r = conn.execute("SELECT id, title, path, class, kind, file FROM pages WHERE n=? AND live=1 "
                         "AND sha_ok=1", (n,)).fetchone()
        if not r or r[3] not in allowed:
            continue
        item = {"id": r[0], "title": r[1], "path": r[2], "class": r[3], "kind": r[4],
                "file": r[5], "score": round(score, 4)}
        if fragments:
            item["fragments"] = [
                {"claim": c[0], "q": c[1], "src": f"{c[2]}:{c[3]}" + (f"-{c[4]}" if c[4] != c[3] else ""),
                 "date": c[5], "fidelity": c[6]}
                for c in conn.execute(
                    "SELECT text, fragment, path, line, line_end, date, fidelity FROM claims "
                    "WHERE page_id=? AND valid=1 AND pointer=0 ORDER BY rowid", (r[0],))]
        out.append(item)
        if len(out) >= k:
            break
    return out


def margin(results: list) -> float | None:
    """How far the first result leads the third (bm25 units), logged by the hook; no floor."""
    if len(results) < 2:
        return None
    return round(results[-1]["score"] - results[0]["score"], 4)


# The queue entry titles a /build pipeline writes for its stages: `Build <id> <slug>: <name>`,
# and `Build <id> retry<N> <slug>: <name>` for a retry.
BUILD_TITLE_RE = re.compile(r"^Build (?P<stage>[a-z0-9-]+)(?: retry\d+)? (?P<slug>[a-z0-9][a-z0-9-]*): ")


def _entry_title_body(entry_text: str) -> tuple:
    fm, body, _ = parse_frontmatter(entry_text)
    return fm.get("title", ""), body


def _after_provenance(body: str) -> str:
    """The brief body without its leading provenance paragraphs (the `**Repo:` line, `Who
    queued this`, and the standing-order paragraphs that start with a bold label)."""
    paras = re.split(r"\n\s*\n", body.strip())
    i = 0
    while i < len(paras) and (paras[i].lstrip().startswith("**") or "queued this" in paras[i][:200]):
        i += 1
    return "\n\n".join(paras[i:]) if i < len(paras) else body


def builds_dir() -> Path:
    return _env_temp_path("KB_BUILDS_DIR") or MAIN_CHECKOUT / "_meta" / "builds"


def query_for_entry(entry_path, body_text: str | None = None) -> dict:
    """The pinned query for a queue entry. For a /build stage, found by the
    entry's title (never a temp path): the pipeline's request and deliverable, the stage id,
    and the brief's `## This stage:` heading line. Otherwise the entry title and the first 200
    words after the provenance paragraphs. Returns {"kind", "text", "terms"}."""
    entry_text = Path(entry_path).read_text(encoding="utf-8", errors="replace")
    title, ebody = _entry_title_body(entry_text)
    body = body_text if body_text is not None else ebody
    m = BUILD_TITLE_RE.match(title)
    if m:
        pj = builds_dir() / m.group("slug") / "pipeline.json"
        try:
            p = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            p = None
        if isinstance(p, dict):
            heading = next((x for x in body.split("\n") if x.startswith("## This stage:")), "")
            text = "\n".join([str(p.get("request", "")), str(p.get("deliverable", "")),
                              m.group("stage"), heading[len("## "):]])
            return {"kind": "build", "slug": m.group("slug"), "stage": m.group("stage"),
                    "text": text, "terms": query_terms(text)}
    words = _after_provenance(body).split()[:200]
    text = title + "\n" + " ".join(words)
    return {"kind": "entry", "text": text, "terms": query_terms(text)}


def audience_dir() -> Path:
    return _env_temp_path("KB_AUDIENCE_DIR") or STATE / "kb-audience"


def coursework_run(rid: str) -> bool:
    """A coursework-like managed run: one launched with the role `coursework` (LT_ROLE) or with
    a run id starting `coursework-`. Such a run may be launched directly by an agent runner,
    with no hook sidecar, so it is recognised by role or id and gets course pages only."""
    return os.environ.get("LT_ROLE") == "coursework" or rid.startswith("coursework-")


def managed_audience() -> str | None:
    """Under a managed run, the audience the hook recorded for it, else `outward`. A
    coursework-like run is `coursework` (course pages only) whatever its sidecar says. None
    outside a managed run."""
    rid = os.environ.get("LT_RUN_ID")
    if not rid:
        return None
    if coursework_run(rid):
        return "coursework"
    if "/" in rid or rid.startswith("."):
        return "outward"
    try:
        a = (audience_dir() / rid).read_text(encoding="utf-8").strip()
    except OSError:
        return "outward"
    return a if a in AUDIENCES else "outward"


def ask(text: str, audience: str | None = None, k: int = TOP_K, index=None, root=None) -> tuple:
    """(exit code, result dict). 0 answered, 1 NOT LIVE, 2 refused. Under a managed run the
    audience comes from the run's sidecar and a wider --audience is refused. A query that
    holds a listed person's name is refused, and the name is never printed."""
    ceiling = managed_audience()
    if ceiling is not None:
        if audience is None:
            audience = ceiling
        elif not set(ask_classes(audience)) <= set(ask_classes(ceiling)):
            return 2, {"refused": f"audience {audience} is wider than this run's {ceiling}"}
    audience = audience or "open"
    if names_in(text, names()):
        return 2, {"refused": "the question names a listed person; the bank holds no profiles of people"}
    try:
        root = Path(root) if root is not None else root_path()
    except KBError as e:
        return 2, {"refused": str(e)}
    problems = validate_root(root) if root.is_dir() else [f"{root} does not exist (install pending)"]
    if problems:
        return 1, {"not_live": problems[0]}
    try:
        conn = open_index_ro(index)
    except KBError as e:
        return 1, {"not_live": str(e)}
    try:
        terms = query_terms(text)
        pages = top_pages(conn, terms, audience, k, fragments=True)
        engine = _engine(conn)
    finally:
        conn.close()
    # A page edited since the scan is not served: its bytes must still match the manifest.
    listed = read_manifest(root).get("pages") or {}
    pages = [p for p in pages if Path(p["path"]).is_file()
             and sha256_file(p["path"]) == (listed.get(p["file"]) or {}).get("sha256")]
    return 0, {"audience": audience, "engine": engine, "terms": terms, "pages": pages,
               "margin": margin(pages)}


def _ask_text(res: dict) -> str:
    out = [f"Knowledge bank, audience {res['audience']}: {len(res['pages'])} page(s)."]
    for p in res["pages"]:
        out.append(f"\n{p['id']} | {p['title']} | {p['path']} ({p['class']})")
        for f in p.get("fragments", []):
            out.append(f"  - {f['claim']}\n    \"{f['q']}\" [{f['src']} | {f['date']} | {f['fidelity']}]")
    if not res["pages"]:
        out.append("Nothing relevant the audience may see. Read the sources directly.")
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------
# Status

def status(root=None) -> dict:
    try:
        root = Path(root) if root is not None else root_path()
    except KBError as e:
        return {"live": False, "reason": str(e)}
    out = {"root": str(root), "live": False}
    if not root.is_dir():
        out["reason"] = f"{root} does not exist (install pending)"
        return out
    problems = validate_root(root)
    if problems:
        out["reason"] = problems[0]
        out["problems"] = problems
        return out
    manifest = read_manifest(root)
    if not manifest:
        out["reason"] = f"no {MANIFEST} in {root}"
        return out
    ok, bad = live_pages(root)
    by_class, newest = {}, ""
    entries = manifest.get("pages") or {}
    for p in ok:
        c = entries.get(p.name, {}).get("class", "?")
        by_class[c] = by_class.get(c, 0) + 1
        for cl in load_page(p)["claims"]:
            newest = max(newest, cl["date"])
    idx = index_path()
    out.update({
        "live": bool(ok), "pages": len(ok), "by_class": by_class, "hash_mismatch": bad,
        "newest_source": newest or None, "installed": manifest.get("installed"),
        "last_scan": (dt.datetime.fromtimestamp(idx.stat().st_mtime, dt.timezone.utc)
                      .strftime("%Y-%m-%dT%H:%M:%SZ") if idx.exists() else None),
    })
    if not ok:
        out["reason"] = "no page matches its manifest hash"
    return out


def _status_line(s: dict) -> str:
    if not s.get("live"):
        return f"Knowledge bank: NOT LIVE ({s.get('reason', 'unknown')})"
    classes = ", ".join(f"{c} {n}" for c, n in sorted(s["by_class"].items(), key=lambda x: RANK.get(x[0], 99)))
    extra = f", {len(s['hash_mismatch'])} ignored (hash mismatch)" if s["hash_mismatch"] else ""
    return (f"Knowledge bank: {s['pages']} pages ({classes}){extra}, newest source "
            f"{s['newest_source']}, installed {s['installed']}, last scan {s['last_scan'] or 'never'}")


KB_OUTCOMES = ("injected", "empty", "wall-dropped", "failed")   # "off" counts as other
KB_SCAN_MAX_H = 48


def kb_line(now: dt.datetime | None = None, hours: int = 24) -> str:
    """One counted line for a morning brief or a dashboard. It says NOT LIVE with the reason
    when the root fails validation, the index is missing or unreadable, or the last scan is
    over 48 h old, and always adds the launch-hook outcomes of the last `hours`, so a broken
    retriever reads as failed launches, never as "nothing relevant"."""
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    try:
        import kb_hook  # noqa: E402
    except Exception as e:  # noqa: BLE001
        return f"Knowledge bank: NOT LIVE (its code does not import: {type(e).__name__}: {e})"
    counts = {o: 0 for o in KB_OUTCOMES}
    other = 0
    try:
        with open(kb_hook.log_path(), encoding="utf-8") as f:
            for raw in f:
                try:
                    r = json.loads(raw)
                    t = dt.datetime.strptime(r["t"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
                except (ValueError, KeyError, TypeError):
                    continue
                if now - t > dt.timedelta(hours=hours) or t > now:
                    continue
                if r.get("outcome") in counts:
                    counts[r["outcome"]] += 1
                else:
                    other += 1
    except OSError:
        pass
    tail = (f"; launches in {hours}h: " + ", ".join(f"{o} {n}" for o, n in counts.items())
            + (f", other {other}" if other else ""))
    try:
        st = status()
    except Exception as e:  # noqa: BLE001
        return f"Knowledge bank: NOT LIVE (status failed: {type(e).__name__}: {e}){tail}"
    if not st.get("live"):
        return f"Knowledge bank: NOT LIVE ({st.get('reason', 'unknown')}){tail}"
    try:
        conn = open_index_ro()
        try:
            conn.execute("SELECT count(*) FROM pages").fetchone()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        return f"Knowledge bank: NOT LIVE ({e}){tail}"
    last = st.get("last_scan")
    try:
        age_h = (now - dt.datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ")
                 .replace(tzinfo=dt.timezone.utc)).total_seconds() / 3600
    except (TypeError, ValueError):
        age_h = None
    if age_h is None or age_h > KB_SCAN_MAX_H:
        what = "never" if age_h is None else f"{int(age_h)} h ago"
        return f"Knowledge bank: NOT LIVE (last scan {what}, over {KB_SCAN_MAX_H} h){tail}"
    distils = sorted(p.name for p in runs_dir().glob("*kb-distil*")) if runs_dir().is_dir() else []
    last_distil = distils[-1][:15] if distils else "never"
    return (f"Knowledge bank: {st['pages']} pages, newest source {st.get('newest_source') or 'none'}, "
            f"last scan {last}, last distil {last_distil}{tail}")


# ---------------------------------------------------------------------------------------------
# The dictation reader, pointer pages, the distiller's queue, purge

ASK_KINDS = ("note", "task", "work", "update", "reminder")
DICTATION_SKIP = ("triaged-noise", "triaged-duplicate")
LOC_UNSAFE = re.compile(r"[:@#|\]\[\n]")


def _fm_of(sources: Sources, logical: str) -> dict:
    lines = sources.lines(logical)
    return parse_frontmatter("\n".join(lines))[0] if lines is not None else {}


def read_dictations(sources: Sources | None = None, skipped: dict | None = None) -> list:
    """The owner's dictations the bank may point at: the intake candidates in `_inbox/` and
    the raw dictation notes in `~/vault/raw/inbox/dictations/`. A raw note that names its
    candidate (`candidate:`) is the same dictation, so the candidate stands for both and a raw
    note with no `ask:` of its own takes its candidate's. Skipped: `triaged-noise`,
    `triaged-duplicate`, and anything with no `ask:` or an ask outside ASK_KINDS. Reads files
    only, so it runs where the Wispr MCP does not (every managed run)."""
    sources = sources or Sources()
    skipped = skipped if skipped is not None else {}
    items, cands = [], {}
    base = sources.resolve("_inbox")
    for f in sorted(base.glob("candidate-dictation-*.md")) if base.is_dir() else []:
        logical = f"_inbox/{f.name}"
        cands[f.name] = _fm_of(sources, logical)
        items.append((logical, cands[f.name]))
    raw = sources.resolve("~/vault/raw/inbox/dictations")
    for f in sorted(raw.glob("*.md")) if raw.is_dir() else []:
        logical = f"~/vault/raw/inbox/dictations/{f.name}"
        fm = _fm_of(sources, logical)
        cand = Path(fm.get("candidate", "")).name
        if cand and cand in cands:
            skipped["carried by its candidate"] = skipped.get("carried by its candidate", 0) + 1
            continue
        items.append((logical, fm))
    out = []
    for logical, fm in items:
        why = None
        if excluded(logical):
            why = "excluded"
        elif fm.get("status", "") in DICTATION_SKIP:
            why = fm["status"]
        elif not fm.get("ask"):
            why = "no ask"
        elif fm["ask"].strip().lower() not in ASK_KINDS:
            why = f"ask {fm['ask'].strip().lower()}"
        elif LOC_UNSAFE.search(logical):
            why = "path cannot be cited"
        if why:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        out.append({"path": logical, "ask": fm["ask"].strip().lower(), "status": fm.get("status", ""),
                    "title": fm.get("title", ""), "date": _source_date(sources, logical)})
    return out


def _clean(s: str) -> str:
    """One line of plain text a claim or title may carry: no brackets, bars, quotes, backticks,
    markdown emphasis or fence markers."""
    s = re.sub(r"[\[\]|\"`*_]", " ", s or "")
    s = s.replace("===", " ")
    return " ".join(s.split()).strip(" -:;,.")


def _title12(s: str) -> str:
    words = _clean(s).split()
    return " ".join(words[:12])


def _safe(text: str, name_terms) -> bool:
    """Text a pointer page may carry about a source: no listed name, no role label for one or
    two people, no speech put in a role's mouth, no personal-pipeline term."""
    return bool(text) and not (names_in(text, name_terms) or SMALL_ROLE_RE.search(text)
                               or SPEECH_RE.search(text) or leaks(text))


def _heading_of(lines: list, fm: dict) -> str:
    if fm.get("title"):
        return fm["title"]
    for ln in lines[:80]:
        if ln.startswith("# "):
            return ln[2:]
    return ""


def _sections(lines: list, cap: int = 15) -> list:
    """(1-based line, heading text) for the `## ` headings of a note, at most cap of them."""
    out, fence = [], False
    for i, ln in enumerate(lines, 1):
        if ln.startswith("```"):
            fence = not fence
        elif not fence and ln.startswith("## "):
            out.append((i, ln[3:]))
            if len(out) >= cap:
                break
    return out


def _md_files(sources: Sources, folder: str, recursive: bool = False) -> list:
    base = sources.resolve(folder)
    if not base.is_dir():
        return []
    found = base.rglob("*.md") if recursive else base.glob("*.md")
    out = []
    for f in sorted(found):
        rel = f.relative_to(base).as_posix()
        if any(part.startswith((".", "_")) for part in rel.split("/")) or not f.is_file():
            continue
        out.append(f"{folder}/{rel}")
    return out


def _spec(slug, title, subject, bottom, claims) -> dict:
    return {"slug": slug, "title": _title12(title), "subject": subject, "bottom": bottom, "claims": claims}


def pointer_specs(sources: Sources, name_terms) -> list:
    """The mechanical pointer pages, as specs: goals, one page per course plus
    one per exam-pack note, one per club minutes file (meeting level, no names), one per
    dictation ask, and interview prep. Claims are pointers: the page says where to read and
    carries no quoted text. Text taken from a source (a title, a section heading) is kept only
    when `_safe`; otherwise the claim falls back to a neutral description."""
    specs = []

    def claim(path, text, fallback, line=None):
        if (LOC_UNSAFE.search(path) or excluded(path) or source_class(path) is None
                or names_in(path.replace("-", " ").replace("_", " "), name_terms)):
            return None
        lines = sources.lines(path)
        if lines is None:
            return None
        date = _source_date(sources, path)
        if not date:
            return None
        text = _clean(text)
        if not _safe(text, name_terms):
            text = fallback
        return {"text": text, "path": path, "line": line, "date": date}

    def doc_claims(path, label, fallback, sections=True):
        lines = sources.lines(path) or []
        fm = parse_frontmatter("\n".join(lines))[0]
        out = [claim(path, f"{label}: {_heading_of(lines, fm)}", fallback)]
        if sections:
            for n, h in _sections(lines):
                out.append(claim(path, f"Section: {h}", None, n))
        return [c for c in out if c and c["text"]]

    # Goals: one page per _meta/goals/*.md. Decision sources, so pointers only.
    for path in _md_files(sources, "_meta/goals"):
        stem = Path(path).stem
        lines = sources.lines(path) or []
        head = _clean(_heading_of(lines, parse_frontmatter("\n".join(lines))[0])) or stem.replace("-", " ")
        num = re.match(r"^(\d+)-", stem)
        title = f"Goal {num.group(1)}: {head}" if num else f"Goals file {stem.replace('-', ' ')}: {head}"
        if not _safe(title, name_terms):
            title = f"Goal file {stem.replace('-', ' ')}"
        specs.append(_spec(f"goal-{stem}", title, f"goal-{stem}",
                           f"Goal file {path}: read it before acting on anything it decides.",
                           doc_claims(path, "Goal file", "The goal file itself")))

    # Courses: one page per course over its exam pack and prep notes, and one per exam note.
    courses = set()
    for top in ("vault/exams", "vault/prep"):
        base = sources.resolve(top)
        for d in sorted(base.iterdir()) if base.is_dir() else []:
            if d.is_dir() and not d.name.startswith((".", "_")) and source_class(f"{top}/{d.name}/x.md") == "course":
                courses.add(d.name)
    for c in sorted(courses):
        cl = []
        for path in _md_files(sources, f"vault/exams/{c}"):
            cl += doc_claims(path, "Exam pack note", "An exam pack note for this course", sections=False)
        for path in _md_files(sources, f"vault/prep/{c}", recursive=True):
            cl += doc_claims(path, "Prep note", "A prep note for this course", sections=False)
        what = " and ".join(x for x, top in (("exam pack", "vault/exams"), ("session prep", "vault/prep"))
                            if _md_files(sources, f"{top}/{c}", recursive=True))
        specs.append(_spec(f"course-{c}", f"{c.upper()} course notes: {what}", f"course-{c}",
                           f"Where the {c.upper()} notes are: the exam pack in vault/exams/{c}/ and the prep notes in vault/prep/{c}/.",
                           cl))
        for path in _md_files(sources, f"vault/exams/{c}"):
            stem = Path(path).stem
            lines = sources.lines(path) or []
            head = _clean(_heading_of(lines, parse_frontmatter("\n".join(lines))[0]))
            title = f"{c.upper()} exam notes: {head or stem.replace('-', ' ')}"
            if not _safe(title, name_terms):
                title = f"{c.upper()} exam notes: {stem.replace('-', ' ')}"
            specs.append(_spec(f"course-{c}-{stem}", title, f"course-{c}-{stem}",
                               f"{c.upper()} exam pack note {path}.",
                               doc_claims(path, "Exam pack note", "The exam pack note itself")))

    # club minutes: meeting level. The page names the meeting's date and its topics, never a
    # person, and its id and title come from the date, never the file name.
    em = _md_files(sources, CLUB_MEETINGS) + _md_files(sources, CLUB_MEETINGS + "/processed")
    seen = {}
    for path in em:
        date = (re.match(r"^(\d{4}-\d{2}-\d{2})", Path(path).name) or [None, None])[1] or _source_date(sources, path)
        if not date:
            continue
        seen[date] = seen.get(date, 0) + 1
        n = seen[date]
        suffix = "" if n == 1 else f"-{n}"
        day = dt.date.fromisoformat(date).strftime("%a %d %b %Y")
        cl = [claim(path, f"Minutes of a club meeting held on {day}", f"Minutes of a club meeting held on {day}")]
        for ln, h in _sections(sources.lines(path) or []):
            cl.append(claim(path, f"Topic: {h}", None, ln))
        specs.append(_spec(f"club-minutes-{date}{suffix}", f"club meeting minutes, {day}" + (f" ({n})" if n > 1 else ""),
                           f"club-minutes-{date}{suffix}",
                           "Meeting-level pointer to club minutes. What was said is attributed to the meeting.",
                           [c for c in cl if c and c["text"]]))

    # Dictations: one page per ask, by page id. Never the dictation's file name in the id or title.
    by_ask = {}
    for d in read_dictations(sources):
        c = claim(d["path"], f"A {d['ask']} dictation from {d['date']}: {d['title']}",
                  f"A {d['ask']} dictation from {d['date']}")
        if c:
            by_ask.setdefault(d["ask"], []).append(c)
    for ask in ASK_KINDS:
        if by_ask.get(ask):
            specs.append(_spec(f"dictation-ask-{ask}", f"Dictations the owner filed as {ask}", f"dictation-ask-{ask}",
                               f"The owner's own {ask} dictations, newest last, pointed at by path and date.",
                               sorted(by_ask[ask], key=lambda c: (c["date"], c["path"]))))

    # Interview prep: career class.
    cl = []
    for path in _md_files(sources, "vault/interview-prep", recursive=True) + _md_files(sources, "vault/prep/careers", recursive=True):
        cl += doc_claims(path, "Career note", "A career note", sections=False)
    if cl:
        specs.append(_spec("career-interview-prep", "Interview prep and career notes", "career-interview-prep",
                           "Where the interview prep and career notes are.", cl))
    return specs


def _render_page(spec: dict, cls: str, claims: list, slug: str, subject: str) -> str:
    captured = max(c["date"] for c in claims)
    lines = ["---", f"id: kb-{cls}-{slug}", f'title: "{spec["title"]}"', "kind: pointer",
             f"subject: {subject}", f"class: {cls}", f"captured: {captured}",
             'source: "knowledge.py pointers"', 'found_by: "kb pointers"', "---", "",
             f"**Bottom line: {_clean(spec['bottom'])}.**", ""]
    for c in claims:
        loc = c["path"] + (f":{c['line']}" if c["line"] else "")
        lines.append(f"- {c['text']} [src: {loc} | {c['date']} | pointer]")
    return "\n".join(lines) + "\n"


def _page_from_text(text: str, fname: str) -> dict:
    fm, body, start = parse_frontmatter(text)
    claims, bad = [], []
    for i, line in enumerate(body.split("\n")):
        if line.startswith("- "):
            c = parse_claim(line)
            if c is None:
                bad.append((start + i, line))
            else:
                c["line_no"] = start + i
                claims.append(c)
    return {"path": Path(fname), "file": fname, "text": text, "fm": fm, "body": body,
            "claims": claims, "bad_bullets": bad}


def build_pointer_pages(sources: Sources, name_terms) -> tuple:
    """([(file name, text)], [(file name, defect)]). A claim whose source path or text trips
    termcheck moves to a `-private` sibling page, so one employer name in a file name
    does not lift a whole course page to private. The id's class is the class the sources give."""
    pages, skipped = [], []
    for spec in pointer_specs(sources, name_terms):
        open_c = [c for c in spec["claims"] if not leaks(c["path"] + " " + c["text"])]
        priv_c = [c for c in spec["claims"] if leaks(c["path"] + " " + c["text"])]
        for claims, slug, subject in ((open_c, spec["slug"], spec["subject"]),
                                      (priv_c, spec["slug"] + "-private", spec["subject"] + "-private")):
            if not claims:
                continue
            draft = _page_from_text(_render_page(spec, "system", claims, slug, subject), "x.md")
            cls = page_class(draft, sources, name_terms)
            fname = f"kb-{cls}-{slug}.md"
            text = _render_page(spec, cls, claims, slug, subject)
            defects = page_defects(_page_from_text(text, fname), sources, name_terms)
            if defects:
                skipped += [(fname, d) for d in defects]
            else:
                pages.append((fname, text))
    return pages, skipped


def pointers(to, source_root=None, emit=print) -> int:
    """Write the pointer pages into `to` (staging or a temp folder, never ~/vault). 0 written,
    1 nothing written, 2 refused."""
    to = Path(os.path.abspath(os.path.expanduser(str(to))))
    if _under(to, [str(VAULT), _real(VAULT)]) or _real(to) == _real(VAULT):
        emit(f"pointers: refused: {to} is inside {VAULT}; write to {STAGING} and install from there")
        return 2
    sources = Sources(Path(source_root)) if source_root else Sources()
    name_terms = names(fixture=source_root is not None)
    pages, skipped = build_pointer_pages(sources, name_terms)
    for f, d in skipped:
        emit(f"pointers: skipped {f}: {d}")
    if not pages:
        emit("pointers: no page written")
        return 1
    to.mkdir(parents=True, exist_ok=True)
    changed = 0
    for fname, text in pages:
        dest = to / fname
        if dest.exists() and dest.read_text(encoding="utf-8") == text:
            continue
        atomicio.write_text(dest, text)
        changed += 1
    by = {}
    for fname, _ in pages:
        c = fname.split("-")[1]
        by[c] = by.get(c, 0) + 1
    emit(f"pointers: {len(pages)} pages in {to} ({', '.join(f'{c} {n}' for c, n in sorted(by.items(), key=lambda x: RANK.get(x[0], 99)))}), "
         f"{changed} written or changed, {len(set(f for f, _ in skipped))} skipped")
    return 0


# The distiller's queue. One `KB distil` brief a night at most, Sonnet, 30 minutes,
# audience outward, naming at most 8 stale sources, oldest first.

DISTIL_MAX = 8
DISTIL_GAP = dt.timedelta(hours=20)
DISTIL_TEMPLATE = HERE / "templates" / "kb-distil-brief.md"
DISTIL_SKIP_CLASSES = ("club", "project")   # other people's words; decisions are pointers only


def queue_add_path() -> Path | None:
    """The live queue is the main checkout's (queue_add.py finds its queue from its own path).
    KB_QUEUE_ADD, under the temp dir only, points at a sandbox copy for the selftest. A temp
    index with no sandbox queue gets None: a test's stale list never reaches the live queue."""
    v = os.environ.get("KB_QUEUE_ADD")
    if v and under_temp(v):
        return Path(v)
    if index_path() != STATE / "kb-index.db":
        return None
    return Path(os.path.expanduser(v)) if v else HERE / "queue_add.py"


def distil_selection(stale: list, sources: Sources | None = None, cap: int | None = DISTIL_MAX) -> list:
    """Stale sources the distiller may read, oldest first, at most DISTIL_MAX. Excluded by class
    before selection: the excluded sources (call transcripts, lectures, group chats, the
    bank's own outputs), anything off the allow-list, club minutes (other people's words;
    their pointer pages are mechanical), decision sources (pointers only), and dictations the
    reader skips (noise, duplicates, no ask)."""
    sources = sources or Sources()
    ok_dictations = None
    keep = []
    for s in stale:
        p = _norm_path(str(s.get("path", "")))
        c = source_class(p)
        if not p or excluded(p) or c is None or c in DISTIL_SKIP_CLASSES:
            continue
        if c == "dictation":
            if ok_dictations is None:
                ok_dictations = {d["path"] for d in read_dictations(sources)}
            if p not in ok_dictations:
                continue
        keep.append(dict(s, path=p))
    keep.sort(key=lambda s: (s.get("source_date") or "9999-99-99", s["path"]))
    return keep if cap is None else keep[:cap]


def _distil_recent(qa: Path, now: dt.datetime) -> str | None:
    """Why no new KB distil brief may be queued: one pending, or one done in the last 20 h."""
    q = qa.parents[1] / "scheduled-work"
    for sub in ("pending", "done"):
        d = q / sub
        for f in sorted(d.glob("*.md")) if d.is_dir() else []:
            try:
                head = f.read_text(encoding="utf-8", errors="replace")[:2000]
            except OSError:
                continue
            if not re.search(r"(?m)^title:\s*[\"']?KB distil\b", head):
                continue
            if sub == "pending":
                return f"a KB distil brief is already pending ({f.name})"
            if now.timestamp() - f.stat().st_mtime < DISTIL_GAP.total_seconds():
                return f"a KB distil brief ran in the last 20 hours ({f.name})"
    return None


def render_distil_brief(date: str, picked: list, total: int) -> str:
    tpl = DISTIL_TEMPLATE.read_text(encoding="utf-8")
    listing = "\n".join(f"{i}. `{s['path']}` (source dated {s.get('source_date') or 'undated'}: "
                        f"{s.get('why', 'stale')})" for i, s in enumerate(picked, 1))
    more = (f"{total - len(picked)} more stale sources wait for later nights; the list is oldest first."
            if total > len(picked) else "That is every stale source the bank may read tonight.")
    tpl = (tpl.replace("{{repo}}", str(MAIN_CHECKOUT)).replace("{{staging}}", str(STAGING))
           .replace("{{live_root}}", str(LIVE_ROOT)).replace("{{kb}}", str(HERE / "knowledge.py")))
    return (tpl.replace("{{date}}", date).replace("{{n}}", str(len(picked)))
            .replace("{{sources}}", listing).replace("{{more}}", more))


def distil_queue(dry_run: bool = False, now=None, source_root=None, emit=print) -> int:
    """Queue at most one KB distil brief. 0 queued or nothing to do, 1 failed,
    2 refused (a test index with no sandbox queue)."""
    now = now or dt.datetime.now()
    date = now.date().isoformat()
    sf = index_path().parent / "kb-stale.json"
    try:
        data = json.loads(sf.read_text(encoding="utf-8"))
    except OSError:
        emit(f"nothing stale (no {sf}: the bank has not been scanned yet)")
        return 0
    except ValueError:
        emit(f"distil-queue: {sf} is not JSON; run knowledge.py scan")
        return 1
    stale = data.get("sources") or []
    every = distil_selection(stale, Sources(Path(source_root)) if source_root else None, cap=None)
    if not every:
        emit(f"nothing stale ({len(stale)} listed, none the distiller may read)")
        return 0
    eligible, total = every[:DISTIL_MAX], len(every)
    qa = queue_add_path()
    if qa is None and not dry_run:
        emit("distil-queue: refused: the index is not the live one and KB_QUEUE_ADD names no sandbox queue")
        return 2
    why = _distil_recent(qa, now) if qa else None
    if why:
        emit(f"already queued: {why}")
        return 0
    title = f"KB distil {date}"
    if dry_run:
        emit(f"would queue: {title} ({len(eligible)} of {total} stale sources, audience outward, 30 min, sonnet)")
        for s in eligible:
            emit(f"  {s['path']} ({s.get('source_date') or 'undated'})")
        return 0
    brief = STATE_DIR() / f"kb-distil-{date}.md"
    brief.parent.mkdir(parents=True, exist_ok=True)
    atomicio.write_text(brief, render_distil_brief(date, eligible, total))
    try:
        r = subprocess.run([sys.executable, str(qa), "add", "--at", "tonight", "--title", title, "--file", str(brief),
                            "--kb-audience", "outward", "--max-minutes", "30", "--model", "sonnet"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        emit(f"distil-queue: queue_add failed: {e}")
        return 1
    out = (r.stdout + r.stderr).strip()
    emit(f"distil-queue: {out.splitlines()[-1] if out else 'queue_add said nothing'}")
    return 0 if r.returncode == 0 else 1


def STATE_DIR() -> Path:  # noqa: N802 - reads like the constant it stands in for
    """Where the bank's state files go: beside the index, so a test index keeps them in temp."""
    return index_path().parent


# Purge: take a source out of the bank everywhere it is quoted or pointed at. By hand, for the owner:
# never under a managed run. Nothing is deleted as a file (a design rule: code here never deletes): pages
# and _history copies are rewritten without the claims, and the index rows are removed.

def _purge_match(arg: str):
    a = _norm_path(arg)
    if "/" in a:
        if a.endswith("/"):
            return lambda p: _norm_path(p).startswith(a)
        return lambda p: _norm_path(p) == a
    return lambda p: Path(p).name == a or Path(p).stem == a


def _strip_claims(text: str, match) -> tuple:
    out, n = [], 0
    for line in text.split("\n"):
        c = parse_claim(line) if line.startswith("- ") else None
        if c and match(c["path"]):
            n += 1
            continue
        out.append(line)
    return "\n".join(out), n


def _fts_body(page: dict) -> str:
    bottom = " ".join(x for x in page["body"].split("\n") if x.startswith("**"))
    return " ".join([bottom] + [c["text"] + " " + (c["fragment"] or "") for c in page["claims"]])


def purge(source: str, root=None, index=None, emit=print) -> int:
    """0 purged (or nothing cited it), 1 NOT LIVE, 2 refused."""
    if os.environ.get("LT_RUN_ID"):
        emit("purge: refused under a managed run (LT_RUN_ID is set); the owner runs purge by hand")
        return 2
    if not source.strip():
        emit("purge: refused: give --source <path or id>")
        return 2
    try:
        root = Path(root) if root is not None else root_path()
    except KBError as e:
        emit(f"purge: refused: {e}")
        return 2
    if not root.is_dir():
        emit(f"Knowledge bank: NOT LIVE ({root} does not exist)")
        return 1
    problems = validate_root(root)
    if problems:
        emit(f"Knowledge bank: NOT LIVE ({problems[0]})")
        return 1
    match = _purge_match(source)
    manifest = read_manifest(root)
    entries = manifest.get("pages") or {}
    touched, claims_out, hist_out = [], 0, 0
    today = dt.date.today().isoformat()
    for name in sorted(entries):
        p = root / name
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8")
        new, n = _strip_claims(text, match)
        if not n:
            continue
        if not load_page_text(new)["claims"]:
            new = new.rstrip("\n") + f"\n\nEvery claim on this page cited a source purged on {today}.\n"
        atomicio.write_text(p, new)
        entries[name] = dict(entries[name], sha256=sha256_file(p))
        touched.append(name)
        claims_out += n
    hist = root / HISTORY
    for f in sorted(hist.glob("*.prev")) if hist.is_dir() else []:
        text = f.read_text(encoding="utf-8", errors="replace")
        new, n = _strip_claims(text, match)
        if n:
            atomicio.write_text(f, new)
            hist_out += 1
    if touched:
        manifest["pages"] = entries
        manifest["purged"] = today
        atomicio.write_text(root / MANIFEST, json.dumps(manifest, indent=1) + "\n")
    idx = Path(index) if index is not None else index_path()
    rows_out = 0
    if idx.is_file():
        fd = os.open(str(idx.with_suffix(".lock")), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            conn = sqlite3.connect(str(idx))
            try:
                paths = [r[0] for r in conn.execute("SELECT DISTINCT path FROM claims")]
                hit = [x for x in paths if match(x)]
                pids = sorted(set(r[0] for x in hit for r in
                                  conn.execute("SELECT page_id FROM claims WHERE path=?", (x,))))
                for x in hit:
                    rows_out += conn.execute("DELETE FROM claims WHERE path=?", (x,)).rowcount
                engine = (conn.execute("SELECT value FROM meta WHERE key='engine'").fetchone() or ["fts5"])[0]
                for pid in pids:
                    row = conn.execute("SELECT n, file FROM pages WHERE id=?", (pid,)).fetchone()
                    if not row:
                        continue
                    n, fname = row
                    left = conn.execute("SELECT count(*) FROM claims WHERE page_id=? AND valid=1", (pid,)).fetchone()[0]
                    conn.execute("UPDATE pages SET live = live * ?, sha_ok = 1 WHERE n=?", (1 if left else 0, n))
                    body = _fts_body(load_page(root / fname)) if (root / fname).is_file() else ""
                    if engine == "fts5":
                        conn.execute("UPDATE pages_fts SET claims=? WHERE rowid=?", (body, n))
                    else:
                        conn.execute("DELETE FROM pages_tok WHERE n=? AND field=2", (n,))
                        tf = {}
                        for t in _terms(body):
                            tf[t] = tf.get(t, 0) + 1
                        conn.executemany("INSERT INTO pages_tok VALUES (?,?,?,?)", [(n, 2, t, k) for t, k in tf.items()])
                conn.commit()
            finally:
                conn.close()
        finally:
            os.close(fd)
        sf = idx.parent / "kb-stale.json"
        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
            keep = [s for s in data.get("sources") or [] if not match(str(s.get("path", "")))]
            if len(keep) != len(data.get("sources") or []):
                atomicio.write_text(sf, json.dumps(dict(data, sources=keep), indent=1) + "\n")
        except (OSError, ValueError):
            pass
    emit(f"purge: {claims_out} claims removed from {len(touched)} pages, {hist_out} history copies rewritten, "
         f"{rows_out} index rows removed")
    return 0


def load_page_text(text: str) -> dict:
    return _page_from_text(text, "x.md")


# ---------------------------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    v = sub.add_parser("verify", help="lint the pages in a folder")
    v.add_argument("dir")
    v.add_argument("--sources", help="resolve cited paths under this folder instead of the repo")
    i = sub.add_parser("install", help="verify, then copy pages into the bank")
    i.add_argument("--from", dest="src", required=True)
    i.add_argument("--to", dest="root")
    i.add_argument("--sources", help="resolve cited paths under this folder instead of the repo")
    s = sub.add_parser("status", help="is the bank live, and what is in it")
    s.add_argument("--json", action="store_true")
    s.add_argument("--line", action="store_true", help="the one-line health check with launch-hook counts")
    sc = sub.add_parser("scan", help="verify every claim and rebuild the index")
    sc.add_argument("--if-due", action="store_true", help="skip when the index is under an hour old")
    sc.add_argument("--sources", help="resolve cited paths under this folder instead of the repo")
    q = sub.add_parser("ask", help="the few most relevant pages, with whole-line fragments")
    q.add_argument("text", nargs="?", default="")
    q.add_argument("--audience", choices=AUDIENCES)
    q.add_argument("--brief", help="build the question from this queue entry or brief file")
    q.add_argument("--k", type=int, default=TOP_K)
    q.add_argument("--json", action="store_true")
    pt = sub.add_parser("pointers", help="write the mechanical pointer pages into a folder")
    pt.add_argument("--to", required=True)
    pt.add_argument("--sources", help="read sources under this folder instead of the repo")
    dq = sub.add_parser("distil-queue", help="queue at most one KB distil brief a night")
    dq.add_argument("--dry-run", action="store_true")
    dq.add_argument("--sources", help="read sources under this folder instead of the repo")
    pg = sub.add_parser("purge", help="remove a source's claims, history copies and index rows")
    pg.add_argument("--source", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "verify":
        if not Path(a.dir).is_dir():
            print(f"verify: {a.dir} is not a folder", file=sys.stderr)
            return 2
        files = page_files(a.dir)
        defects = verify_dir(a.dir, Path(a.sources) if a.sources else None)
        for f, d in defects:
            print(f"{f}: {d}")
        bad = len(set(f for f, _ in defects))
        print(f"verify: {len(files)} pages, {bad} with defects")
        return 1 if defects or not files else 0
    if a.cmd == "install":
        if a.root:
            try:
                ok_root = (under_temp(a.root) or _real(a.root) == _real(LIVE_ROOT)
                           or _under(a.root, [str(STAGING)]) is not None)
            except OSError:
                ok_root = False
            if not ok_root:
                print(f"install: refused: --to {a.root} is neither the bank, staging nor under the temp dir")
                return 2
        rc, msgs = install(a.src, a.root, Path(a.sources) if a.sources else None)
        for m in msgs:
            print(m)
        print(f"install exit={rc}")
        return rc
    if a.cmd == "scan":
        if a.if_due:
            idx = index_path()
            if idx.exists() and dt.datetime.now().timestamp() - idx.stat().st_mtime < 3600:
                print("scan: not due (index under an hour old)")
                print("kb scan exit=0")
                return 0
        try:
            rc = scan(source_root=Path(a.sources) if a.sources else None)
        except (OSError, sqlite3.Error, KBError) as e:
            print(f"scan: failed: {e}")
            rc = 1
        if rc != 0:
            print(f"kb scan exit={rc}")
        return rc
    if a.cmd == "ask":
        text = a.text
        if a.brief:
            try:
                text = (text + "\n" + query_for_entry(a.brief)["text"]).strip()
            except OSError as e:
                print(f"ask: cannot read {a.brief}: {e}", file=sys.stderr)
                return 2
        if not text.strip():
            print("ask: give a question or --brief <file>", file=sys.stderr)
            return 2
        rc, res = ask(text, a.audience, max(1, min(a.k, 10)))
        if a.json:
            print(json.dumps(res, indent=1))
        elif "refused" in res:
            print(f"ask: refused: {res['refused']}")
        elif "not_live" in res:
            print(f"Knowledge bank: NOT LIVE ({res['not_live']})")
        else:
            print(_ask_text(res))
        return rc
    if a.cmd == "pointers":
        return pointers(a.to, a.sources)
    if a.cmd == "distil-queue":
        return distil_queue(dry_run=a.dry_run, source_root=a.sources)
    if a.cmd == "purge":
        return purge(a.source)
    if a.cmd == "status" and a.line:
        line = kb_line()
        print(line)
        return 1 if line.startswith("Knowledge bank: NOT LIVE") else 0
    if a.cmd == "status":
        st = status()
        print(json.dumps(st, indent=1) if a.json else _status_line(st))
        return 0 if st.get("live") else 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
