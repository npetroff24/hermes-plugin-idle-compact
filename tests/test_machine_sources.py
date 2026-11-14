"""Чёрный список машинных сессий и единый YAML-хелпер.

Перевёрнутая логика: работаем для всех, кроме заведомо машинных источников.
Белый список отставал от ядра и молча пропускал gui, discord, slack и др.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def load_init():
    spec = importlib.util.spec_from_file_location("ic_under_test_ms", PLUGIN_DIR / "__init__.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ic_under_test_ms"] = mod
    spec.loader.exec_module(mod)
    return mod


REQUIRED_MACHINE = {
    "subagent", "oneshot", "tool", "cron", "kanban", "batch",
    "curator", "a2a", "api_server", "webhook", "bot_room",
}


class TestMachineSources(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_init()

    def test_list_covers_core_machine_sources(self):
        """Список не «похудел» молча: обязательные машинные источники на месте."""
        present = set(self.mod.MACHINE_SOURCES)
        self.assertTrue(
            REQUIRED_MACHINE <= present,
            f"потеряны источники: {REQUIRED_MACHINE - present}",
        )

    def test_list_is_fixed_order_tuple(self):
        """Кортеж фиксированного порядка — из него же строится SQL."""
        self.assertIsInstance(self.mod.MACHINE_SOURCES, tuple)

    def test_machine_detected(self):
        m = self.mod
        for src in ("subagent", "oneshot", "cron", "kanban", "tool", "batch"):
            self.assertTrue(m._is_machine_source(src), src)

    def test_human_and_future_platforms_pass(self):
        """Человеческие и неизвестные будущие платформы — не машинные."""
        m = self.mod
        for src in ("telegram", "desktop", "discord", "gui", "slack", "signal",
                    "local", "cli", "zzz_future", "", None):
            self.assertFalse(m._is_machine_source(src), repr(src))

    def test_sql_filter_is_parameterized(self):
        """Фильтр — параметризованный NOT IN из той же константы, с COALESCE."""
        flt, params = self.mod._human_filter_sql()
        self.assertIn("COALESCE(source,'') NOT IN", flt)
        self.assertEqual(flt.count("?"), len(self.mod.MACHINE_SOURCES))
        self.assertEqual(tuple(params), tuple(self.mod.MACHINE_SOURCES))
        self.assertNotIn("discord", flt)  # литералов платформ в SQL нет


class TestTimerGating(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_init()

    def _no_timer_for(self, source):
        m = self.mod
        with mock.patch.object(m, "_session_row",
                               return_value={"source": source, "model": "x"}):
            with mock.patch.object(m.threading, "Timer") as timer:
                m._on_session_end(session_id="s-" + str(source))
        return timer

    def test_machine_session_arms_no_timer(self):
        for src in ("subagent", "cron", "tool", "oneshot", "kanban"):
            timer = self._no_timer_for(src)
            self.assertFalse(timer.called, f"таймер встал для {src}")

    def test_human_and_unknown_arm_timer(self):
        m = self.mod
        for src in ("discord", "gui", "telegram", "zzz_future"):
            with mock.patch.object(m, "_session_row",
                                   return_value={"source": src, "model": "x"}):
                with mock.patch.object(m.threading, "Timer") as timer:
                    with mock.patch.object(m, "_delay_for_model", return_value=60):
                        m._on_session_end(session_id="s-" + str(src))
            self.assertTrue(timer.called, f"таймер НЕ встал для {src}")


class TestScanCandidates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_init()

    def test_machine_excluded_discord_included(self):
        m = self.mod
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "state.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE sessions (id TEXT, source TEXT, model TEXT, "
                    "message_count INT, last_activity_at REAL, archived INT, ended_at INT)")
        con.execute("CREATE TABLE messages (content TEXT)")
        con.executemany(
            "INSERT INTO sessions VALUES (?, ?, 'm', 1, 1.0, 0, NULL)",
            [("s-sub", "subagent"), ("s-disc", "discord"), ("s-cron", "cron")],
        )
        con.commit()
        con.close()
        with mock.patch.object(m, "_state_db", return_value=db):
            with mock.patch.object(m, "_rough_tokens", return_value=10 ** 9):
                out = m.scan_candidates()
        ids = {r["id"] for r in out}
        self.assertIn("s-disc", ids)
        self.assertNotIn("s-sub", ids)
        self.assertNotIn("s-cron", ids)


class TestYamlHelper(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_init()

    def _mod_with_home(self, text=None):
        m = load_init()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        if text is not None:
            (Path(tmp.name) / "config.yaml").write_text(text, encoding="utf-8")
        m._home = lambda: Path(tmp.name)  # type: ignore[attr-defined]
        return m

    def test_read_config_parses_yaml(self):
        m = self._mod_with_home("compression:\n  threshold: 0.5\n")
        self.assertEqual(m._read_config().get("compression"), {"threshold": 0.5})

    def test_read_config_missing_file_is_empty(self):
        m = self._mod_with_home(None)
        self.assertEqual(m._read_config(), {})

    def test_read_config_broken_yaml_is_empty(self):
        m = self._mod_with_home("compression: [unclosed\n")
        self.assertEqual(m._read_config(), {})

    def test_prefers_hermes_yaml_over_pyyaml(self):
        """hermes_yaml первый: подсовываем фейковый, PyYAML прячем."""
        import builtins
        m = self.mod
        fake = mock.Mock()
        fake.safe_load.return_value = {"compression": {"threshold": 0.1}}
        real_import = builtins.__import__

        def _fake_import(name, *a, **k):
            if name == "hermes_yaml":
                return fake
            if name == "yaml":
                raise ImportError("спрятан для теста")
            return real_import(name, *a, **k)

        with mock.patch.object(builtins, "__import__", side_effect=_fake_import):
            # _yaml_module делает plain import внутри try — ловим через sys.modules
            with mock.patch.dict(sys.modules, {"hermes_yaml": fake}):
                with mock.patch.dict(sys.modules):
                    sys.modules.pop("yaml", None)
                    got = m._yaml_module()
        self.assertIs(got, fake)

    def test_no_yaml_at_all_returns_empty(self):
        """Нет ни hermes_yaml, ни yaml — читаем {} без исключения."""
        import builtins
        m = self._mod_with_home("a: 1\n")
        real_import = builtins.__import__

        def _no_yaml(name, *a, **k):
            if name in ("hermes_yaml", "yaml"):
                raise ImportError(f"нет {name}")
            return real_import(name, *a, **k)

        with mock.patch.object(builtins, "__import__", side_effect=_no_yaml):
            with mock.patch.dict(sys.modules):
                sys.modules.pop("hermes_yaml", None)
                sys.modules.pop("yaml", None)
                self.assertEqual(m._read_config(), {})

    def test_yaml11_parity_on_off(self):
        """YAML 1.1: `on` читается как True — как в PyYAML, так и в фасаде."""
        m = self._mod_with_home("flag: on\n")
        self.assertIs(m._read_config().get("flag"), True)

    def test_plugin_version_still_reads_plugin_yaml(self):
        """Версия берётся из plugin.yaml, а не из конфига."""
        m = load_init()
        self.assertRegex(m._plugin_version(), r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
