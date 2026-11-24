"""Задержка таймера = min(TTL модели, TTL выселения агента) минус 30 с."""
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock
from _bootstrap import load_plugin_root

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def load_init():
    return load_plugin_root("ic_under_test")


class TestEvictCap(unittest.TestCase):
    """Планировщик упирается в выселение агента раньше TTL провайдера."""

    def _mod_with_config(self, text: str):
        mod = load_init()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "config.yaml").write_text(text, encoding="utf-8")
        mod._home = lambda: Path(tmp.name)  # type: ignore[attr-defined]
        return mod

    def test_default_evict_caps_deepseek(self):
        mod = self._mod_with_config("compression:\n  threshold_tokens: 100000\n")
        self.assertEqual(mod._evict_ttl(), 3600.0)
        # min(5100, 3600) - 30 = 3570: раньше выселения, позже старого 3240.
        self.assertAlmostEqual(mod._delay_for_model("deepseek-v4.1-flash"), 3570.0)

    def test_raised_evict_unlocks_deepseek(self):
        mod = self._mod_with_config("agent:\n  agent_cache:\n    idle_ttl_secs: 5400\n")
        self.assertEqual(mod._evict_ttl(), 5400.0)
        # min(5100, 5400) - 30 = 5070: полный TTL провайдера минус запас.
        self.assertAlmostEqual(mod._delay_for_model("deepseek-v4.1-flash"), 5070.0)

    def test_short_models_unaffected(self):
        mod = self._mod_with_config("agent:\n  agent_cache:\n    idle_ttl_secs: 5400\n")
        # min(600, 5400) - 30 = 570.
        self.assertAlmostEqual(mod._delay_for_model("muse-spark-1.3-contributor"), 570.0)

    def test_broken_config_falls_back(self):
        mod = self._mod_with_config("agent: [unclosed\n")
        self.assertEqual(mod._evict_ttl(), 3600.0)

    def test_evict_cap_warns_once(self):
        mod = self._mod_with_config("compression:\n  threshold_tokens: 100000\n")
        with self.assertLogs("ic_under_test", level="WARNING") as logs:
            mod._delay_for_model("deepseek-v4.1-flash")
        self.assertTrue(
            any("idle_ttl_secs" in line for line in logs.output),
            logs.output,
        )
        # Второй вызов молчит — подсказка одноразовая.
        with self.assertNoLogs("ic_under_test", level="WARNING"):
            mod._delay_for_model("deepseek-v4.1-flash")


class _SyncThread:
    """Поток-заглушка: работа сжатия выполняется в тесте сразу."""

    def __init__(self, target=None, **kw):
        self._target = target

    def start(self):
        if self._target is not None:
            self._target()


class TestCompressSuccessFlag(unittest.TestCase):
    """Флаг успеха сжатия доходит до guard'а выселения (nonlocal в run()).

    Без nonlocal запись уходила в локальную переменную run(), внешний
    did_compress оставался False, и агент после успешного сжатия не
    выселялся никогда.
    """

    def _mod(self):
        mod = load_init()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "config.yaml").write_text("other: 1" + chr(10), encoding="utf-8")
        mod._home = lambda: Path(tmp.name)  # type: ignore[attr-defined]
        mod._gate_floor = lambda comp, sid=None: 1  # type: ignore[attr-defined]
        mod._capture_for = lambda sid: None  # type: ignore[attr-defined]
        return mod

    def _agent(self, aborted=False):
        history = [{"role": "user", "content": "x" * 40},
                   {"role": "assistant", "content": "y" * 40}]

        class Comp:
            awaiting_real_usage_after_compression = False
            _compression_skipped_due_to_lock = False
            context_length = 1_000_000

            class _DB:
                @staticmethod
                def get_messages_as_conversation(sid):
                    return history

            _session_db = _DB()

        agent = types.SimpleNamespace(
            compression_enabled=True,
            api_mode="chat_completions",
            compression_checkpoint_required=False,
            context_compressor=Comp(),
            _last_compress_aborted=aborted,
            _compress_context=lambda messages, text, **kw: None,
        )
        return agent

    def _run(self, mod, agent, runner):
        fake_gw = types.SimpleNamespace(
            _cached_agent_for_hygiene=lambda runner_ref, route: agent,
            _gateway_runner_ref=lambda: runner,
        )
        with mock.patch.dict(sys.modules, {"gateway.run": fake_gw}),                 mock.patch.object(mod.threading, "Thread", _SyncThread):
            mod._do_compress("s-flag", "m", 500000, None,
                             agent=agent, route="route-1", runner=runner)

    def test_successful_compression_evicts_agent(self):
        mod = self._mod()
        evicted = []
        runner = types.SimpleNamespace(_evict_cached_agent=evicted.append)
        self._run(mod, self._agent(), runner)
        self.assertEqual(evicted, ["route-1"],
                         "успешное сжатие обязано выселять агента")

    def test_aborted_compression_keeps_agent(self):
        mod = self._mod()
        evicted = []
        runner = types.SimpleNamespace(_evict_cached_agent=evicted.append)
        self._run(mod, self._agent(aborted=True), runner)
        self.assertEqual(evicted, [], "прерванное сжатие агент не выселяет")


if __name__ == "__main__":
    unittest.main()
