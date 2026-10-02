---
id: kb-system-fixture-index
title: "Where the fixture index lives and how fast it rebuilds"
kind: topic
subject: fixture-index
class: system
captured: 2026-09-20
source: "knowledge-bank fixture corpus"
found_by: "kb fixture"
---

**Bottom line: the index sits outside the vault and rebuilds in seconds.**

- The index is kept out of the mirrored vault. [src: _meta/builds/fixture-alpha/out/research-1.md#pab0ed6c1:3 | 2026-09-14 | q: "The index file lives outside the vault so the mirror never copies it." | high]
- A rebuild took 2.4 s on 90 pages. [src: _meta/builds/fixture-alpha/out/research-1.md#pab0ed6c1:4 | 2026-09-14 | q: "A rebuild of the fixture index took 2.4 s on 90 pages." | inferred]
- The compile piece took 94 s in the build run. [src: _meta/agent-runs/_reports/build-fixture-alpha-build-1-report.md#p5607109f:3 | 2026-09-16 | q: "Compile piece measured at 94 s in this run." | high]
