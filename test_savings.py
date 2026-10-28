"""Тесты savings.py: цены, фактические счётчики, расчёт экономии.

Запуск:  python -m unittest discover -s tests -v   (из каталога плагина)
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

import importlib.util

_spec = importlib.util.spec_from_file_location("savings_under_test", PLUGIN_DIR / "savings.py")
assert _spec is not None and _spec.loader is not None, "cannot load savings.py"
savings = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(savings)


def _make_db(path: Path, rows: list[tuple]) -> None:
    """Мини-копия session_model_usage: (session_id, model, task, input, cache_read, output, calls)."""
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE session_model_usage ("
        "session_id TEXT, model TEXT, task TEXT, input_tokens INT, "
        "cache_read_tokens INT, output_tokens INT, api_call_count INT)"
    )
    con.executemany(
        "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?)", rows
    )
    con.commit()
    con.close()


class TestPrices(unittest.TestCase):
    def test_muse_prices(self):
        p = savings.price_for("muse-spark-1.3-contributor")
        self.assertEqual(p["fresh"], 0.10)
        self.assertEqual(p["read"], 0.002)
        self.assertEqual(p["out"], 0.20)

    def test_deepseek_longest_match(self):
        """deepseek-v4.1 должен победить deepseek-v4 — иначе цены разъедутся."""
        p = savings.price_for("deepseek-v4.1-flash")
        self.assertEqual(p["fresh"], 0.30)

    def test_deepseek_plain(self):
        p = savings.price_for("deepseek-v4-flash")
        self.assertEqual(p["fresh"], 0.14)

    def test_unknown_falls_back(self):
        self.assertEqual(savings.price_for(""), savings._FALLBACK)
        self.assertEqual(savings.price_for("gpt-6-luna"), savings._FALLBACK)

    def test_cost_of(self):
        # 100K по $0.10/1M = $0.01
        self.assertAlmostEqual(savings.cost_of(100_000, "fresh", "muse-spark"), 0.01, places=9)
        # 100K по $0.002/1M = $0.0002
        self.assertAlmostEqual(savings.cost_of(100_000, "read", "muse-spark"), 0.0002, places=9)

    def test_cost_of_negative_is_zero(self):
        self.assertEqual(savings.cost_of(-5, "fresh", "muse-spark"), 0.0)


class TestFacts(unittest.TestCase):
    def test_cost_of_facts(self):
        facts = {"input": 10_000, "cache_read": 500_000, "output": 2_000}
        p = savings._FALLBACK
        expected = (
            10_000 / 1e6 * p["fresh"]
            + 500_000 / 1e6 * p["read"]
            + 2_000 / 1e6 * p["out"]
        )
        self.assertAlmostEqual(savings.cost_of_facts(facts, "muse-spark"), expected, places=12)

    def test_delta(self):
        before = {"input": 100, "cache_read": 50, "output": 10, "calls": 1}
        after = {"input": 900, "cache_read": 650, "output": 60, "calls": 3}
        d = savings.delta(before, after)
        self.assertEqual(d, {"input": 800, "cache_read": 600, "output": 50, "calls": 2})

    def test_delta_never_negative(self):
        """Сброс счётчиков (новая строка usage) не должен давать минус."""
        before = {"input": 900, "cache_read": 650, "output": 60, "calls": 3}
        after = {"input": 100, "cache_read": 50, "output": 10, "calls": 1}
        d = savings.delta(before, after)
        self.assertEqual(d, {"input": 0, "cache_read": 0, "output": 0, "calls": 0})


class TestCompressionCounters(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.db"
        _make_db(
            self.db,
            [
                ("s1", "muse-spark", "compression", 4_000, 30_000, 900, 1),
                ("s1", "muse-spark", "background_review", 100_000, 500_000, 1_000, 5),
                ("s1", "muse-spark", "compression", 1_000, 0, 300, 1),
                ("s2", "muse-spark", "compression", 999, 999, 999, 1),
            ],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_sums_only_compression_for_session(self):
        f = savings.compression_counters("s1", db_path=self.db)
        self.assertEqual(f["input"], 5_000)
        self.assertEqual(f["cache_read"], 30_000)
        self.assertEqual(f["output"], 1_200)
        self.assertEqual(f["calls"], 2)

    def test_other_session_not_mixed(self):
        f = savings.compression_counters("s1", db_path=self.db)
        self.assertNotEqual(f["input"], 5_999)

    def test_missing_session_is_zero(self):
        f = savings.compression_counters("nope", db_path=self.db)
        self.assertEqual(f, {"input": 0, "cache_read": 0, "output": 0, "calls": 0})

    def test_missing_db_is_zero(self):
        f = savings.compression_counters("s1", db_path=Path(self.tmp.name) / "absent.db")
        self.assertEqual(f["input"], 0)


class TestComputeSaving(unittest.TestCase):
    def test_cold_case_is_positive(self):
        """Кеш умер бы: сжатие с тёплым чтением дешевле холодного."""
        facts = {"input": 0, "cache_read": 125_000, "output": 1_000, "calls": 1}
        saved, verdict, cost = savings.compute_saving(
            n_before=225_000, n_after=100_000, facts=facts,
            gap_s=3600, ttl_s=600, model="muse-spark",
        )
        # baseline 225K × 0.10/1M = 0.0225; cost = 125K×0.002/1M + 1K×0.20/1M
        self.assertAlmostEqual(cost, 0.00025 + 0.0002, places=9)
        self.assertAlmostEqual(saved, 0.0225 - 0.00045, places=9)
        self.assertIn("холодный", verdict)
        self.assertGreater(saved, 0)

    def test_warm_case_can_be_negative(self):
        """Вернулся раньше TTL — плагин потратил лишнее."""
        facts = {"input": 0, "cache_read": 125_000, "output": 1_000, "calls": 1}
        saved, verdict, cost = savings.compute_saving(
            n_before=225_000, n_after=100_000, facts=facts,
            gap_s=120, ttl_s=600, model="muse-spark",
        )
        # baseline 225K × 0.002/1M = 0.00045 < cost 0.00045 → около нуля/минус
        self.assertIn("тёплым", verdict)
        self.assertLessEqual(saved, 0.00045)

    def test_cold_case_without_cache_read_is_worse(self):
        """Если факт показывает чтение без кеша — экономии почти нет."""
        cold_facts = {"input": 125_000, "cache_read": 0, "output": 1_000, "calls": 1}
        saved, _, cost = savings.compute_saving(
            n_before=225_000, n_after=100_000, facts=cold_facts,
            gap_s=3600, ttl_s=600, model="muse-spark",
        )
        self.assertAlmostEqual(cost, 0.0125 + 0.0002, places=9)
        self.assertAlmostEqual(saved, 0.0225 - 0.0127, places=9)

    def test_ttl_boundary_is_cold(self):
        facts = {"input": 0, "cache_read": 100_000, "output": 100, "calls": 1}
        _, verdict, _ = savings.compute_saving(
            n_before=200_000, n_after=100_000, facts=facts,
            gap_s=600, ttl_s=600, model="muse-spark",
        )
        self.assertIn("холодный", verdict)

    def test_ttl_for_models(self):
        self.assertEqual(savings.ttl_seconds("deepseek-v4.1-flash"), 5100)
        self.assertEqual(savings.ttl_seconds("muse-spark-1.3-contributor"), 600)


class TestLifecycle(unittest.TestCase):
    """begin -> end -> живой вызов: сквозной учёт на подставной базе."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.db"
        _make_db(self.db, [])
        self._orig_db = savings.state_db_path
        savings.state_db_path = lambda: self.db  # type: ignore[assignment]
        self._orig_ledger = savings.ledger_path
        self.ledger = Path(self.tmp.name) / "ledger.jsonl"
        savings.ledger_path = lambda: self.ledger  # type: ignore[assignment]
        savings._state.clear()
        savings._compacting.clear()

    def tearDown(self):
        savings.state_db_path = self._orig_db  # type: ignore[assignment]
        savings.ledger_path = self._orig_ledger  # type: ignore[assignment]
        savings._state.clear()
        savings._compacting.clear()
        self.tmp.cleanup()

    def test_full_cycle_records_actual_cost(self):
        sid = "sess-1"
        savings.begin_compaction(sid, "muse-spark-1.3-contributor", 225_000)
        # ядро дописало строку aux-вызова: 0 свежих, 125K из кеша, 1K выход
        con = sqlite3.connect(str(self.db))
        con.execute(
            "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?)",
            (sid, "muse-spark", "compression", 0, 125_000, 1_000, 1),
        )
        con.commit()
        con.close()
        savings.end_compaction(sid, after_tokens=100_000)

        pend = savings._state[sid]["pending"]
        self.assertEqual(pend["facts"]["cache_read"], 125_000)
        self.assertGreater(pend["cost_usd"], 0)

        # живой вызов через час: кеш всё равно умер бы
        savings.on_post_api_request(session_id=sid, started_at=pend["ts"] + 3600)
        self.assertIsNone(savings._state[sid]["pending"])
        self.assertEqual(savings._state[sid]["events"], 1)
        self.assertGreater(savings._state[sid]["saved_usd"], 0)

        lines = [json.loads(x) for x in self.ledger.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["session"], sid)
        self.assertIn("холодный", lines[0]["verdict"])

    def test_no_pending_means_no_event(self):
        savings.on_post_api_request(session_id="nothing", started_at=0)
        self.assertEqual(savings.session_summary("nothing"), {})

    def test_abort_clears_window(self):
        savings.begin_compaction("sess-2", "muse-spark", 200_000)
        savings.abort_compaction("sess-2")
        self.assertNotIn("sess-2", savings._compacting)
        savings.end_compaction("sess-2", 100_000)
        self.assertEqual(savings.session_summary("sess-2"), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
