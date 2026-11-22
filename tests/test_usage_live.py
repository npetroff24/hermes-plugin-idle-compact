"""Живой замер usage из события post_api + безопасная запись кеша."""
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def load_init():
    spec = importlib.util.spec_from_file_location("ic_under_test", PLUGIN_DIR / "__init__.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ic_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class UsageLiveBase(unittest.TestCase):
    def _mod_with_home(self):
        mod = load_init()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        mod._home = lambda: home  # type: ignore[attr-defined]
        mod._usage_last_write.clear()
        return mod, home


class TestPayloadTokens(UsageLiveBase):
    def test_direct_prompt_tokens(self):
        mod, _ = self._mod_with_home()
        self.assertEqual(mod._payload_prompt_tokens({"usage": {"prompt_tokens": 123}}), 123)

    def test_fallback_sums_three_buckets(self):
        mod, _ = self._mod_with_home()
        kw = {"usage": {"input_tokens": 100, "cache_read_tokens": 50,
                        "cache_write_tokens": 25}}
        self.assertEqual(mod._payload_prompt_tokens(kw), 175)

    def test_fallback_short_keys(self):
        mod, _ = self._mod_with_home()
        kw = {"usage": {"input": 10, "cache_read": 5, "cache_write": 2}}
        self.assertEqual(mod._payload_prompt_tokens(kw), 17)

    def test_empty_is_zero(self):
        mod, _ = self._mod_with_home()
        self.assertEqual(mod._payload_prompt_tokens({}), 0)
        self.assertEqual(mod._payload_prompt_tokens({"usage": {}}), 0)


class TestOnPostApiNotesUsage(UsageLiveBase):
    def test_hook_writes_fresh_size(self):
        mod, home = self._mod_with_home()
        mod._on_post_api(session_id="s-live",
                          usage={"prompt_tokens": 777000})
        data = json.loads((home / "idle_compact_usage.json").read_text(encoding="utf-8"))
        self.assertEqual(data["s-live"]["tokens"], 777000)

    def test_hook_without_usage_keeps_old(self):
        mod, home = self._mod_with_home()
        (home / "idle_compact_usage.json").write_text(
            json.dumps({"s-keep": {"tokens": 5000, "ts": time.time()}}),
            encoding="utf-8")
        mod._on_post_api(session_id="s-keep")
        data = json.loads((home / "idle_compact_usage.json").read_text(encoding="utf-8"))
        self.assertEqual(data["s-keep"]["tokens"], 5000)


class TestSafeWrite(UsageLiveBase):
    def test_throttle_skips_tiny_delta(self):
        mod, home = self._mod_with_home()
        mod._note_usage("s-t", 100000)
        first = json.loads((home / "idle_compact_usage.json").read_text(encoding="utf-8"))
        mod._note_usage("s-t", 100500)  # +0.5% в те же 10 с — пропуск
        second = json.loads((home / "idle_compact_usage.json").read_text(encoding="utf-8"))
        self.assertEqual(first, second)

    def test_throttle_passes_big_delta(self):
        mod, home = self._mod_with_home()
        mod._note_usage("s-b", 100000)
        mod._note_usage("s-b", 200000)
        data = json.loads((home / "idle_compact_usage.json").read_text(encoding="utf-8"))
        self.assertEqual(data["s-b"]["tokens"], 200000)

    def test_no_tmp_leftovers(self):
        mod, home = self._mod_with_home()
        mod._note_usage("s-a", 1000)
        leftovers = list(home.glob("*.tmp"))
        self.assertEqual(leftovers, [])
        # Файл — валидный JSON после атомарной записи.
        data = json.loads((home / "idle_compact_usage.json").read_text(encoding="utf-8"))
        self.assertEqual(data["s-a"]["tokens"], 1000)

    def test_broken_file_reads_zero(self):
        mod, home = self._mod_with_home()
        (home / "idle_compact_usage.json").write_text("{oops", encoding="utf-8")
        self.assertEqual(mod._cached_usage("s-x"), 0)


class TestExactCoreCount(UsageLiveBase):
    def _with_breakdown(self, mod, value):
        fake_bd = ModuleType("agent.context_breakdown")
        fake_bd.compute_session_context_breakdown = lambda agent: {"context_used": value}
        old = sys.modules.get("agent.context_breakdown")
        sys.modules["agent.context_breakdown"] = fake_bd
        self.addCleanup(lambda: sys.modules.__setitem__("agent.context_breakdown", old)
                        if old is not None else sys.modules.pop("agent.context_breakdown", None))
        mod._resident = lambda sid: (object(), None, "", None)  # type: ignore[attr-defined]

    def test_rpc_uses_core_count_on_click(self):
        mod, home = self._with_home_and_cache()
        self._with_breakdown(mod, 181500)
        res = mod._rpc_status({"session_id": "s-e"})
        self.assertEqual(res["chip"], f"{mod._fmt_k(300000 - 181500)} left")
        self.assertEqual(res["bar"]["used"], 181500)
        self.assertIn("Below gate", res["panel"])

    def test_rpc_falls_back_without_agent(self):
        mod, home = self._with_home_and_cache()
        mod._resident = lambda sid: None  # type: ignore[attr-defined]
        res = mod._rpc_status({"session_id": "s-e"})
        self.assertEqual(res["chip"], "~290K left")
        self.assertIn("Below gate", res["panel"])
        self.assertEqual(res["bar"]["used"], 10000)

    def test_push_stays_cheap(self):
        mod, home = self._with_home_and_cache()
        seen = []
        mod._resident = lambda sid: seen.append(sid) or (object(), None, "", None)  # type: ignore[attr-defined]
        pkg = ModuleType("hermes_cli")
        pe = ModuleType("hermes_cli.plugin_events")
        pe.emit_plugin_session_event = lambda *a: True
        old_pkg, old_mod = sys.modules.get("hermes_cli"), sys.modules.get("hermes_cli.plugin_events")
        sys.modules["hermes_cli"] = pkg
        sys.modules["hermes_cli.plugin_events"] = pe
        self.addCleanup(lambda: self._restore_sys(old_pkg, old_mod))
        mod._push_status("s-e")
        self.assertEqual(seen, [])

    def _restore_sys(self, old_pkg, old_mod):
        if old_pkg is None:
            sys.modules.pop("hermes_cli", None)
        else:
            sys.modules["hermes_cli"] = old_pkg
        if old_mod is None:
            sys.modules.pop("hermes_cli.plugin_events", None)
        else:
            sys.modules["hermes_cli.plugin_events"] = old_mod

    def _with_home_and_cache(self):
        mod, home = self._mod_with_home()
        (home / "config.yaml").write_text(
            "plugins:\n  idle_compact:\n    gate_fraction: 0.3\n", encoding="utf-8")
        import sqlite3

        con = sqlite3.connect(str(home / "state.db"))
        con.execute(
            "CREATE TABLE sessions (id TEXT, source TEXT, model TEXT, session_key TEXT,"
            " message_count INT, api_call_count INT, archived INT, ended_at INT,"
            " last_activity_at INT)"
        )
        con.execute("INSERT INTO sessions (id, model) VALUES (?, ?)", ("s-e", "m"))
        con.commit()
        con.close()
        (home / "idle_compact_windows.json").write_text(
            json.dumps({"m": 1000000}), encoding="utf-8")
        (home / "idle_compact_usage.json").write_text(
            json.dumps({"s-e": {"tokens": 10000, "ts": time.time()}}),
            encoding="utf-8")
        return mod, home


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestToastAndBar(UsageLiveBase):
    def _db_with_session(self, home, session_id="s-t", model="m"):
        import sqlite3

        con = sqlite3.connect(str(home / "state.db"))
        con.execute(
            "CREATE TABLE sessions (id TEXT, source TEXT, model TEXT, session_key TEXT,"
            " message_count INT, api_call_count INT, archived INT, ended_at INT,"
            " last_activity_at INT)"
        )
        con.execute("INSERT INTO sessions (id, model) VALUES (?, ?)", (session_id, model))
        con.commit()
        con.close()

    def _home_full(self, tokens=10000):
        mod, home = self._mod_with_home()
        (home / "config.yaml").write_text(
            "plugins:\n  idle_compact:\n    gate_fraction: 0.3\n", encoding="utf-8")
        self._db_with_session(home)
        (home / "idle_compact_windows.json").write_text(
            json.dumps({"m": 1000000}), encoding="utf-8")
        (home / "idle_compact_usage.json").write_text(
            json.dumps({"s-t": {"tokens": tokens, "ts": time.time()}}),
            encoding="utf-8")
        return mod, home

    def test_threshold_from_fraction(self):
        mod, _ = self._home_full()
        cfg = {"compression": {"threshold": 0.5}}
        self.assertEqual(mod._live_threshold(1000000, cfg), 500000)

    def test_threshold_default_core(self):
        mod, _ = self._home_full()
        self.assertEqual(mod._live_threshold(1000000, {}), 500000)
        self.assertEqual(mod._live_threshold(200000, {}), 150000)

    def test_toast_below_gate(self):
        mod, _ = self._home_full(tokens=10000)
        out = mod._toast_panel("s-t")
        self.assertIn("Below gate", out)
        self.assertNotIn("current context", out)

    def test_toast_will_compact_with_timer(self):
        mod, home = self._home_full(tokens=400000)
        (home / "idle_compact_pending.json").write_text(
            json.dumps({"s-t": {"deadline": time.time() + 480}}), encoding="utf-8")
        out = mod._toast_panel("s-t")
        self.assertIn("Will compact after ~8m idle", out)

    def test_toast_core_owns(self):
        mod, _ = self._home_full(tokens=400000)
        mod._on_post_compaction(session_id="s-t", after_tokens=1000)
        out = mod._toast_panel("s-t")
        self.assertIn("Core owns it", out)

    def test_toast_no_data(self):
        mod, _ = self._home_full()
        out = mod._toast_panel("s-unknown")
        self.assertIn("No measurements yet", out)

    def test_bar_numbers(self):
        mod, _ = self._home_full(tokens=245000)
        bar = mod._bar_numbers("s-t")
        self.assertEqual(bar, {"used": 245000, "idle_gate": 300000,
                               "threshold": 500000})

    def test_bar_none_without_usage(self):
        mod, _ = self._home_full()
        self.assertIsNone(mod._bar_numbers("s-unknown"))
