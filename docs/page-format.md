# The page format

A bank page is one markdown file named `<id>.md`, with flat frontmatter, a bold bottom line and
one bullet per claim. Every claim carries its citation at the end of its line.

```markdown
---
id: kb-system-backup-routine
title: "The nightly backup routine and what it keeps"
kind: topic                     # topic | project | decision | pointer
subject: backup-routine         # unique across the bank
class: system                   # checked by verify against the cited sources
captured: 2026-10-01
source: "how this page was made"
found_by: "who or what made it"
---

**Bottom line: the backup runs at 02:15 and keeps 14 nightly copies.**

- It runs nightly at 02:15. [src: docs/backup-routine.md@3f2a9c1:7 | 2026-10-01 | q: "<the whole source line>" | high]
- A claim tied to no single line. [src: docs/notes.md#p1a2b3c4d:4-6 | 2026-10-01 | q: "<the whole first line>" | inferred]
- A decision is pointed at, never quoted. [src: _meta/decisions.md | 2026-10-01 | pointer]
```

## The claim grammar

- `@<commit>` is required when the cited file is tracked by git in the source repo. An untracked
  file carries `#p<hash8>` instead: the first eight hex digits of the sha256 of the paragraph,
  whitespace normalised.
- The quoted fragment after `q:` must be the whole cited line, found within three lines of the
  line number. `scan` re-checks this every day and stops serving a claim whose line has gone.
- `high` means the claim says what the line says. `inferred` means the claim is drawn from it.
- A `pointer` claim names a source and quotes nothing. Decisions and goals are pointers only.

## How long a claim is served

It depends on where the source sits (its volatility, read from the path alone).

| Volatility | Paths | Served |
|---|---|---|
| state | run reports, build outputs, session ledgers | 24 hours from the source's own date |
| procedure | `docs/`, `CLAUDE.md`, `_meta/scripts/*.md` | while the fragment still matches |
| reference | vault notes, dictations, club minutes | while the fragment still matches |
| decision | goals, decision logs, `target.md` | as a pointer only, with no text |

## Titles

At most 12 words, and never starting with an imperative such as "run", "ignore" or "delete",
so a title cannot read as an instruction once it sits inside an agent's brief. A title may not
carry a `**Repo:` line or a fence marker.

## Classes

A page's class is the most restrictive class among the sources it cites, set mechanically by
`verify` from the source paths. Least to most restrictive: system, project, course, club,
career, dictation, private. The table is an allow-list: a source on no row is refused.

| Source path | Class |
|---|---|
| `docs/`, `CLAUDE.md`, `_meta/scripts/*.md` | system |
| `_meta/agent-runs/_reports/`, `_meta/builds/*/out/` | system if the report passes the term blocklist and names nobody, else private |
| `_meta/goals/`, `_meta/decisions.md`, `_meta/builds/*/target.md` | project (pointer only) |
| `vault/exams/`, `vault/prep/` (except `vault/prep/careers/`) | course |
| `~/Documents/Club/meetings/` | club |
| `_inbox/candidate-dictation-*`, `~/vault/raw/inbox/dictations/` | dictation |
| `vault/interview-prep/`, `vault/prep/careers/`, `../career/`, `obligations/co-*` | career |

Some sources are excluded outright and fail `verify`: group chat captures, the group roster,
lecture and call transcripts, anything under a `KB_EXCLUDE` prefix (call recordings, for
example), and the bank's own folder, staging and state. The rules are in `_excluded_rules`
in `kb/knowledge.py`. The table above is written for the layout
of the Life Tracker. To fit another layout, edit `source_class`, `volatility` and `EXCLUDED`
there, and the selftests show what each rule is meant to hold.

## People

A page is about a topic, a project or a decision, never about a person. `verify` refuses a page
that names anyone on the people list (`KB_PEOPLE_FILE`, the group roster, and the attendee
names in the `KB_ATTENDEE_DIRS` folders), that gives a role label to fewer than three people, or that
attributes speech to a role. What was said is attributed to the meeting.

## Audiences

| Audience | In a launch block | Through `ask` |
|---|---|---|
| private | system, project, course (ids, titles and paths only) | every class, with fragments |
| coursework | nothing | course only |
| open (`ask` outside a managed run) | nothing | system, project, course |
| club, career, outward, missing or unknown | nothing | system, project, course |
