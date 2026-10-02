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

- It runs nightly at 02:15. [src: docs/backup-routine.md#pf2647343:7 | 2026-10-01 | q: "The backup job runs at 02:15 every night and copies the notes folder to the external drive." | high]
- A missing drive gives exit code 3 and one report line. [src: docs/backup-routine.md#pf2647343:8 | 2026-10-01 | q: "A run that finds the drive missing writes one line to the morning report and exits with code 3." | high]
- Copies are kept for 14 days. [src: docs/backup-routine.md#p5587f972:12 | 2026-10-01 | q: "Each night's copy is kept for 14 days, then the oldest copy is overwritten." | high]
- The source folder is never deleted. [src: docs/backup-routine.md#p5587f972:13 | 2026-10-01 | q: "The job never deletes the source folder, it only reads it." | high]
