#!/usr/bin/env python3
"""termcheck.py - the term blocklist: names that must never reach a shared page or a brief.

    python3 kb/termcheck.py --terms           list the loaded terms
    python3 kb/termcheck.py FILE ...          exit 1 if any file names a blocked term

The terms come from a plain text file, one term per line, `#` for comments. The path is
KB_BLOCKLIST, else ~/.config/knowledge-bank/blocklist.txt. Keep that file outside any repo:
it is, by its nature, a list of things you would rather not publish.

Typical entries are employers you have applied to, assessment vendors, client names and
reference numbers. knowledge.py runs every page through this check at install and at scan,
and a page that names a term becomes `private`. kb_hook.py runs every block line through it.

Matching ignores punctuation and whitespace between a term's letters, so "zorblax holdings"
also catches "Zorblax^Holdings", "zorblax_holdings" and "ZORBLAX\nHOLDINGS", but never a term
inside another word. Stdlib only.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

DEFAULT = Path.home() / ".config" / "knowledge-bank" / "blocklist.txt"

# `~` is LaTeX's non-breaking space. `^`, `_` and `/` are the separators a datamarked or slugged
# block uses, so a page or block written that way is still caught.
NAME_GAPS = set(".-&'’~^_/")


def blocklist_path() -> Path:
    v = os.environ.get("KB_BLOCKLIST")
    return Path(os.path.expanduser(v)) if v else DEFAULT


def blocklist() -> dict:
    """term -> why it is blocked. An absent file is an empty list."""
    p = blocklist_path()
    out = {}
    try:
        for line in p.read_text(encoding="utf-8").split("\n"):
            t = line.split("#", 1)[0].strip().lower()
            if len(t) >= 2:
                out[t] = f"listed in {p.name}"
    except OSError:
        pass
    return out


def squash(text: str) -> tuple:
    """Letters and digits only, with each kept character's index in the original."""
    pos = [i for i, ch in enumerate(text) if ch.isalnum()]
    return "".join(text[i] for i in pos), pos


def find_term(text: str, squashed: tuple, term: str):
    """Offset in `text` of the first whole-word occurrence of `term`, ignoring the separators
    a name is written with between its letters. Inside another word never counts."""
    key = squash(term)[0]
    flat, pos = squashed
    if not key:
        return None
    # A term spelt with a separator ("zor-b") must be matched with one, or an English word is a hit.
    needs_gap = not term.strip().isalnum()
    k = flat.find(key)
    while k != -1:
        start, end = pos[k], pos[k + len(key) - 1]
        span = text[start:end + 1]
        gaps = [ch for ch in span if not ch.isalnum()]
        if ((start == 0 or not text[start - 1].isalnum())
                and (end + 1 >= len(text) or not text[end + 1].isalnum())
                and end - start < 2 * len(key) + 2
                and all(ch.isspace() or ch in NAME_GAPS for ch in gaps)
                and (gaps or not needs_gap)):
            return start
        k = flat.find(key, k + 1)
    return None


def hits(text: str, terms=None) -> list:
    terms = blocklist() if terms is None else terms
    low = text.lower()
    sq = squash(low)
    return [t for t in terms if find_term(low, sq, t) is not None]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="check files against the term blocklist")
    ap.add_argument("--terms", action="store_true", help="list the loaded terms")
    ap.add_argument("files", nargs="*")
    a = ap.parse_args(argv)
    terms = blocklist()
    if a.terms:
        print(f"{len(terms)} term(s) from {blocklist_path()}")
        for t in sorted(terms):
            print(f"  {t}")
        return 0
    bad = 0
    for f in a.files:
        found = hits(Path(f).read_text(encoding="utf-8", errors="replace"), terms)
        if found:
            bad += 1
            print(f"{f}: {', '.join(sorted(found))}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
