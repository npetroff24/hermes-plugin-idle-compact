"""Страж «единого источника»: времена заданы в policy.py, а не по файлам.

Тест читает исходники как AST (не как текст): числа в докстрингах, тестовых
данных и версии плагина не считаются, в отличие от литералов в коде.
"""

from __future__ import annotations

import ast
import importlib
import sys
import types
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]

# Значения времени жизни: жить они должны только в policy.py и model_ttl.py.
BANNED = {600, 7200, 3600, 600.0, 7200.0, 3600.0}
ALLOWED_FILES = {"policy.py", "model_ttl.py"}


def _policy():
    """Загрузить policy.py как часть синтетического пакета (нужны relative imports)."""
    name = "idle_compact_pkg"
    if name not in sys.modules:
        pkg = types.ModuleType(name)
        pkg.__path__ = [str(PLUGIN_DIR)]  # type: ignore[attr-defined]
        sys.modules[name] = pkg
    return importlib.import_module(f"{name}.policy")


class TestNoScatteredTimeLiterals(unittest.TestCase):
    def test_time_literals_only_in_policy(self):
        offenders: list[str] = []
        for path in sorted(PLUGIN_DIR.glob("*.py")):
            if path.name in ALLOWED_FILES:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and not isinstance(node.value, bool):
                    if isinstance(node.value, (int, float)) and node.value in BANNED:
                        offenders.append(f"{path.name}:{node.lineno} = {node.value!r}")
        self.assertEqual(offenders, [], "литералы времени вне policy.py: " + ", ".join(offenders))

    def test_client_has_no_module_timeout_constant(self):
        """Таймаут приходит параметром из policy, а не константой клиента."""
        src = (PLUGIN_DIR / "warm_client.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        assigned = {
            t.id
            for node in tree.body if isinstance(node, ast.Assign)
            for t in node.targets if isinstance(t, ast.Name)
        }
        self.assertNotIn("TIMEOUT", assigned)


class TestPolicyValues(unittest.TestCase):
    def test_arm_window_follows_model_ttl(self):
        p = _policy()
        self.assertAlmostEqual(p.arm_window_for("deepseek-v4.1-flash"), 5100 * p.TTL_FACTOR)
        self.assertAlmostEqual(p.arm_window_for("claude-sonnet"), 300 * p.TTL_FACTOR)

    def test_capture_age_matches_provider_ttl(self):
        p = _policy()
        self.assertEqual(p.capture_max_age_for("deepseek-v4.1-flash"), 5100.0)
        self.assertEqual(p.capture_max_age_for("muse-spark-1.3-contributor"), 600.0)

    def test_schedule_delay_is_capped_by_eviction(self):
        p = _policy()
        self.assertAlmostEqual(p.schedule_delay_for("deepseek-v4.1-flash", 3600.0), 3600.0 * p.TTL_FACTOR)
        self.assertAlmostEqual(p.schedule_delay_for("claude-sonnet", 3600.0), 300 * p.TTL_FACTOR)

    def test_watchdog_grace_has_named_floor(self):
        p = _policy()
        self.assertEqual(p.watchdog_grace(10.0), p.WATCHDOG_MIN_GRACE_S)
        self.assertAlmostEqual(p.watchdog_grace(5000.0), 10000.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
