"""Warm-суммаризация: повтор последнего запроса + инструкция в хвосте.

Проверено замерами 29.10.2026 на opencode-go: такой повтор читает историю
из prompt-cache на 96–98%, тогда как штатный aux-вызов (сериализация истории
в отдельную строку) — 0%.

Поддерживаются три транспорта Hermes:
  * chat_completions   — POST {base}/chat/completions
  * codex_responses    — POST {base}/responses
  * anthropic_messages — POST {base}/v1/messages

Ключ и маршрут берутся из захваченного запроса (хук pre_api_request), поэтому
warm-вызов идёт ровно туда же, куда шёл основной.
"""

from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

TIMEOUT = 300.0


class WarmRefused(RuntimeError):
    """Warm-путь неприменим — вызывающий должен откатиться на aux-суммаризацию."""


def _headers(api_key: str, api_mode: str, base_url: str) -> dict[str, str]:
    h = {
        "Content-Type": "application/json",
        "User-Agent": "HermesAgent/0.22.5",
    }
    if api_mode == "anthropic_messages":
        h["x-api-key"] = api_key
        h["anthropic-version"] = "2023-06-01"
    else:
        h["Authorization"] = f"Bearer {api_key}"
    # opencode-релеи требуют идентификатор сессии для маршрутизации
    if "opencode.ai" in (base_url or ""):
        h["x-opencode-session"] = "idle-warm"
    return h


def _url(base_url: str, api_mode: str) -> str:
    base = (base_url or "").rstrip("/")
    if api_mode == "anthropic_messages":
        # у anthropic-релея база без /v1
        return f"{base}/v1/messages"
    if api_mode == "codex_responses":
        return f"{base}/responses"
    return f"{base}/chat/completions"


def _instruction_row(prompt: str) -> dict:
    return {"role": "user", "content": prompt}


def build_payload(captured: dict, prompt: str, api_mode: str, max_tokens: int | None) -> dict:
    """Собрать warm-запрос: захваченный payload + инструкция в хвосте."""
    body = dict(captured)
    # Двойная защита: хук может отдать обёртку {"method": "POST", "body": {...}}
    if isinstance(body.get("body"), dict) and not ({"messages", "input"} & set(body)):
        body = dict(body["body"])
    # служебные поля захвата, которых нет в реальном запросе
    for key in ("stream", "stream_options"):
        body.pop(key, None)

    if api_mode == "codex_responses":
        rows = list(body.get("input") or [])
    elif api_mode == "anthropic_messages":
        rows = list(body.get("messages") or [])
        sys_prompt = body.get("system")
        if sys_prompt is not None:
            body["system"] = sys_prompt
    else:
        rows = list(body.get("messages") or [])

    if not rows:
        raise WarmRefused(f"в захваченном запросе нет истории (ключи: {sorted(body)[:10]})")
    # Диагностика: без неё не видно, почему warm-запрос больше основного и не
    # попадает в кеш (реальный случай: 124 087 против 101 599 токенов).
    _chars = 0
    for _r in rows:
        _c = _r.get("content") if isinstance(_r, dict) else None
        if isinstance(_c, str):
            _chars += len(_c)
        elif isinstance(_c, list):
            _chars += sum(len(str(p.get("text") or "")) for p in _c if isinstance(p, dict))
    import json as _json
    _instr = body.get("instructions") or ""
    _tools = body.get("tools") or []
    _cache_key = body.get("prompt_cache_key")
    try:
        _tools_chars = len(_json.dumps(_tools, ensure_ascii=False))
    except Exception:
        _tools_chars = -1
    logger.info(
        "idle-warm: структура — режим=%s строк=%d символов_истории=%d "
        "instructions_символов=%d tools_символов=%d prompt_cache_key=%s store=%s",
        api_mode, len(rows), _chars, len(_instr) if isinstance(_instr, str) else -1,
        _tools_chars, _cache_key, body.get("store"),
    )
    rows.append(_instruction_row(prompt))

    if api_mode == "codex_responses":
        body["input"] = rows
        body.pop("messages", None)
        body["max_output_tokens"] = int(max_tokens or 4000)
        body["store"] = False
        # stream в /responses не выставляем: с stream=false релей отвечает 403
        body.pop("stream", None)
    elif api_mode == "anthropic_messages":
        body["messages"] = rows
        body["max_tokens"] = int(max_tokens or 4000)
    else:
        body["messages"] = rows
        body["max_tokens"] = int(max_tokens or 4000)
        body["stream"] = False
    return body


def _extract_text(data: dict, api_mode: str) -> str:
    if api_mode == "codex_responses":
        parts = []
        for item in data.get("output") or []:
            for chunk in item.get("content") or []:
                if chunk.get("type") in ("output_text", "text"):
                    parts.append(chunk.get("text") or "")
        return "".join(parts).strip()
    if api_mode == "anthropic_messages":
        return "".join(
            c.get("text") or "" for c in (data.get("content") or []) if c.get("type") == "text"
        ).strip()
    try:
        return ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
    except Exception:
        return ""


def request_summary(
    *, captured: dict, prompt: str, api_mode: str, base_url: str, api_key: str,
    max_tokens: int | None = None,
) -> tuple[str, dict]:
    """Выполнить warm-запрос. Возвращает (текст саммари, usage)."""
    body = build_payload(captured, prompt, api_mode, max_tokens)
    url = _url(base_url, api_mode)
    data = json.dumps(body).encode()
    # Отладочный дамп отправленного warm-тела: без него не видно, почему
    # провайдер не находит кеш (вход 61k при захваченных 42k).
    try:
        from pathlib import Path as _P
        _dump = _P.home() / ".hermes" / f"idle_warm_body_{api_mode}.json"
        _dump.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    req = urllib.request.Request(url, data=data, headers=_headers(api_key, api_mode, base_url), method="POST")
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise WarmRefused(f"HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise WarmRefused(f"{type(exc).__name__}: {exc}") from exc

    try:
        parsed = json.loads(raw)
    except Exception as exc:
        raise WarmRefused("не JSON в ответе") from exc

    text = _extract_text(parsed, api_mode)
    usage = parsed.get("usage") or {}
    oc = (usage.get("opencode") or {}).get("tokens") or {}
    stats = {
        "cache_read": oc.get("cache_read"),
        "fresh_input": oc.get("input"),
        "input_tokens": usage.get("input_tokens") or usage.get("prompt_tokens"),
        "output_tokens": usage.get("output_tokens") or usage.get("completion_tokens"),
    }
    if not text:
        raise WarmRefused("пустой ответ модели")
    return text, stats


def apply_warm(
    *, captured: dict, prompt: str, api_mode: str, base_url: str, api_key: str,
    max_tokens: int | None = None,
) -> tuple[str, dict]:
    """Проверить применимость и выполнить warm-запрос."""
    if not captured:
        raise WarmRefused("нет захваченного запроса")
    if api_mode not in ("chat_completions", "codex_responses", "anthropic_messages"):
        raise WarmRefused(f"режим {api_mode} не поддерживается")
    if not base_url or not api_key:
        raise WarmRefused("нет маршрута или ключа")
    return request_summary(
        captured=captured, prompt=prompt, api_mode=api_mode,
        base_url=base_url, api_key=api_key, max_tokens=max_tokens,
    )
