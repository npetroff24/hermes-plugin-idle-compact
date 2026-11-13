"""Единственное место, где заданы временные константы плагина.

Здесь и только здесь хранятся числа, отвечающие на вопрос «сколько живёт»:
запас до истечения кеша, срок хранения захвата запроса, таймаут HTTP,
сторожевой пол для восстановления дедлайнов. Остальные файлы плагина берут
значения отсюда и литералов не содержат — иначе цифры разъезжаются, и
вердикт «кеш был бы тёплым» начинает врать.

Таблица TTL по моделям живёт отдельно, в ``model_ttl.py``: это данные
провайдеров, а не политика плагина. Здесь — только вычисления над ними.
"""

from __future__ import annotations

from .model_ttl import DEFAULT_TTL_SECONDS, TTL_BY_MODEL_SUBSTR, ttl_for_model

__all__ = [
    "AGENT_CACHE_IDLE_TTL_DEFAULT_S",
    "DEFAULT_TTL_SECONDS",
    "REQUEST_TIMEOUT_S",
    "TTL_BY_MODEL_SUBSTR",
    "TTL_FACTOR",
    "WATCHDOG_MIN_GRACE_S",
    "arm_window_for",
    "capture_max_age_for",
    "schedule_delay_for",
    "ttl_for",
    "watchdog_grace",
]

# Запас до истечения кеша: работаем на 90% TTL, чтобы успеть до его конца.
TTL_FACTOR = 0.9

# Таймаут одного запроса суммаризации, секунды. Живое сжатие длится дольше
# дефолтных 5 с httpx, но не бесконечно.
REQUEST_TIMEOUT_S = 300.0

# Дефолт TTL выселения агента из кеша гейтвея — agent.agent_cache.idle_ttl_secs.
# Совпадает с ядровым _AGENT_CACHE_IDLE_TTL_SECS; читать приватную константу
# ядра плагину нельзя, поэтому значение задано здесь.
AGENT_CACHE_IDLE_TTL_DEFAULT_S = 3600.0

# Минимальный сторожевой запас при восстановлении/перепроверке дедлайнов:
# дедлайн, просроченный сильнее, не воскрешаем — кеш всё равно мёртв.
WATCHDOG_MIN_GRACE_S = 3600.0


def ttl_for(model: str) -> int:
    """TTL prompt-cache модели, секунды."""
    return ttl_for_model(model)


def arm_window_for(model: str) -> float:
    """Сколько секунд захват считается достаточно свежим для тёплого повтора."""
    return ttl_for_model(model) * TTL_FACTOR


def capture_max_age_for(model: str) -> float:
    """Сколько секунд хранить захват запроса: дольше кеша он бесполезен."""
    return float(ttl_for_model(model))


def schedule_delay_for(model: str, evict_ttl: float) -> float:
    """Пауза планировщика: сжимаем, пока живы и кеш, и агент в памяти гейтвея."""
    return float(min(ttl_for_model(model), evict_ttl)) * TTL_FACTOR


def watchdog_grace(delay: float) -> float:
    """Допустимое опоздание дедлайна: двойная пауза, но не меньше пола."""
    return max(float(delay) * 2.0, WATCHDOG_MIN_GRACE_S)
