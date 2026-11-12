"""Гейт и порог: хаб настроек, ручной ключ, дефолты ядра."""
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


class TestHubPrecedence(unittest.TestCase):
    def _mod_with_config(self, text):
        mod = load_init()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "config.yaml").write_text(text, encoding="utf-8")
        mod._home = lambda: Path(tmp.name)  # type: ignore[attr-defined]
        mod._test_settings = lambda *a, **k: {}  # type: ignore[attr-defined]
        return mod

    def test_hub_wins_over_manual(self):
        mod = self._mod_with_config(
            "plugins:\n"
            "  entries:\n"
            "    idle-compact:\n"
            "      settings:\n"
            "        auto_compact_ratio: 0.3\n"
            "  idle_compact:\n"
            "    gate_fraction: 0.1\n"
        )
        self.assertAlmostEqual(mod._gate_fraction(), 0.3)

    def test_manual_when_no_hub(self):
        mod = self._mod_with_config("plugins:\n  idle_compact:\n    gate_fraction: 0.1\n")
        self.assertAlmostEqual(mod._gate_fraction(), 0.1)

    def test_zero_when_nothing(self):
        mod = self._mod_with_config("other: 1\n")
        self.assertAlmostEqual(mod._gate_fraction(), 0.0)


class TestKernelDefaults(unittest.TestCase):
    def _mod_with_config(self, text):
        mod = load_init()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "config.yaml").write_text(text, encoding="utf-8")
        mod._home = lambda: Path(tmp.name)  # type: ignore[attr-defined]
        return mod

    def test_default_target_big_window(self):
        """Без секции — 0.50 окна × 0.2: окно 1М даёт цель 100К."""
        mod = self._mod_with_config("other: 1\n")
        self.assertEqual(mod._live_target(window_tokens=1_048_576), 104_857)

    def test_default_floor_small_window(self):
        """Окно <512К — пол 0.75: 200К × 0.75 × 0.2 = 30К."""
        mod = self._mod_with_config("other: 1\n")
        self.assertEqual(mod._live_target(window_tokens=200_000), 30_000)

    def test_file_still_wins(self):
        mod = self._mod_with_config(
            "compression:\n  threshold_tokens: 500000\n  target_ratio: 0.1\n"
        )
        self.assertEqual(mod._live_target(window_tokens=1_048_576), 50_000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
