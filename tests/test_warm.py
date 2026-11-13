"""Тесты warm-пути: сборка запроса, разбор ответа, переключение движка."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

from _bootstrap import AGENT_HOME  # noqa: E402,F401  (кладёт исходники ядра в sys.path)

_spec = importlib.util.spec_from_file_location("wc_under_test", PLUGIN_DIR / "warm_client.py")
assert _spec is not None and _spec.loader is not None
wc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wc)


class TestUrls(unittest.TestCase):
    def test_chat(self):
        self.assertEqual(wc._url("https://x/v1", "chat_completions"), "https://x/v1/chat/completions")

    def test_responses(self):
        self.assertEqual(wc._url("https://x/v1", "codex_responses"), "https://x/v1/responses")

    def test_anthropic_adds_v1(self):
        """У anthropic-релея база без /v1 — иначе 404 (проверено живьём)."""
        self.assertEqual(wc._url("https://opencode.ai/zen/go", "anthropic_messages"),
                         "https://opencode.ai/zen/go/v1/messages")

    def test_trailing_slash(self):
        self.assertEqual(wc._url("https://x/v1/", "chat_completions"), "https://x/v1/chat/completions")


class TestHeaders(unittest.TestCase):
    def test_bearer_for_chat(self):
        h = wc._headers("k", "chat_completions", "https://x/v1")
        self.assertEqual(h["Authorization"], "Bearer k")
        self.assertNotIn("x-api-key", h)

    def test_anthropic_headers(self):
        h = wc._headers("k", "anthropic_messages", "https://x")
        self.assertEqual(h["x-api-key"], "k")
        self.assertIn("anthropic-version", h)

    def test_opencode_session_header(self):
        h = wc._headers("k", "chat_completions", "https://opencode.ai/zen/go/v1")
        self.assertIn("x-opencode-session", h)


class TestBuildPayload(unittest.TestCase):
    def test_chat_appends_instruction(self):
        captured = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "temperature": 0}
        body = wc.build_payload(captured, "сожми", "chat_completions", 500)
        self.assertEqual(len(body["messages"]), 2)
        self.assertEqual(body["messages"][-1], {"role": "user", "content": "сожми"})
        self.assertEqual(body["max_tokens"], 500)
        self.assertFalse(body["stream"])

    def test_chat_does_not_mutate_capture(self):
        captured = {"messages": [{"role": "user", "content": "hi"}]}
        wc.build_payload(captured, "сожми", "chat_completions", 500)
        self.assertEqual(len(captured["messages"]), 1)

    def test_responses_uses_input(self):
        captured = {"model": "m", "input": [{"role": "user", "content": "hi"}], "instructions": "sys"}
        body = wc.build_payload(captured, "сожми", "codex_responses", 700)
        self.assertEqual(len(body["input"]), 2)
        self.assertNotIn("messages", body)
        self.assertEqual(body["max_output_tokens"], 700)
        self.assertFalse(body["store"])
        self.assertEqual(body["instructions"], "sys")

    def test_anthropic_keeps_system(self):
        captured = {"model": "m", "system": "sys", "messages": [{"role": "user", "content": "hi"}]}
        body = wc.build_payload(captured, "сожми", "anthropic_messages", 900)
        self.assertEqual(body["system"], "sys")
        self.assertEqual(len(body["messages"]), 2)
        self.assertEqual(body["max_tokens"], 900)

    def test_empty_history_refused(self):
        with self.assertRaises(wc.WarmRefused):
            wc.build_payload({"model": "m"}, "сожми", "chat_completions", 100)

    def test_stream_forced_off(self):
        """Стриминг выключаем явно: ответ нужен целиком, а stream_options не нужны."""
        captured = {"messages": [{"role": "user", "content": "x"}], "stream": True,
                    "stream_options": {"include_usage": True}}
        body = wc.build_payload(captured, "p", "chat_completions", 100)
        self.assertIs(body.get("stream"), False)
        self.assertNotIn("stream_options", body)


class TestExtractText(unittest.TestCase):
    def test_chat(self):
        data = {"choices": [{"message": {"content": "привет"}}]}
        self.assertEqual(wc._extract_text(data, "chat_completions"), "привет")

    def test_responses(self):
        data = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "ок"}]}]}
        self.assertEqual(wc._extract_text(data, "codex_responses"), "ок")

    def test_anthropic(self):
        data = {"content": [{"type": "text", "text": "ок"}, {"type": "tool_use", "name": "x"}]}
        self.assertEqual(wc._extract_text(data, "anthropic_messages"), "ок")

    def test_empty(self):
        self.assertEqual(wc._extract_text({}, "chat_completions"), "")


class TestApplyWarm(unittest.TestCase):
    def test_no_capture(self):
        with self.assertRaises(wc.WarmRefused):
            wc.apply_warm(captured={}, prompt="p", api_mode="chat_completions",
                          base_url="https://x", api_key="k", timeout=5.0)

    def test_unsupported_mode(self):
        with self.assertRaises(wc.WarmRefused):
            wc.apply_warm(captured={"messages": [{"role": "user", "content": "x"}]},
                          prompt="p", api_mode="weird", base_url="https://x", api_key="k", timeout=5.0)

    def test_no_route(self):
        with self.assertRaises(wc.WarmRefused):
            wc.apply_warm(captured={"messages": [{"role": "user", "content": "x"}]},
                          prompt="p", api_mode="chat_completions", base_url="", api_key="", timeout=5.0)


class TestAffinityHeader(unittest.TestCase):
    def test_session_key_used_when_given(self):
        h = wc._headers("k", "codex_responses", "https://opencode.ai/zen/go/v1",
                        session_key="20261027_054524_0c574947")
        self.assertEqual(h["x-opencode-session"], "20261027_054524_0c574947")

    def test_fallback_idle_warm_without_key(self):
        h = wc._headers("k", "codex_responses", "https://opencode.ai/zen/go/v1")
        self.assertEqual(h["x-opencode-session"], "idle-warm")

    def test_no_header_off_opencode_without_key(self):
        h = wc._headers("k", "chat_completions", "https://x/v1")
        self.assertNotIn("x-opencode-session", h)

    def test_conversation_key_matches_session(self):
        """Ключ разговора совпадает с session_id (нормализация — тождество)."""
        key = wc._conversation_session_key(
            "opencode-go", "https://opencode.ai/zen/go/v1", "20261027_054524_0c574947")
        self.assertEqual(key, "20261027_054524_0c574947")

    def test_conversation_key_none_off_target(self):
        key = wc._conversation_session_key(None, "https://x/v1", "s")
        self.assertIsNone(key)


class TestRedirectSafety(unittest.TestCase):
    """Редирект не преследуем: ключ пользователя не должен уйти на чужой хост."""

    class _Resp:
        def __init__(self, status, headers=None, content=b"{}"):
            self.status_code = status
            self.headers = headers or {}
            self.content = content
            self.text = content.decode()

    class _Client:
        seen_kwargs = None

        def __init__(self, **kwargs):
            type(self).seen_kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **kw):
            return TestRedirectSafety._Resp(302, {"location": "https://evil.example/x"})

    def test_follow_redirects_disabled(self):
        orig = wc.httpx.Client
        wc.httpx.Client = self._Client
        try:
            with self.assertRaises(wc.WarmRefused) as ctx:
                wc.request_summary(
                    captured={"body": {"messages": [{"role": "user", "content": "hi"}]}},
                    prompt="summarise", api_mode="anthropic_messages",
                    base_url="https://api.anthropic.com", api_key="k", timeout=5.0,
                )
        finally:
            wc.httpx.Client = orig
        self.assertFalse(self._Client.seen_kwargs.get("follow_redirects"))
        self.assertIn("redirect not followed", str(ctx.exception))


class TestEngineSignature(unittest.TestCase):
    def test_signature_matches_kernel(self):
        """Если ядро сменит сигнатуру _call_summary_llm — warm должен отключиться, а не падать."""
        spec = importlib.util.spec_from_file_location("we_under_test", PLUGIN_DIR / "warm_engine.py")
        assert spec is not None and spec.loader is not None
        we = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(we)
        ok, why = we.signature_ok()
        self.assertTrue(ok, why)


if __name__ == "__main__":
    unittest.main(verbosity=2)
