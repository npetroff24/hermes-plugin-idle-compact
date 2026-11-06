"""Задержка таймера = min(TTL модели, TTL выселения агента) x 0.9."""
import importlib.util
import sys
import tempfile
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
        # min(5100, 3600) x 0.9 = 3240: раньше выселения, позже старого 2970 — честно.
        self.assertAlmostEqual(mod._delay_for_model("deepseek-v4.1-flash"), 3240.0)

    def test_raised_evict_unlocks_deepseek(self):
        mod = self._mod_with_config("agent:\n  agent_cache:\n    idle_ttl_secs: 5400\n")
        self.assertEqual(mod._evict_ttl(), 5400.0)
        # min(5100, 5400) x 0.9 = 4590: полный TTL провайдера.
        self.assertAlmostEqual(mod._delay_for_model("deepseek-v4.1-flash"), 4590.0)

    def test_short_models_unaffected(self):
        mod = self._mod_with_config("agent:\n  agent_cache:\n    idle_ttl_secs: 5400\n")
        # min(600, 5400) x 0.9 = 540: короткие модели не меняются.
        self.assertAlmostEqual(mod._delay_for_model("muse-spark-1.3-contributor"), 540.0)

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


if __name__ == "__main__":
    unittest.main()
