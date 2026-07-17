"""The shared mkdir lock never steals a live owner's lock and reclaims dead owners."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK_LIB = ROOT / "bin" / "lock-lib.sh"


class LockingTest(unittest.TestCase):
    def _run(self, lockdir: Path):
        script = (
            f'LOCKDIR="{lockdir}"; LOCK_LABEL=test; LOCK_WAIT_TRIES=1; '
            f'source "{LOCK_LIB}"; with_lock bash -c "exit 0"'
        )
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    def test_live_owner_is_not_stolen(self):
        with tempfile.TemporaryDirectory() as tmp:
            lockdir = Path(tmp) / "lock.d"
            lockdir.mkdir()
            owner = f"{os.getpid()}:live-token\n"
            (lockdir / "owner").write_text(owner, encoding="utf-8")

            result = self._run(lockdir)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("lock 等待超时", result.stderr)
            self.assertEqual((lockdir / "owner").read_text(encoding="utf-8"), owner)

    def test_dead_owner_is_reclaimed_and_own_lock_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            lockdir = Path(tmp) / "lock.d"
            lockdir.mkdir()
            (lockdir / "owner").write_text("99999999:dead-token\n", encoding="utf-8")

            result = self._run(lockdir)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(lockdir.exists())


if __name__ == "__main__":
    unittest.main()
