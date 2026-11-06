"""Единственный источник правды о времени жизни prompt-cache по моделям.

Здесь и только здесь живёт таблица TTL. Её импортируют и планировщик
(``__init__.py``), и учёт экономии (``savings.py``) — иначе копии
разъезжаются и вердикт «кеш был бы тёплым» начинает врать.

Значения — секунды. Замеры и источники:

* ``muse-spark`` — ~10 мин, измерено на нашем трафике (вердикты SAVINGS).
* ``deepseek`` — ~85–90 мин по замерам; в доках вендора точного TTL нет
  («hours to days»). В таблице — ЧЕСТНЫЙ TTL провайдера (5100 с).
  Планировщик отдельно упирается в выселение агента из кеша gateway
  (дефолт 3600 с, ``agent.agent_cache.idle_ttl_secs``): задержка таймера
  считается как min(TTL модели, TTL выселения) × 0.9 — иначе плагин
  не находит живой агент и пишет «agent evicted/absent».
* ``claude``/``kimi``/``qwen``/``minimax``/``glm``/``mistral``/``grok`` —
  официальные дефолты explicit-кеша 5 мин.
* ``gpt``/``gemini`` — консервативно 10 мин (у GPT часть моделей 5–10 мин,
  у Gemini implicit-кеш без публичного TTL).

Неизвестная модель получает ``DEFAULT_TTL_SECONDS``.
"""

from __future__ import annotations

DEFAULT_TTL_SECONDS = 300

# Самая длинная подстрока побеждает.
TTL_BY_MODEL_SUBSTR: dict[str, int] = {
    "deepseek": 5100,   # 85 мин: честный TTL провайдера; планировщик режет по выселению агента (дефолт 3600 с)
    "muse-spark": 600,
    "spark": 600,
    "gpt": 600,
    "openai": 600,
    "gemini": 600,
    "google": 600,
    "claude": 300,
    "anthropic": 300,
    "kimi": 300,
    "moonshot": 300,
    "qwen": 300,
    "dashscope": 300,
    "minimax": 300,
    "glm": 300,
    "zhipu": 300,
    "mistral": 300,
    "grok": 300,
    "xai": 300,
}


def ttl_for_model(model: str) -> int:
    """TTL кеша модели по подстроке (longest-match wins)."""
    m = (model or "").strip().lower()
    best = -1
    ttl = DEFAULT_TTL_SECONDS
    for sub, value in TTL_BY_MODEL_SUBSTR.items():
        if sub in m and len(sub) > best:
            best = len(sub)
            ttl = value
    return ttl
