"""Тесты warm-движка: клонирование, warm-флаг, откат на aux."""

from __future__ import annotations

import importlib.util
import sys
import threading
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

from _bootstrap import AGENT_HOME  # noqa: E402,F401  (кладёт исходники ядра в sys.path)


def _load():
    spec = importlib.util.spec_from_file_location("we_test", PLUGIN_DIR / "warm_engine.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestClone(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.we = _load()
        cls.Engine = cls.we.IdleWarmCompressor.build()

    def _make(self):
        return self.Engine(model="m", base_url="https://x/v1", api_key="k",
                           provider="p", api_mode="chat_completions", config_context_length=100000)

    def test_name_is_not_compressor(self):
        """Имя не должно быть 'compressor' — иначе ядро не активирует плагинный движок."""
        self.assertEqual(self._make().name, "idle_warm")

    def test_clone_has_own_lock(self):
        a = self._make()
        b = a.clone_for_agent()
        self.assertIsNot(a._warm_lock, b._warm_lock)
        self.assertIsInstance(b._warm_lock, type(threading.Lock()))

    def test_clone_copies_route(self):
        a = self._make()
        b = a.clone_for_agent()
        self.assertEqual(b.model, a.model)
        self.assertEqual(b.api_mode, a.api_mode)

    def test_clone_does_not_share_warm_state(self):
        a = self._make()
        a.begin_warm({"messages": []}, {"base_url": "x"})
        b = a.clone_for_agent()
        self.assertIsNone(b._warm_capture)


class TestWarmFlag(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.we = _load()
        cls.Engine = cls.we.IdleWarmCompressor.build()

    def _make(self):
        return self.Engine(model="m", base_url="https://x/v1", api_key="k",
                           provider="p", api_mode="chat_completions", config_context_length=100000)

    def test_begin_end_cycle(self):
        e = self._make()
        e.begin_warm({"messages": [{"role": "user", "content": "x"}]},
                     {"base_url": "https://x/v1", "api_mode": "chat_completions", "api_key": "k"})
        self.assertIsNotNone(e._warm_capture)
        e.end_warm()
        self.assertIsNone(e._warm_capture)
        self.assertIsNone(e._warm_route)

    def test_without_warm_goes_to_aux(self):
        """Без begin_warm движок обязан уйти в штатный путь (не падать и не звать warm)."""
        e = self._make()
        called = {"aux": False, "warm": False}

        class Fake(Exception):
            pass

        def fake_super(prompt, ts):
            called["aux"] = True
            return "aux-summary"

        # подменяем родительский вызов только в тесте
        base = type(e).__mro__[1]
        original = base._call_summary_llm
        base._call_summary_llm = lambda self, p, t: fake_super(p, t)
        try:
            out = e._call_summary_llm("prompt", 0.0)
        finally:
            base._call_summary_llm = original
        self.assertEqual(out, "aux-summary")
        self.assertTrue(called["aux"])
        self.assertFalse(called["warm"])

    def test_broken_warm_falls_back(self):
        """Если warm падает — отдаём штатный результат, а не исключение."""
        e = self._make()
        e.begin_warm({"messages": []}, {"base_url": "https://x/v1",
                                        "api_mode": "chat_completions", "api_key": "k"})
        base = type(e).__mro__[1]
        original = base._call_summary_llm
        base._call_summary_llm = lambda self, p, t: "aux-fallback"
        try:
            out = e._call_summary_llm("prompt", 0.0)  # warm упадёт: пустой messages
        finally:
            base._call_summary_llm = original
            e.end_warm()
        self.assertEqual(out, "aux-fallback")

    def test_refusal_logs_reason(self):
        """Причина отказа попадает в лог отдельной строкой, а не только «откат»."""
        e = self._make()
        e.begin_warm({"messages": []}, {"base_url": "https://x/v1",
                                        "api_mode": "chat_completions", "api_key": "k"})
        base = type(e).__mro__[1]
        original = base._call_summary_llm
        base._call_summary_llm = lambda self, p, t: "aux"
        try:
            with self.assertLogs(level="WARNING") as logs:
                e._call_summary_llm("prompt", 0.0)
        finally:
            base._call_summary_llm = original
            e.end_warm()
        joined = "\n".join(logs.output)
        self.assertIn("idle-warm: отказ", joined)
        self.assertIn("→ штатный путь", joined)


class TestMessagesApi(unittest.TestCase):
    """Новое ядро: _call_summary_llm(self, messages, ts) — пара [system, user]."""

    def _fresh(self):
        import sys
        import types as _types
        src = (PLUGIN_DIR / "warm_engine.py").read_text(encoding="utf-8")
        pkg = _types.ModuleType("wepkg_fake")
        pkg.__path__ = []
        policy = _types.SimpleNamespace(
            capture_max_age_for=lambda m: 10 ** 9, REQUEST_TIMEOUT_S=30)
        calls = {}
        wc = _types.ModuleType("wepkg_fake.warm_client")

        class _WarmRefused(RuntimeError):
            pass

        def _apply_warm(**kw):
            calls.update(kw)
            return "warm-summary", {"cache_read": 1, "fresh_input": 2,
                                    "output_tokens": 3}
        wc.apply_warm = _apply_warm
        wc.WarmRefused = _WarmRefused
        pkg.policy = policy
        pkg.warm_client = wc
        old = {k: sys.modules.get(k) for k in
               ("wepkg_fake", "wepkg_fake.policy", "wepkg_fake.warm_client")}
        sys.modules["wepkg_fake"] = pkg
        sys.modules["wepkg_fake.policy"] = policy
        sys.modules["wepkg_fake.warm_client"] = wc

        def _restore():
            for k, v in old.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
            sys.modules.pop("wepkg_fake.warm_engine", None)
        self.addCleanup(_restore)

        class FakeBase:
            def __init__(self, **kw):
                for k, v in kw.items():
                    setattr(self, k, v)
            def _call_summary_llm(self, messages, prompt_started_at):
                self.aux_messages = messages
                return "aux-summary"
            def update_model(self, *a, **k):
                pass

        spec = importlib.util.spec_from_file_location(
            "wepkg_fake.warm_engine", PLUGIN_DIR / "warm_engine.py")
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["wepkg_fake.warm_engine"] = mod
        spec.loader.exec_module(mod)
        mod._import_base = lambda: FakeBase
        mod.IdleWarmCompressor._cls = None
        engine_cls = mod.IdleWarmCompressor.build()
        return mod, engine_cls, calls

    def _make(self, engine_cls):
        return engine_cls(model="m", base_url="https://x/v1", api_key="k",
                          provider="p", api_mode="chat_completions",
                          config_context_length=100000)

    def test_variant_detected(self):
        mod, _engine_cls, _calls = self._fresh()
        self.assertEqual(mod._summary_api_variant(), "messages")
        ok, why = mod.signature_ok()
        self.assertTrue(ok)
        self.assertIn("messages", why)

    def test_warm_path_with_messages(self):
        _mod, engine_cls, calls = self._fresh()
        e = self._make(engine_cls)
        e.begin_warm({"messages": [{"role": "user", "content": "hi"}]},
                     {"base_url": "https://x/v1", "api_mode": "chat_completions",
                      "api_key": "k", "model": "m"})
        out = e._call_summary_llm([
            {"role": "system", "content": 'sys\n\nFOCUS TOPIC: "тема"'},
            {"role": "user", "content": "PREVIOUS SUMMARY:\nстарое\n\nNEW TURNS TO INCORPORATE:\nновое"},
        ], 0.0)
        self.assertEqual(out, "warm-summary")
        prompt = calls.get("prompt", "")
        self.assertIn("## Goal", prompt)
        self.assertIn("старое", prompt)
        self.assertNotIn("новое", prompt)
        self.assertIn('FOCUS TOPIC: "тема"', prompt)

    def test_no_warm_falls_back(self):
        _mod, engine_cls, _calls = self._fresh()
        e = self._make(engine_cls)
        out = e._call_summary_llm([{"role": "system", "content": "s"},
                                   {"role": "user", "content": "u"}], 0.0)
        self.assertEqual(out, "aux-summary")
        self.assertEqual(len(e.aux_messages), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
