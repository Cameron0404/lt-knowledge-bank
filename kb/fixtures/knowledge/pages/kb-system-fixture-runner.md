---
id: kb-system-fixture-runner
title: "How the fixture runner handles a held lock"
kind: topic
subject: fixture-runner-lock
class: system
captured: 2026-09-20
source: "knowledge-bank fixture corpus"
found_by: "kb fixture"
---

**Bottom line: a held lock means wait a minute and retry, and records are never trimmed.**

- A launch that finds the lock held waits one minute. [src: _meta/scripts/kb-fixture-notes.md#p54035d7e:4 | 2026-09-12 | q: "A launch that finds the lock held waits one minute and tries again." | high]
- Old records are kept. [src: _meta/scripts/kb-fixture-notes.md#pa8d61ce2:6 | 2026-09-12 | q: "Records older than 30 days are kept, never trimmed." | high]
