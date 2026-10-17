# lt-knowledge-bank

A knowledge bank for AI agents, where every claim on a page quotes the whole source line it came from. Each quote names the file, the line, the commit (or a paragraph hash for an untracked file) and the date. Agents get pointers to the pages, never distilled sentences, and a daily scan stops serving any claim whose source line has gone.

It is one system from the Life Tracker, my personal agent system, published on its own. Written up at [thecameronmills.com/projects/knowledge-bank](https://thecameronmills.com/projects/knowledge-bank). The session handover that the site page describes alongside it (`/initiate`, `/checkpoint` and the session ledger) lives in [life-tracker-kit](https://github.com/cmills-dev/life-tracker-kit).

## Why

Unattended agents start every run cold. They re-read the same docs, re-derive the same facts and sometimes get them wrong. I wanted a bank that every agent draws on for the task in hand.

The obvious design hands agents distilled sentences with citations attached. That fails often. Liu and colleagues, "Evaluating Verifiability in Generative Search Engines" (Findings of EMNLP 2023, arxiv.org/abs/2304.09848) found that only 51.5% of generated sentences are fully supported by their citations, and only 74.5% of citations support their sentence. That study is of web search answers, not of single-source notes, but it is why this bank never puts a distilled sentence into a brief. The brief names pages. The agent reads the page, and `ask` returns the whole source lines, so what an agent acts on is the source's own wording.

## How it works

```
 your repo and vault            staging folder              the bank (a real folder)
 docs/, reports, notes  ──────► pages written by  ──verify──► ~/vault/wiki/knowledge/
                                 hand or by a       install    + manifest + _history/
                                 nightly agent                        │
                                                                      │ scan (daily)
                                                                      ▼
 queued agent brief ◄── kb_hook.py adds a ≤120-word block ◄── SQLite FTS5 index
                        naming up to 3 pages                         │
 any agent ──────────── knowledge.py ask "question" ─────────────────┘
                        (pages plus whole source lines)
```

- **Pages.** One page per topic, project or decision. Every bullet is one claim with its citation. The format and the rules are in [docs/page-format.md](docs/page-format.md).
- **verify and install.** `verify` lints a folder of pages: format, citations, title rules, the class wall and the people rule. `install` verifies, then copies the pages into the bank. It never deletes: an overwritten page's previous text goes to `_history/`.
- **The wall.** A page's class is the most restrictive class among the sources it cites, worked out from the paths, so an author cannot label a career note as a system page. Each audience sees only some classes. A page that names a term on your blocklist becomes private.
- **scan.** Rebuilds the index, re-checks every quoted line against its source (through `git show main:<path>` for tracked files) and lists the sources that are newer than the pages citing them.
- **ask.** Returns the few most relevant pages with their whole source lines. Retrieval runs on SQLite FTS5, with a plain token match as the fallback.
- **The launch hook.** `kb_hook.py` runs on each queued brief before an agent starts it, and adds at most 120 words naming up to three pages. It fails open: a four-second alarm, and on any error the brief launches untouched. A kill switch file turns it off.
- **distil-queue.** After a good scan, queues at most one distil brief a night for an agent to turn up to eight stale sources into new pages.
- **The status line.** `status --line` says `Knowledge bank: NOT LIVE (<reason>)` when the bank fails validation, the index is missing or the last scan is over 48 hours old, and always counts the launch hook's outcomes, so a broken retriever shows as failed launches, never as "nothing relevant".

## Requirements

- macOS or Linux. It was built and run on macOS.
- Python 3.9 or newer, standard library only. Nothing to `pip install`.
- SQLite with FTS5 (the Python that ships with macOS and most Linux builds has it). Without FTS5 retrieval falls back to token matching.
- git, for the claim checks on tracked files.
- bash, for `setup.sh`, `bin/kb`, `kb/daily.sh` and the demo.

## Setup, step by step

1. Clone the repo.

   ```bash
   git clone https://github.com/cmills-dev/lt-knowledge-bank.git
   cd lt-knowledge-bank
   ```

2. Run the setup script. It checks Python and FTS5, writes `~/.config/knowledge-bank/env` from `.env.example`, creates an empty blocklist and people list there, makes the staging and state folders, then runs the selftests and the demo. It never overwrites a file that already exists.

   ```bash
   bash setup.sh
   ```

3. See the whole cycle on the invented example corpus. It works in a temp folder and leaves your real bank alone.

   ```bash
   bash examples/demo.sh
   ```

4. Edit `~/.config/knowledge-bank/env`. At least set `KB_SOURCE_REPO` (the git repo your pages cite), `KB_LIVE_ROOT` (where the bank lives) and `KB_OWNER_NAMES` (your own name parts).

5. Fill the two private lists in `~/.config/knowledge-bank/`, one entry per line. `blocklist.txt` holds terms that must never reach a shared page or a brief, such as employers you applied to, client names or reference numbers. `people.txt` holds other people's names, so no page is ever about a person. Keep both files out of every repo.

6. Write your first pages into the staging folder (`~/kb-staging` by default), following [docs/page-format.md](docs/page-format.md). Then verify, install and scan.

   ```bash
   bin/kb verify ~/kb-staging
   bin/kb install --from ~/kb-staging
   bin/kb scan
   bin/kb status --line
   ```

7. Schedule the daily upkeep, once a morning. With cron:

   ```
   50 6 * * *  /path/to/lt-knowledge-bank/bin/kb daily
   ```

   On macOS a launchd agent does the same. `daily` runs `KB_PRE_SCAN_CMD` if you set one, then `scan`, then `distil-queue`.

8. Wire the hook into whatever launches your agents. Before the agent reads its brief, call:

   ```bash
   bin/kb hook --entry <queue entry file> --brief <brief temp file> --run <run name>
   ```

   The hook only rewrites the brief temp file. Keep a copy of the brief before the call and put it back if the hook exits non-zero or is killed. A brief gets a block only when its queue entry says `kb_audience: private`.

## Configuration

Every setting is an environment variable. `bin/kb` loads them from `~/.config/knowledge-bank/env` (or `KB_ENV_FILE`), and anything already set in the environment wins.

| Variable | Default | What it sets |
|---|---|---|
| `KB_SOURCE_REPO` | the current folder | The git repo whose files the pages cite |
| `KB_MAIN_BRANCH` | `main` | The branch tracked claims are checked against |
| `KB_VAULT` | `~/vault` | Your notes vault |
| `KB_LIVE_ROOT` | `~/vault/wiki/knowledge` | The bank folder (a real folder, never a symlink) |
| `KB_STAGING` | `~/kb-staging` | Where pages are written before install |
| `KB_STATE_HOME` | `~/.local/state/knowledge-bank` | Index, hook log, cache and the kill switch |
| `KB_BLOCKLIST` | `~/.config/knowledge-bank/blocklist.txt` | Terms that make a page private |
| `KB_PEOPLE_FILE` | none | Other people's names, refused on any page |
| `KB_OWNER_NAMES` | none | Your own name parts, never treated as another person |
| `KB_EXPORT_TREES` | `vault/prep:vault/exams` | Repo trees whose contents leave the machine |
| `KB_COURSEWORK_TREES` | `_meta/coursework-queue:vault/revision` | Repo trees of shared group work, also outward |
| `KB_EXCLUDE` | `~/Documents/Recordings:_meta/video` | Path prefixes that are never a source, such as call recordings |
| `KB_ATTENDEE_DIRS` | none | Folders of meeting JSON whose attendee names join the people list |
| `KB_QUEUE_ADD` | `kb/queue_add.py` | The queue the nightly distil brief goes to |
| `KB_PRE_SCAN_CMD` | none | A command `daily` runs before the scan |

`KB_ROOT`, `KB_INDEX`, `KB_STATE_DIR` and the other test switches are honoured only under the system temp folder (and `KB_ROOT` under staging), so a test can never point the bank at a live folder by mistake.

A missing blocklist or people file fails open: it counts as an empty list, so a run report that names a term you never listed is served as a system page. Fill both files before you install pages built from reports.

Outside a managed run (no `LT_RUN_ID` in the environment), `ask --audience private` returns every class, fragments included. It is meant for you in your own terminal. Under a managed run the audience comes from the run's sidecar, and a wider `--audience` is refused.

To turn the launch hook off, create the kill switch file: `touch ~/.local/state/knowledge-bank/kb-inject-off`.

## Usage

```bash
# The few most relevant pages for a question, with their whole source lines
bin/kb ask "what happens when the backup drive is missing"
bin/kb ask --brief path/to/brief.md --k 3 --json

# Is the bank live, how many pages of each class, when was it last scanned
bin/kb status
bin/kb status --line

# Pointer pages, built mechanically from course notes, goals, club minutes and dictations
bin/kb pointers --to ~/kb-staging

# What the nightly distil would queue, without queueing it
bin/kb distil-queue --dry-run

# Take one source out of the bank everywhere it is quoted (by hand only)
bin/kb purge --source docs/old-notes.md

# Check any file against your blocklist
bin/kb termcheck path/to/draft.md
```

Output of `ask` on the example corpus:

```
kb-system-example-backup-routine | The nightly backup routine and what it keeps | .../kb-system-example-backup-routine.md (system)
  - A missing drive gives exit code 3 and one report line.
    "A run that finds the drive missing writes one line to the morning report and exits with code 3." [docs/backup-routine.md:8 | 2026-10-01 | high]
```

## Tests

```bash
bash tests/run_all.sh
```

This runs three selftests (the atomic write, the page rules with install, scan and ask, and the launch hook) plus a verify of the example pages. They use the invented fixture corpus in `kb/fixtures/knowledge/` and write only under a fresh temp folder. One case, `tracked_file_needs_commit`, needs the repo to be a git clone and prints `SKIP` otherwise. Clone outside the system temp folder: the tests treat anything under it as a test path, so two confinement cases fail when the repo itself sits there. The hook test includes a timing run of 20 launches against 309 pages, with the load average read through `os.getloadavg()`.

## Results in my own use

From the build on 30 Sep 2026 in the Life Tracker. The 90-page seed installed with 724 of 724 claims valid. On 12 held-out agent briefs, 19 of the 36 top-three results were relevant (0.528), against a bar of 0.30 fixed before the run.

## Limitations

- Three review judges scored the first bank 4, 4 and 7 out of 10. The main reason: 72 of the 90 seed pages were pointers, because the distiller had not yet run.
- Retrieval has no relevance floor, so any shared word returns a result. Read what `ask` returns before trusting it.
- The hook missed its four-second bound at p95 under heavy machine load (load average 15 to 24). On a busy night some launches go without a block, and the status line counts them as failed.
- The class table is written for the Life Tracker's folder layout (`docs/`, `_meta/`, `vault/`). Another layout needs `source_class`, `volatility` and `EXCLUDED` in `kb/knowledge.py` edited to fit.
- The names check catches the names it knows. It is a backstop, and the class table is the real wall.
- The distil step needs an agent runner to pick up the queued brief. The bundled `kb/queue_add.py` only writes the entry.
- Writing pages by hand is slow. The design expects an agent to write them, with `verify` as the gate.

## Licence

MIT, see [LICENSE](LICENSE). Copyright (c) 2026 Cameron Mills.
