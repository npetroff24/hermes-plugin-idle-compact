"""Guard от двойного сжатия: ядро сжало — таймер пропускает."""
import time
import unittest
from pathlib import Path
from _bootstrap import load_plugin_root

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def load_init():
    return load_plugin_root("ic_under_test")


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

    def test_apply_core_size_caps_only_measurement_before_compaction(self):
        """Замер, сделанный ДО сжатия, подменяем; сделанный после — нет."""
        self.mod._on_post_compaction(session_id="s7", after_tokens=50000)
        rec = self.mod._core_record("s7")
        assert rec is not None
        compacted_at = rec[0]
        # Замер раньше сжатия: подмена работает (ради этого guard и заведён).
        self.assertEqual(
            self.mod._apply_core_size("s7", 300000, measured_at=compacted_at - 1), 50000)
        # Замер позже сжатия: сессия выросла, 50К — это прошлое, не размер.
        self.assertEqual(
            self.mod._apply_core_size("s7", 300000, measured_at=compacted_at + 1), 300000)

    def test_fire_does_not_cap_session_that_grew_after_compaction(self):
        """P1 из ревью: сжатая до 50К и выросшая до 300К сессия идёт в сжатие.

        Без правки гейт видел min(300000, 50000) и скипал сессию до конца
        жизни записи о сжатии (четыре часа).
        """
        closed = []
        compaction = self.mod._on_post_compaction
        compaction(session_id="s-grown", after_tokens=50000)
        rec = self.mod._core_record("s-grown")
        assert rec is not None
        compacted_at = rec[0]
        grew_at = compacted_at + 5

        class Comp:
            context_length = 1_000_000
            last_prompt_tokens = 300000

        self.mod._session_row = (  # type: ignore[attr-defined]
            lambda sid: {"id": sid, "source": "gui", "last_activity_at": grew_at})
        self.mod._resident = lambda sid: (object(), Comp(), "route", object())  # type: ignore[attr-defined]
        self.mod._gate_floor = lambda comp, sid=None: 100000  # type: ignore[attr-defined]
        self.mod._estimate_history = lambda comp, sid: None  # type: ignore[attr-defined]
        self.mod._note_window = lambda model, window: None  # type: ignore[attr-defined]
        self.mod._note_usage = lambda sid, tokens: None  # type: ignore[attr-defined]
        self.mod._do_compress = lambda sid, model, size, ctx, **kw: closed.append(size)  # type: ignore[attr-defined]
        self.mod._forget = lambda sid: None  # type: ignore[attr-defined]

        self.mod._fire("s-grown", grew_at, "m")
        self.assertEqual(closed, [300000], "сжатие обязано видеть реальные 300000")

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
