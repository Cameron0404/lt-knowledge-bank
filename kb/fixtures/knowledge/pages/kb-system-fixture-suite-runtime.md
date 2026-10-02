---
id: kb-system-fixture-suite-runtime
title: "Fixture suite runtime and the three-piece split"
kind: topic
subject: fixture-suite-runtime
class: system
captured: 2026-09-20
source: "knowledge-bank fixture corpus"
found_by: "kb fixture"
---

**Bottom line: the fixture suite is longer than one tool call, so it runs in pieces.**

- The whole fixture suite took 7 min 12 s on the base. [src: docs/kb-fixture-suite.md#p3b537593:3 | 2026-09-15 | q: "The whole fixture suite took 7 min 12 s on the fixture base commit." | high]
- One tool call is 300 s, hence three pieces. [src: docs/kb-fixture-suite.md#p3b537593:4 | 2026-09-15 | q: "It is longer than one 300 s tool call, so it runs in three pieces." | high]
- Two measurements of the slow queue selftest disagree, 181 s here and 205 s in a build run; kept side by side, not averaged. [src: docs/kb-fixture-suite.md#pd474766c:8 | 2026-09-15 | q: "Piece three runs the slow queue selftest alone, which took 181 s." | high]
- The build run's figure for the slow queue selftest. [src: _meta/agent-runs/_reports/build-fixture-alpha-build-1-report.md#p5607109f:4 | 2026-09-16 | q: "The slow queue selftest measured at 205 s in this run." | high]
