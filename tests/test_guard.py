"""Guard от двойного сжатия: ядро сжало — таймер пропускает."""
import importlib.util
import sys
import time
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def load_init():
    spec = importlib.util.spec_from_file_location("ic_under_test", PLUGIN_DIR / "__init__.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ic_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestPostCompactionGuard(unittest.TestCase):
    def setUp(self):
        self.mod = load_init()
        with self.mod._core_compactions_lock:
            self.mod._core_compactions.clear()

    def test_handler_records_session(self):
        self.mod._on_post_compaction(session_id="s1", after_tokens=50000,
                                     reason="cache_expiry")
        rec = self.mod._core_record("s1")
        self.assertIsNotNone(rec)
        assert rec is not None
        self.assertLess(abs(rec[0] - time.time()), 5)
        self.assertEqual(rec[1], 50000)

    def test_handler_records_old_session_too(self):
        self.mod._on_post_compaction(session_id="child", old_session_id="parent",
                                     after_tokens=1000, reason="threshold")
        self.assertIsNotNone(self.mod._core_record("child"))
        self.assertIsNotNone(self.mod._core_record("parent"))

    def test_handler_ignores_empty(self):
        self.mod._on_post_compaction()
        self.mod._on_post_compaction(session_id=None, old_session_id="")
        with self.mod._core_compactions_lock:
            self.assertEqual(self.mod._core_compactions, {})

    def test_stale_record_dropped(self):
        with self.mod._core_compactions_lock:
            self.mod._core_compactions["old"] = (
                time.time() - self.mod._CORE_COMPACTION_TTL_S - 1, 10)
        self.assertIsNone(self.mod._core_record("old"))
        with self.mod._core_compactions_lock:
            self.assertNotIn("old", self.mod._core_compactions)

    def test_apply_core_size_takes_min(self):
        self.mod._on_post_compaction(session_id="s2", after_tokens=10000)
        self.assertEqual(self.mod._apply_core_size("s2", 100000), 10000)

    def test_apply_core_size_no_record(self):
        self.assertEqual(self.mod._apply_core_size("nope", 100000), 100000)

    def test_fire_skips_when_core_compacted_after_arm(self):
        calls = []
        self.mod._session_row = lambda sid: calls.append(sid) or {"id": sid}  # type: ignore[attr-defined]
        stamp = time.time() - 10
        self.mod._on_post_compaction(session_id="s3", after_tokens=1000)
        self.mod._fire("s3", stamp, "m")
        self.assertEqual(calls, [])

    def test_fire_proceeds_when_record_older_than_stamp(self):
        calls = []
        self.mod._session_row = lambda sid: calls.append(sid) or None  # type: ignore[attr-defined]
        self.mod._on_post_compaction(session_id="s4", after_tokens=1000)
        time.sleep(0.01)
        self.mod._fire("s4", time.time(), "m")
        self.assertEqual(calls, ["s4"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
