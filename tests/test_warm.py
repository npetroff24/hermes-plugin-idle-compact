"""Тесты warm-пути: сборка запроса, разбор ответа, переключение движка."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

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
                          base_url="https://x", api_key="k")

    def test_unsupported_mode(self):
        with self.assertRaises(wc.WarmRefused):
            wc.apply_warm(captured={"messages": [{"role": "user", "content": "x"}]},
                          prompt="p", api_mode="weird", base_url="https://x", api_key="k")

    def test_no_route(self):
        with self.assertRaises(wc.WarmRefused):
            wc.apply_warm(captured={"messages": [{"role": "user", "content": "x"}]},
                          prompt="p", api_mode="chat_completions", base_url="", api_key="")


class TestEngineSignature(unittest.TestCase):
    def test_signature_matches_kernel(self):
        """Если ядро сменит сигнатуру _call_summary_llm — warm должен отключиться, а не падать."""
        sys.path.insert(0, "/usr/local/lib/hermes-agent")
        spec = importlib.util.spec_from_file_location("we_under_test", PLUGIN_DIR / "warm_engine.py")
        assert spec is not None and spec.loader is not None
        we = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(we)
        ok, why = we.signature_ok()
        self.assertTrue(ok, why)


if __name__ == "__main__":
    unittest.main(verbosity=2)
