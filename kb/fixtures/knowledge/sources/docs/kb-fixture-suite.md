# Fixture suite notes

The whole fixture suite took 7 min 12 s on the fixture base commit.
It is longer than one 300 s tool call, so it runs in three pieces.

Piece one is compile only.
Piece two runs every selftest except the slow queue one.
Piece three runs the slow queue selftest alone, which took 181 s.
