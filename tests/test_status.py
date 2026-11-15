"""Команда /idle-compact: панель статуса, read-only, один парс конфига на хук."""
import importlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def load_init():
    spec = importlib.util.spec_from_file_location("ic_under_test", PLUGIN_DIR / "__init__.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ic_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def load_savings():
    return importlib.import_module("ic_under_test.savings")


class FakeCtx:
    def __init__(self):
        self.manifest = mock.Mock()
        self.manifest.version = "9.9.9-test"
        self.hooks = []
        self.commands = {}

    def register_hook(self, name, fn):
        self.hooks.append(name)

    def register_middleware(self, name, fn):
        pass

    def register_context_engine(self, engine):
        return None  # ядро не приняло — warm останется выключенным

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands[name] = handler
        return None


class StatusBase(unittest.TestCase):
    def _mod_with_home(self, config_text="other: 1\n"):
        mod = load_init()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        (home / "config.yaml").write_text(config_text, encoding="utf-8")
        mod._home = lambda: home  # type: ignore[attr-defined]
        mod._test_settings = lambda *a, **k: {}  # type: ignore[attr-defined]
        load_savings()._home = lambda: home  # type: ignore[attr-defined]
        return mod, home


class TestCommandRegistration(StatusBase):
    def test_register_exposes_idle_compact(self):
        mod, _home = self._mod_with_home()
        ctx = FakeCtx()
        mod.register(ctx)
        self.addCleanup(mod.unregister, None)
        self.assertIn("idle-compact", ctx.commands)
        out = ctx.commands["idle-compact"]("")
        self.assertIn("idle-compact 9.9.9-test", out)
        self.assertIn("warm-движок: нет (этот процесс)", out)


class TestPanelReadOnly(StatusBase):
    def _home_with_state(self):
        mod, home = self._mod_with_home(
            "compression:\n  threshold_tokens: 100000\n  target_ratio: 0.5\n"
        )
        now = time.time()
        pending = {
            "s-live": {"pid": os.getpid(), "deadline": now + 600, "stamp": now, "model": "m"},
            "s-dead": {"pid": 999999999, "deadline": now + 600, "stamp": now, "model": "m"},
            "s-expired": {"pid": os.getpid(), "deadline": now - 60, "stamp": now - 600, "model": "m"},
            "s-junk": {"pid": "xx", "deadline": "soon", "model": "x"},
        }
        (home / "idle_compact_pending.json").write_text(
            json.dumps(pending), encoding="utf-8")
        ledger = [
            {"event": "settle", "saved_usd": 0.01, "ts": 1794728400.0},
            {"event": "settle", "saved_usd": -0.003, "ts": 1794728410.0},
            {"event": "settle", "saved_usd": 0.005, "ts": 1794728420.0},
        ]
        (home / "idle_compact_ledger.jsonl").write_text(
            "\n".join(json.dumps(e) for e in ledger) + "\n", encoding="utf-8")
        return mod, home

    def test_files_and_timers_untouched(self):
        mod, home = self._home_with_state()
        before_pending = (home / "idle_compact_pending.json").read_bytes()
        before_ledger = (home / "idle_compact_ledger.jsonl").read_bytes()
        out = mod._cmd_status("")
        self.assertIn("idle-compact", out)
        self.assertEqual((home / "idle_compact_pending.json").read_bytes(), before_pending)
        self.assertEqual((home / "idle_compact_ledger.jsonl").read_bytes(), before_ledger)
        self.assertEqual(dict(mod._timers), {})

    def test_phantom_filter(self):
        mod, home = self._home_with_state()
        con = sqlite3.connect(str(home / "state.db"))
        con.execute(
            "CREATE TABLE sessions (id TEXT, source TEXT, model TEXT, session_key TEXT,"
            " message_count INT, api_call_count INT, archived INT, ended_at INT,"
            " last_activity_at INT)"
        )
        # Строки есть у s-live и s-expired; у s-ghost нет — он отсекается
        # третьим фактором (переиспользованный pid + будущий дедлайн).
        con.execute("INSERT INTO sessions (id) VALUES ('s-live'), ('s-expired')")
        con.commit()
        con.close()
        pending = json.loads((home / "idle_compact_pending.json").read_text(encoding="utf-8"))
        pending["s-ghost"] = {"pid": os.getpid(), "deadline": time.time() + 600,
                              "stamp": time.time(), "model": "m"}
        (home / "idle_compact_pending.json").write_text(json.dumps(pending), encoding="utf-8")
        out = mod._cmd_status("")
        self.assertIn("наблюдение (файл): 1 живых из 5", out)

    def test_signed_money(self):
        mod, _home = self._home_with_state()
        out = mod._cmd_status("")
        self.assertIn("событий 3, в плюс 2 (+$0.01500), зря 1 (-$0.00300), итог $+0.01200", out)
        self.assertIn("последний счёт: ", out)
        self.assertNotIn("пока нет", out)

    def test_no_settle_note(self):
        mod, home = self._home_with_state()
        (home / "idle_compact_ledger.jsonl").write_text(
            json.dumps({"event": "begin", "saved_usd": 0}) + "\n", encoding="utf-8")
        out = mod._cmd_status("")
        self.assertIn("последний счёт: пока нет", out)

    def test_gate_in_tokens(self):
        mod, _home = self._home_with_state()
        out = mod._cmd_status("")
        self.assertIn("порог: ~100000 токенов", out)

    def test_gate_fraction_fallback(self):
        mod, home = self._mod_with_home(
            "compression:\n  threshold: 0.5\n  target_ratio: 0.2\n"
            "plugins:\n  idle_compact:\n    gate_fraction: 0.35\n"
        )
        out = mod._cmd_status("")
        self.assertIn("порог: 0.35 окна модели (окно неизвестно)", out)

    def test_current_size_from_db(self):
        mod, home = self._home_with_state()
        con = sqlite3.connect(str(home / "state.db"))
        con.execute("CREATE TABLE messages (session_id TEXT, content TEXT, active INT)")
        con.execute("INSERT INTO messages VALUES ('s1', ?, 1)", ("x" * 4000,))
        con.commit()
        con.close()
        out = mod._cmd_status("", session_id="s1")
        self.assertIn("сейчас: ~1000 токенов", out)


class TestSingleParse(StatusBase):
    def test_hook_reads_config_once(self):
        mod, _home = self._mod_with_home(
            "compression:\n  threshold_tokens: 100000\n  target_ratio: 0.5\n"
        )
        self.addCleanup(mod.unregister, None)
        real_read = mod._read_config
        calls = []
        mod._read_config = lambda: calls.append(1) or real_read()  # type: ignore[attr-defined]
        with mock.patch.object(mod, "_session_row",
                               return_value={"source": "gui", "model": "m"}):
            mod._on_session_end(session_id="s-x")
        self.assertEqual(len(calls), 1)

    def test_explicit_cfg_never_touches_disk(self):
        mod, _home = self._mod_with_home()
        mod._read_config = mock.Mock(side_effect=AssertionError("disk read"))  # type: ignore[attr-defined]
        cfg = {"compression": {"threshold_tokens": 100000, "target_ratio": 0.5}}
        self.assertEqual(mod._live_gate(cfg=cfg), 100000)
        self.assertAlmostEqual(mod._evict_ttl(cfg), mod.policy.AGENT_CACHE_IDLE_TTL_DEFAULT_S)


if __name__ == "__main__":
    unittest.main()
