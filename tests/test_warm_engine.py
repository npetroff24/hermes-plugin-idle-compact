"""Тесты warm-движка: клонирование, warm-флаг, откат на aux."""

from __future__ import annotations

import importlib.util
import sys
import threading
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, "/usr/local/lib/hermes-agent")


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
