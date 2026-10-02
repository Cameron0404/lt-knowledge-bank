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

- It lists every job that did not run. [src: docs/morning-report.md#pac9a0c11:7 | 2026-10-01 | q: "The morning report lists every scheduled job that did not run since the last report." | high]
- A failed job shows its exit code and log tail. [src: docs/morning-report.md#pac9a0c11:8 | 2026-10-01 | q: "A job that failed shows its exit code and the last ten lines of its log." | high]
- It is written at 07:00 and never emailed. [src: docs/morning-report.md#p7231bb22:12 | 2026-10-01 | q: "The report is written to reports/morning.md at 07:00 and is never emailed." | high]
