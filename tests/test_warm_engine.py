"""Тесты warm-движка: клонирование, warm-флаг, откат на aux."""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

from _bootstrap import AGENT_HOME, load_plugin_module  # noqa: E402,F401  (кладёт исходники ядра в sys.path)


def _load():
    return load_plugin_module("warm_engine.py", "we_test")


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
        wc.apply_warm = _apply_warm  # type: ignore[attr-defined]
        wc.WarmRefused = _WarmRefused  # type: ignore[attr-defined]
        pkg.policy = policy  # type: ignore[attr-defined]
        pkg.warm_client = wc  # type: ignore[attr-defined]
        old = {k: sys.modules.get(k) for k in
               ("wepkg_fake", "wepkg_fake.policy", "wepkg_fake.warm_client")}
        sys.modules["wepkg_fake"] = pkg  # type: ignore[arg-type]
        sys.modules["wepkg_fake.policy"] = policy  # type: ignore[arg-type]
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

        mod = load_plugin_module("warm_engine.py", "wepkg_fake.warm_engine")
        mod._import_base = lambda: FakeBase  # type: ignore[attr-defined]
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


class TestFallbackOutsideHandler(unittest.TestCase):
    """Откат на aux вызывается один раз и вне try/except.

    Раньше `return call_super()` стоял внутри try (ветка протухшего захвата),
    поэтому ошибка штатного пути попадала в тот же handler: логировалась как
    отказ warm и повторяла базовый вызов вторым разом.
    """

    def _load(self, max_age=0):
        import types as _types

        pkg = _types.ModuleType("wf12")
        pkg.__path__ = []
        policy = _types.SimpleNamespace(capture_max_age_for=lambda m: max_age,
                                        REQUEST_TIMEOUT_S=30)
        applied = []
        wc = _types.ModuleType("wf12.warm_client")

        class _Refused(RuntimeError):
            pass

        def _apply_warm(**kw):
            applied.append(kw)
            return "warm-summary", {"cache_read": 1}

        wc.apply_warm = _apply_warm  # type: ignore[attr-defined]
        wc.WarmRefused = _Refused  # type: ignore[attr-defined]
        pkg.policy = policy  # type: ignore[attr-defined]
        pkg.warm_client = wc  # type: ignore[attr-defined]
        old = {k: sys.modules.get(k) for k in
               ("wf12", "wf12.policy", "wf12.warm_client", "wf12.warm_engine")}
        sys.modules["wf12"] = pkg
        sys.modules["wf12.policy"] = policy  # type: ignore[arg-type]
        sys.modules["wf12.warm_client"] = wc

        def _restore():
            for k, v in old.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
        self.addCleanup(_restore)

        mod = load_plugin_module("warm_engine.py", "wf12.warm_engine")
        return mod, applied

    def _engine(self, stale=True):
        import types as _types

        return _types.SimpleNamespace(
            _warm_lock=threading.Lock(),
            _warm_capture={"messages": [{"role": "user", "content": "hi"}]},
            _warm_route={
                "_cap_ts": time.time() - (500 if stale else 0),
                "model": "m", "api_mode": "chat_completions",
                "base_url": "https://x/v1", "api_key": "k", "session_id": "s",
            },
            _warm_stats=None,
            api_mode="chat_completions",
            base_url="https://x/v1",
            api_key="k",
            provider="p",
            _previous_summary=None,
            _warm_max_tokens=lambda: None,
        )

    def test_expired_capture_falls_back_once(self):
        mod, applied = self._load(max_age=1)
        runs = []
        out = mod._run_warm(self._engine(), "text", lambda: runs.append(1) or "aux")
        self.assertEqual(out, "aux")
        self.assertEqual(runs, [1])
        self.assertEqual(applied, [], "протухший захват не повторяем")

    def test_fallback_failure_is_not_retried_and_propagates(self):
        mod, applied = self._load(max_age=1)
        runs = []

        def call_super():
            runs.append(1)
            raise RuntimeError("core down")

        with self.assertRaises(RuntimeError):
            mod._run_warm(self._engine(), "text", call_super)
        self.assertEqual(len(runs), 1,
                         "базовый вызов не повторяем вторым разом")
        self.assertEqual(applied, [])

    def test_warm_failure_falls_back_once(self):
        mod, applied = self._load(max_age=1000)
        engine = self._engine(stale=False)
        mod._run_warm(engine, "text", lambda: None)  # warm вернёт текст

        applied.clear()
        runs = []
        engine2 = self._engine(stale=False)
        engine2._warm_capture = {}  # пустой захват: warm не применим
        out = mod._run_warm(engine2, "text", lambda: runs.append(1) or "aux")
        self.assertEqual(out, "aux")
        self.assertEqual(runs, [1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
