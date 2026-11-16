"""Команда /idle-compact: панель по макету, read-only, один парс на хук."""
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
        return None

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands[name] = handler
        return None

    def register_rpc(self, name, handler):
        self.commands["rpc:" + name] = handler
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

    def _db_with_session(self, home, session_id="s1", model="m", chars=40000):
        con = sqlite3.connect(str(home / "state.db"))
        con.execute(
            "CREATE TABLE sessions (id TEXT, source TEXT, model TEXT, session_key TEXT,"
            " message_count INT, api_call_count INT, archived INT, ended_at INT,"
            " last_activity_at INT)"
        )
        con.execute("INSERT INTO sessions (id, model) VALUES (?, ?)", (session_id, model))
        con.execute("CREATE TABLE messages (session_id TEXT, content TEXT, active INT)")
        con.execute("INSERT INTO messages VALUES (?, ?, 1)", (session_id, "x" * chars))
        con.commit()
        con.close()


class TestCommandRegistration(StatusBase):
    def test_register_exposes_idle_compact(self):
        mod, _home = self._mod_with_home()
        ctx = FakeCtx()
        mod.register(ctx)
        self.addCleanup(mod.unregister, None)
        self.assertIn("idle-compact", ctx.commands)
        out = ctx.commands["idle-compact"]("")
        self.assertTrue(out.startswith("idle-compact 9.9.9-test"))
        self.assertNotIn("warm compaction", out)  # диагностика — только в debug
        dbg = ctx.commands["idle-compact"]("debug")
        self.assertIn("· debug", dbg)
        self.assertIn("warm compaction: off", dbg)


class TestPanelMoney(StatusBase):
    def _home_with_ledger(self):
        mod, home = self._mod_with_home()
        evs = [
            {"ev": "settle", "ts": 1794728400.0, "saved_usd": 0.06,
             "baseline_usd": 0.10, "verdict": "предотвращён холодный вход",
             "facts": {"input": 10, "cache_read": 100, "output": 5, "calls": 1}},
            {"ev": "settle", "ts": 1794728410.0, "saved_usd": -0.01,
             "baseline_usd": 0.02, "verdict": "кеш был бы тёплым",
             "facts": {"input": 10, "cache_read": 100, "output": 5, "calls": 1}},
            {"ev": "settle", "ts": 1794728420.0, "saved_usd": 0.03,
             "baseline_usd": 0.05, "verdict": "предотвращён холодный вход",
             "facts": {"input": 0, "cache_read": 0, "output": 0, "calls": 0}},
            {"ev": "settle", "ts": 1794728390.0, "saved_usd": 0.0,
             "verdict": "предотвращён холодный вход",
             "facts": {"input": 1, "cache_read": 1, "output": 1, "calls": 1}},
        ]
        (home / "idle_compact_ledger.jsonl").write_text(
            "\n".join(json.dumps(e) for e in evs) + "\n", encoding="utf-8")
        return mod, home

    def test_money_percent_breakdown(self):
        mod, _home = self._home_with_ledger()
        out = mod._cmd_status("")
        # net +0.05, base 0.12 → 42%
        self.assertIn("saved: +$0.05 — that's 42% of costs without the plugin", out)
        self.assertIn("paid off 1 · wasted 1 · 1 more not counted (cost not saved)", out)
        self.assertIn("last compaction: ", out)
        self.assertIn("did not pay off", out)

    def test_empty_ledger(self):
        mod, _home = self._mod_with_home()
        out = mod._cmd_status("")
        self.assertIn("compactions counted: 0", out)
        self.assertNotIn("saved:", out)

    def test_no_gloss_without_waste(self):
        mod, home = self._mod_with_home()
        (home / "idle_compact_ledger.jsonl").write_text(
            json.dumps({"ev": "settle", "ts": 1794728400.0, "saved_usd": 0.06,
                        "baseline_usd": 0.10, "verdict": "предотвращён холодный вход",
                        "facts": {"input": 1, "cache_read": 1, "output": 1, "calls": 1}})
            + "\n", encoding="utf-8")
        out = mod._cmd_status("")
        self.assertNotIn("did not pay off", out)


class TestPanelContext(StatusBase):
    def _home_with_all(self, tokens=10000):
        mod, home = self._mod_with_home(
            "plugins:\n  idle_compact:\n    gate_fraction: 0.3\n"
        )
        self._db_with_session(home)
        (home / "idle_compact_windows.json").write_text(
            json.dumps({"m": 1000000}), encoding="utf-8")
        (home / "idle_compact_usage.json").write_text(
            json.dumps({"s1": {"tokens": tokens, "ts": time.time()}}),
            encoding="utf-8")
        return mod, home

    def test_remaining_line(self):
        mod, _home = self._home_with_all()
        out = mod._cmd_status("", session_id="s1")
        self.assertIn("current context: ~10K · ~290K left before compaction (limit ~300K)", out)

    def test_over_gate_line(self):
        mod, _home = self._home_with_all(tokens=300001)
        out = mod._cmd_status("", session_id="s1")
        self.assertIn("over the limit (~300K)", out)

    def test_no_window_no_gate(self):
        mod, home = self._home_with_all()
        (home / "idle_compact_windows.json").unlink()
        out = mod._cmd_status("", session_id="s1")
        self.assertIn("current context: ~10K", out)
        self.assertNotIn("limit", out)

    def test_no_session_no_context_line(self):
        mod, _home = self._home_with_all()
        out = mod._cmd_status("")
        self.assertNotIn("current context", out)

    def test_no_usage_no_context_line(self):
        mod, home = self._home_with_all()
        (home / "idle_compact_usage.json").unlink()
        out = mod._cmd_status("", session_id="s1")
        self.assertNotIn("current context", out)


class TestPanelWatch(StatusBase):
    def _home_with_pending(self):
        mod, home = self._mod_with_home()
        self._db_with_session(home, session_id="s-live", chars=100)
        now = time.time()
        (home / "idle_compact_pending.json").write_text(json.dumps({
            "s-live": {"pid": os.getpid(), "deadline": now + 600,
                       "stamp": now, "model": "m"},
            "s-ghost": {"pid": os.getpid(), "deadline": now + 600,
                        "stamp": now, "model": "m"},
        }), encoding="utf-8")
        return mod, home

    def test_own_session_watched(self):
        mod, _home = self._home_with_pending()
        out = mod._cmd_status("", session_id="s-live")
        self.assertIn("this chat: will compact while idle", out)

    def test_own_session_not_watched(self):
        mod, home = self._home_with_pending()
        con = sqlite3.connect(str(home / "state.db"))
        con.execute("INSERT INTO sessions (id) VALUES ('s-idle')")
        con.commit()
        con.close()
        out = mod._cmd_status("", session_id="s-idle")
        self.assertIn("this chat: will not compact while idle", out)

    def test_no_session_counter(self):
        mod, _home = self._home_with_pending()
        out = mod._cmd_status("")
        self.assertIn("waiting for idle: 1", out)


class TestPanelReadOnly(StatusBase):
    def test_files_and_timers_untouched(self):
        mod, home = self._mod_with_home()
        (home / "idle_compact_pending.json").write_text('{"a": {}}', encoding="utf-8")
        (home / "idle_compact_ledger.jsonl").write_text('{"ev": "x"}\n', encoding="utf-8")
        (home / "idle_compact_windows.json").write_text('{"m": 1}', encoding="utf-8")
        (home / "idle_compact_usage.json").write_text('{"s1": {"tokens": 1}}', encoding="utf-8")
        before = {f: (home / f).read_bytes() for f in (
            "idle_compact_pending.json", "idle_compact_ledger.jsonl",
            "idle_compact_windows.json", "idle_compact_usage.json")}
        mod._cmd_status("", session_id="s1")
        mod._cmd_status("debug", session_id="s1")
        for f, data in before.items():
            self.assertEqual((home / f).read_bytes(), data)
        self.assertEqual(dict(mod._timers), {})


class TestRpcStatus(StatusBase):
    def test_rpc_returns_chip_and_panel(self):
        mod, home = self._mod_with_home(
            "plugins:\n  idle_compact:\n    gate_fraction: 0.3\n"
        )
        self._db_with_session(home, chars=40000)
        (home / "idle_compact_windows.json").write_text(
            json.dumps({"m": 1000000}), encoding="utf-8")
        (home / "idle_compact_usage.json").write_text(
            json.dumps({"s1": {"tokens": 10000, "ts": time.time()}}),
            encoding="utf-8")
        (home / "idle_compact_ledger.jsonl").write_text(
            json.dumps({"ev": "settle", "ts": 1794728400.0, "saved_usd": 0.06,
                        "baseline_usd": 0.10, "verdict": "предотвращён холодный вход",
                        "facts": {"input": 1, "cache_read": 1, "output": 1, "calls": 1}})
            + "\n", encoding="utf-8")
        res = mod._rpc_status({"session_id": "s1"})
        self.assertEqual(res["chip"], "~290K left")
        self.assertTrue(res["panel"].startswith("idle-compact "))
        self.assertIn("saved: +$0.06", res["panel"])

    def test_rpc_ready_over_gate(self):
        mod, home = self._mod_with_home(
            "plugins:\n  idle_compact:\n    gate_fraction: 0.3\n"
        )
        self._db_with_session(home)
        (home / "idle_compact_windows.json").write_text(
            json.dumps({"m": 1000000}), encoding="utf-8")
        (home / "idle_compact_usage.json").write_text(
            json.dumps({"s1": {"tokens": 500000, "ts": time.time()}}),
            encoding="utf-8")
        res = mod._rpc_status({"session_id": "s1"})
        self.assertEqual(res["chip"], "Ready")

    def test_push_status_broadcasts(self):
        import sys
        import types
        mod, home = self._mod_with_home(
            "plugins:\n  idle_compact:\n    gate_fraction: 0.3\n"
        )
        self._db_with_session(home)
        (home / "idle_compact_windows.json").write_text(
            json.dumps({"m": 1000000}), encoding="utf-8")
        (home / "idle_compact_usage.json").write_text(
            json.dumps({"s1": {"tokens": 10000, "ts": time.time()}}),
            encoding="utf-8")
        seen = {}
        fake_mod = types.ModuleType("hermes_cli.plugin_events")
        def _fake_emit(plugin_id, session_id, event, payload=None):
            seen.update({"emit": (plugin_id, session_id, event),
                         "payload": payload})
            return True
        def _fake_broadcast(plugin_id, event, payload=None):
            seen.update({"broadcast": (plugin_id, event),
                         "payload": payload})
        fake_mod.emit_plugin_session_event = _fake_emit
        fake_mod.broadcast_plugin_event = _fake_broadcast
        fake_pkg = types.ModuleType("hermes_cli")
        fake_pkg.__path__ = []
        fake_pkg.plugin_events = fake_mod
        old_mod, old_pkg = (sys.modules.get("hermes_cli.plugin_events"),
                           sys.modules.get("hermes_cli"))
        sys.modules["hermes_cli.plugin_events"] = fake_mod
        sys.modules["hermes_cli"] = fake_pkg
        try:
            mod._push_status("s1")
        finally:
            if old_mod is None:
                sys.modules.pop("hermes_cli.plugin_events", None)
            else:
                sys.modules["hermes_cli.plugin_events"] = old_mod
            if old_pkg is None:
                sys.modules.pop("hermes_cli", None)
            else:
                sys.modules["hermes_cli"] = old_pkg
        self.assertEqual(seen.get("emit"), ("idle-compact", "s1", "status"))
        self.assertNotIn("broadcast", seen)
        payload = seen.get("payload") or {}
        self.assertEqual(payload.get("chip"), "~290K left")
        self.assertIn("current context: ~10K", payload.get("panel", ""))
        self.assertEqual(payload.get("session_id"), "s1")
        self.assertEqual(payload.get("session_key"), "s1")
        self.assertEqual(payload.get("agent_session_id"), "s1")

    def test_ui_session_id_maps_runtime(self):
        import sys
        import types
        mod, _home = self._mod_with_home()
        fake_agent = types.SimpleNamespace(session_id="20261115_181244_8773dc")
        fake_tui = types.ModuleType("tui_gateway.server")
        fake_tui._sessions = {"7bf26255": {"session_key": "20261115_181244_8773dc",
                                           "agent": fake_agent}}
        fake_pkg = types.ModuleType("tui_gateway")
        fake_pkg.__path__ = []
        fake_pkg.server = fake_tui
        old = (sys.modules.get("tui_gateway"), sys.modules.get("tui_gateway.server"))
        sys.modules["tui_gateway"] = fake_pkg
        sys.modules["tui_gateway.server"] = fake_tui
        try:
            self.assertEqual(mod._ui_session_id("20261115_181244_8773dc"), "7bf26255")
            self.assertIsNone(mod._ui_session_id("nope"))
        finally:
            if old[0] is None:
                sys.modules.pop("tui_gateway", None)
            else:
                sys.modules["tui_gateway"] = old[0]
            if old[1] is None:
                sys.modules.pop("tui_gateway.server", None)
            else:
                sys.modules["tui_gateway.server"] = old[1]

    def test_ui_session_id_no_tui(self):
        mod, _home = self._mod_with_home()
        self.assertIsNone(mod._ui_session_id("whatever"))

    def test_push_falls_back_to_broadcast(self):
        import sys
        import types
        mod, home = self._mod_with_home(
            "plugins:\n  idle_compact:\n    gate_fraction: 0.3\n"
        )
        self._db_with_session(home)
        seen = {}
        fake_mod = types.ModuleType("hermes_cli.plugin_events")
        fake_mod.emit_plugin_session_event = lambda *a, **k: False
        def _fake_broadcast(plugin_id, event, payload=None):
            seen.update({"broadcast": (plugin_id, event)})
        fake_mod.broadcast_plugin_event = _fake_broadcast
        fake_pkg = types.ModuleType("hermes_cli")
        fake_pkg.__path__ = []
        fake_pkg.plugin_events = fake_mod
        old_mod, old_pkg = (sys.modules.get("hermes_cli.plugin_events"),
                           sys.modules.get("hermes_cli"))
        sys.modules["hermes_cli.plugin_events"] = fake_mod
        sys.modules["hermes_cli"] = fake_pkg
        try:
            mod._push_status("s1")
        finally:
            if old_mod is None:
                sys.modules.pop("hermes_cli.plugin_events", None)
            else:
                sys.modules["hermes_cli.plugin_events"] = old_mod
            if old_pkg is None:
                sys.modules.pop("hermes_cli", None)
            else:
                sys.modules["hermes_cli"] = old_pkg
        self.assertEqual(seen.get("broadcast"), ("idle-compact", "status"))

    def test_rpc_empty_home(self):
        mod, _home = self._mod_with_home()
        res = mod._rpc_status({})
        self.assertEqual(res["chip"], "idle-compact")
        self.assertIn("compactions counted: 0", res["panel"])

    def test_register_exposes_rpc(self):
        mod, _home = self._mod_with_home()
        ctx = FakeCtx()
        mod.register(ctx)
        self.addCleanup(mod.unregister, None)
        self.assertIn("rpc:status", ctx.commands)


class TestWindowCache(StatusBase):
    def test_roundtrip(self):
        mod, home = self._mod_with_home()
        self.assertEqual(mod._cached_window("m"), 0)
        mod._note_window("m", 1000000)
        self.assertEqual(mod._cached_window("m"), 1000000)
        mod._note_window("", 5)
        mod._note_window("m", 0)
        self.assertEqual(mod._cached_window("m"), 1000000)

    def test_usage_roundtrip(self):
        mod, home = self._mod_with_home()
        self.assertEqual(mod._cached_usage("s1"), 0)
        mod._note_usage("s1", 366000)
        self.assertEqual(mod._cached_usage("s1"), 366000)
        mod._note_usage("", 5)
        mod._note_usage("s1", 0)
        self.assertEqual(mod._cached_usage("s1"), 366000)


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


class TestResidentWarnOnce(StatusBase):
    def test_reason_info_once_per_session_reason(self):
        mod, _home = self._mod_with_home()
        with self.assertLogs(mod.logger, level="INFO") as first:
            mod._resident_reason("s1", "r1")
        self.assertEqual(len(first.records), 1)
        with self.assertNoLogs(mod.logger, level="INFO"):
            mod._resident_reason("s1", "r1")
        with self.assertLogs(mod.logger, level="INFO") as second:
            mod._resident_reason("s1", "r2")
        self.assertEqual(len(second.records), 1)

    def test_found_resets_warnings(self):
        mod, _home = self._mod_with_home()
        mod._resident_reason("s1", "r1")
        mod._resident_found("s1")
        with self.assertLogs(mod.logger, level="INFO") as again:
            mod._resident_reason("s1", "r1")
        self.assertEqual(len(again.records), 1)

    def test_once_info_key_ignores_volatile_text(self):
        mod, _home = self._mod_with_home()
        with self.assertLogs(mod.logger, level="INFO") as first:
            mod._once_info("s1", "k", "msg %s", "v1")
        self.assertEqual(len(first.records), 1)
        with self.assertNoLogs(mod.logger, level="INFO"):
            mod._once_info("s1", "k", "msg %s", "v2")

    def test_resident_reason_ignores_registry_churn(self):
        mod, _home = self._mod_with_home()
        with self.assertLogs(mod.logger, level="INFO"):
            mod._resident_reason("s1", "desktop registry has no match (keys: ['a'])")
        with self.assertNoLogs(mod.logger, level="INFO"):
            mod._resident_reason("s1", "desktop registry has no match (keys: ['a', 'b'])")


if __name__ == "__main__":
    unittest.main()
