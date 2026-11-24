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


class TestNoOutputCap(unittest.TestCase):
    def test_chat_without_cap(self):
        body = wc.build_payload({"messages": [{"role": "user", "content": "hi"}]},
                                "сожми", "chat_completions", None)
        self.assertNotIn("max_tokens", body)

    def test_responses_without_cap(self):
        body = wc.build_payload({"input": [{"role": "user", "content": "hi"}]},
                                "сожми", "codex_responses", None)
        self.assertNotIn("max_output_tokens", body)

    def test_anthropic_without_cap_keeps_required_budget(self):
        """Anthropic требует max_tokens: без него релей отвечает 400.

        Раньше поле удалялось (body.pop) и весь warm-путь падал на этом
        транспорте. Запасной бюджет — не меньше 1500: при меньшем reasoning
        съедает его целиком и текста саммари не остаётся (AGENTS.md).
        """
        body = wc.build_payload({"messages": [{"role": "user", "content": "hi"}]},
                                "сожми", "anthropic_messages", None)
        self.assertEqual(body["max_tokens"], wc.ANTHROPIC_MIN_MAX_TOKENS)
        self.assertGreaterEqual(body["max_tokens"], 1500)

    def test_anthropic_fallback_replaces_capture_budget(self):
        """Захваченный max_tokens основного чата не утекает и без бюджета."""
        body = wc.build_payload({"messages": [{"role": "user", "content": "hi"}],
                                 "max_tokens": 128}, "сожми", "anthropic_messages", None)
        self.assertEqual(body["max_tokens"], wc.ANTHROPIC_MIN_MAX_TOKENS)

    def test_explicit_cap_from_capture_dropped(self):
        """Захват мог содержать max_tokens основного чата — он не должен утечь."""
        body = wc.build_payload({"messages": [{"role": "user", "content": "hi"}],
                                 "max_tokens": 128}, "сожми", "chat_completions", None)
        self.assertNotIn("max_tokens", body)


class TestTruncationGuard(unittest.TestCase):
    def test_chat_length(self):
        self.assertTrue(wc._truncated({"choices": [{"finish_reason": "length"}]}, "chat_completions"))

    def test_chat_stop(self):
        self.assertFalse(wc._truncated({"choices": [{"finish_reason": "stop"}]}, "chat_completions"))

    def test_anthropic_max_tokens(self):
        self.assertTrue(wc._truncated({"stop_reason": "max_tokens"}, "anthropic_messages"))

    def test_anthropic_end_turn(self):
        self.assertFalse(wc._truncated({"stop_reason": "end_turn"}, "anthropic_messages"))

    def test_responses_incomplete(self):
        data = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}
        self.assertTrue(wc._truncated(data, "codex_responses"))

    def test_responses_completed(self):
        self.assertFalse(wc._truncated({"status": "completed"}, "codex_responses"))

    def test_no_choices(self):
        self.assertFalse(wc._truncated({}, "chat_completions"))


class TestExtraContext(unittest.TestCase):
    def _we(self):
        spec = importlib.util.spec_from_file_location("we_extra", PLUGIN_DIR / "warm_engine.py")
        we = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(we)
        return we

    def test_nothing_added(self):
        we = self._we()
        self.assertEqual(we._extra_context("обычный промпт ядра", None), "")

    def test_previous_summary_from_arg(self):
        we = self._we()
        out = we._extra_context("x", "## Goal\nсделать")
        self.assertIn("PREVIOUS SUMMARY:", out)
        self.assertIn("сделать", out)
        self.assertIn("PRESERVE", out)

    def test_previous_summary_parsed_from_prompt(self):
        we = self._we()
        prompt = "text\n\nPREVIOUS SUMMARY:\nстарое саммари\n\nNEW TURNS TO INCORPORATE:\nновое"
        out = we._extra_context(prompt, None)
        self.assertIn("старое саммари", out)
        self.assertNotIn("новое", out)

    def test_previous_summary_truncated(self):
        we = self._we()
        out = we._extra_context("x", "a" * (we.PREV_SUMMARY_LIMIT + 500))
        self.assertIn("[truncated]", out)
        self.assertLess(len(out), we.PREV_SUMMARY_LIMIT + 2000)

    def test_focus_topic(self):
        we = self._we()
        out = we._extra_context('x\n\nFOCUS TOPIC: "плагин idle"\nдальше', None)
        self.assertIn('FOCUS TOPIC: "плагин idle"', out)
        self.assertIn("PRIORITISE", out)

    def test_focus_and_previous_together(self):
        we = self._we()
        out = we._extra_context('FOCUS TOPIC: "тема"', "прошлое")
        self.assertIn("PREVIOUS SUMMARY:", out)
        self.assertIn('FOCUS TOPIC: "тема"', out)


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
