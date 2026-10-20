#!/usr/bin/env python3
"""Atomic write permission and failure regressions using temporary files."""
import importlib.util
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
MODULE = Path(__file__).resolve().parents[1] / "kb/atomicio.py"
spec = importlib.util.spec_from_file_location("atomic_under_test", MODULE)
atomic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(atomic)
class AtomicTests(unittest.TestCase):
    def test_explicit_private_mode_repairs_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            atomic.write_text(path, "one", mode=0o600)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            path.chmod(0o644)
            atomic.write_bytes(path, b"two", mode=0o600)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.read_bytes(), b"two")
    def test_default_mode_preserves_existing_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            atomic.write_text(path, "one")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
            path.chmod(0o640)
            atomic.write_text(path, "two")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
    def test_failure_keeps_original_and_cleans_scratch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("original")
            with mock.patch.object(atomic.os, "replace", side_effect=OSError("fixture")):
                with self.assertRaises(OSError):
                    atomic.write_text(path, "replacement", mode=0o600)
            self.assertEqual(path.read_text(), "original")
            self.assertEqual(list(Path(tmp).iterdir()), [path])
    def test_invalid_modes_fail_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            for mode in (-1, 0o1000, "600"):
                with self.assertRaises(ValueError):
                    atomic.write_text(path, "replacement", mode=mode)
            self.assertEqual(list(Path(tmp).iterdir()), [])
if __name__ == "__main__":
    unittest.main()
