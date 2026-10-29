"""Свой контекст-движок: штатный компрессор + warm-путь для фонового сжатия.

Зачем: ядро суммирует историю отдельным aux-запросом, где история
сериализована в одну строку. Префикс такого запроса не совпадает с основным
разговором, поэтому prompt-cache не срабатывает никогда (замер: cache_read=0
на muse, deepseek, minimax). Warm-путь повторяет последний реальный запрос
модели и дописывает инструкцию в хвост — префикс совпадает, чтение истории
идёт по цене кеша (замер: 96–98%).

Устройство:
  * наследуем штатный ContextCompressor — вся логика (порог, защита
    головы/хвоста, замена середины, коммит) остаётся ядровой;
  * переопределяем единственный метод ``_call_summary_llm`` — точку, где
    ядро делает LLM-вызов суммирования;
  * если плагин выставил warm-контекст (``begin_warm``) и есть захваченный
    запрос — отвечаем warm-вызовом; иначе уходим в штатный aux-путь.

Ничего из ядра не переопределяется в рантайме: это обычное наследование
публичного класса, а не подмена атрибутов модуля.
"""

from __future__ import annotations

import inspect
import logging
import threading

logger = logging.getLogger(__name__)


def _import_base():
    from agent.context_compressor import ContextCompressor

    return ContextCompressor


class IdleWarmCompressor:
    """Фабрика: создаём класс-наследник лениво, чтобы импорт ядра не падал."""

    _cls = None

    @classmethod
    def build(cls):
        if cls._cls is not None:
            return cls._cls

        Base = _import_base()

        class _Engine(Base):  # type: ignore[misc, valid-type]
            """Штатный компрессор с warm-ответом на суммирование."""

            @property
            def name(self) -> str:
                # Своё имя: при "compressor" ядро считает движок встроенным
                # и не активирует плагинный (см. _select_context_engine).
                return "idle_warm"

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._warm_lock = threading.Lock()
                self._warm_capture: dict | None = None
                self._warm_route: dict | None = None
                self._warm_stats: dict | None = None
                logger.info("idle-warm: движок создан (model=%r)", getattr(self, "model", ""))

            # ── публичное API для плагина ────────────────────────────────
            def update_model(self, *args, **kwargs) -> None:
                super().update_model(*args, **kwargs)
                logger.info("idle-warm: движок настроен на модель %r (режим %r)",
                            getattr(self, "model", ""), getattr(self, "api_mode", ""))

            def clone_for_agent(self):
                """Копия для каждого агента: лок не копируется, состояние — независимое.

                Без этого ядро падает на deepcopy (threading.Lock не копируется)
                и молча откатывается на встроенный компрессор.
                """
                import copy as _copy

                state = {k: v for k, v in self.__dict__.items() if k != "_warm_lock"}
                new = object.__new__(type(self))
                try:
                    new.__dict__ = _copy.deepcopy(state)
                except Exception:
                    # Что-то в состоянии некопируемое — берём поверхностно, но
                    # warm-поля всё равно сбрасываем ниже.
                    new.__dict__ = dict(state)
                new._warm_lock = threading.Lock()
                new._warm_capture = None
                new._warm_route = None
                new._warm_stats = None
                logger.info("idle-warm: движок выдан агенту (model=%r)", getattr(new, "model", ""))
                return new

            def begin_warm(self, capture: dict | None, route: dict | None) -> None:
                """Разрешить warm-путь для следующего суммирования."""
                with self._warm_lock:
                    self._warm_capture = capture
                    self._warm_route = route
                    self._warm_stats = None

            def end_warm(self) -> dict | None:
                """Запретить warm-путь и вернуть статистику последнего вызова."""
                with self._warm_lock:
                    stats = self._warm_stats
                    self._warm_capture = None
                    self._warm_route = None
                    self._warm_stats = None
                    return stats

            def warm_status(self) -> dict | None:
                with self._warm_lock:
                    return self._warm_stats

            # ── перехват LLM-вызова суммирования ─────────────────────────
            def _call_summary_llm(self, prompt: str, prompt_started_at: float) -> str:
                capture = route = None
                with self._warm_lock:
                    capture = self._warm_capture
                    route = self._warm_route

                if capture and route:
                    try:
                        from . import warm_client

                        text, stats = warm_client.apply_warm(
                            captured=capture,
                            prompt=prompt,
                            api_mode=route.get("api_mode") or self.api_mode,
                            base_url=route.get("base_url") or self.base_url,
                            api_key=route.get("api_key") or self.api_key,
                            max_tokens=self._warm_max_tokens(),
                        )
                        with self._warm_lock:
                            self._warm_stats = stats
                        logger.info(
                            "idle-warm: суммаризация по кешу — cache_read=%s свежий_вход=%s выход=%s",
                            stats.get("cache_read"), stats.get("fresh_input"), stats.get("output_tokens"),
                        )
                        return text
                    except Exception as exc:
                        logger.warning("idle-warm: warm-путь недоступен (%s), откат на aux", exc)

                return super()._call_summary_llm(prompt, prompt_started_at)

            # ── вспомогательное ──────────────────────────────────────────
            def _warm_max_tokens(self) -> int | None:
                """Бюджет ответа: из конфигурации компрессора, с потолком."""
                for attr in ("summary_budget", "_summary_budget", "max_tokens"):
                    value = getattr(self, attr, None)
                    if isinstance(value, int) and value > 0:
                        return min(value, 8000)
                return None

        cls._cls = _Engine
        return _Engine


def signature_ok() -> tuple[bool, str]:
    """Проверка, что ядро ещё зовёт ``_call_summary_llm(prompt, ts)``."""
    try:
        Base = _import_base()
        sig = inspect.signature(Base._call_summary_llm)
        params = list(sig.parameters)
        if params[:3] != ["self", "prompt", "prompt_started_at"]:
            return False, f"неожиданная сигнатура: {params[:3]}"
        return True, "ok"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
