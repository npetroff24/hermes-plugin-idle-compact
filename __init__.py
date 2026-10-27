"""idle-compact — фоновое сжатие простаивающих сессий ДО истечения prompt-cache.

Этап 0 (этот файл): только dry-run. Ни одной записи в БД, только чтение
state.db в read-only и строки в лог вида «сжал бы N→M».
Боевое сжатие появится следующим этапом за флагом WRITE_ENABLED.

Логика:
- после каждого хода (хук on_session_end) ставим/сбрасываем таймер сессии:
  delay = TTL(модель) x 0.9 (spark 9 мин, deepseek ~76-85 мин);
- таймер сработал: сессия всё ещё тиха, источник human, размер > гейта —
  логируем «would compress», ничего не пишем;
- гейт = цель x 1.5, цель = threshold_tokens x target_ratio
  (сейчас 500000 x 0.2 = 100000 -> гейт 150000).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Конфиг (этап 0: всё константами; из config.yaml заберём следующим этапом)
# ---------------------------------------------------------------------------

DRY_RUN = False           # боевое: все human-сессии
WRITE_ENABLED = True      # боевое сжатие включено
# Пустой = все human-сессии; непустой = только перечисленные (тесты/откат)
ALLOWLIST: set[str] = set()

TTL_BY_MODEL_SUBSTR: dict[str, int] = {  # секунды, longest-match wins
    "deepseek-v4.1": 5100,
    "deepseek-v4": 5100,
    "muse-spark": 600,
}
TTL_FACTOR = 0.9
# ВРЕМЕННО ДЛЯ ТЕСТА (откатить): мгновенный таймер и низкий гейт
TEST_DELAY_SECONDS: float | None = None
TEST_GATE_TOKENS: int | None = None  # тест пишущего пути; формула гейта (цельx1.5) проверена отдельно
GATE_K = 1.5
THRESHOLD_TOKENS = 500_000   # сверено: compression.threshold_tokens (телеметрия)
TARGET_RATIO = 0.2           # сверено: tokens_after ~0.2 x threshold
HUMAN_SOURCES = {"telegram", "desktop", "cli", "tui", "messaging"}

_HERMES_HOME = Path.home() / ".hermes"
_STATE_DB = str(_HERMES_HOME / "state.db")

GATE_TOKENS = int(THRESHOLD_TOKENS * TARGET_RATIO * GATE_K)  # = 150000
if TEST_GATE_TOKENS is not None:
    GATE_TOKENS = TEST_GATE_TOKENS


_warned_no_config = False


def _live_target() -> int | None:
    """Живая цель из config.yaml. Нет конфига — None (скип, не вшитое)."""
    global _warned_no_config
    try:
        import yaml
        with open(str(_HERMES_HOME / "config.yaml"), "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        comp = cfg.get("compression") or {}
        thr = comp.get("threshold_tokens")
        ratio = comp.get("target_ratio")
        if thr is None or ratio is None:
            raise ValueError("no compression.threshold_tokens/target_ratio in config")
        return int(int(thr) * float(ratio))
    except Exception as exc:
        if not _warned_no_config:
            _warned_no_config = True
            logger.warning("idle-compact: no live target (%s), skipping until config readable", exc)
        return None


def _live_gate() -> int | None:
    if TEST_GATE_TOKENS is not None:
        return TEST_GATE_TOKENS
    target = _live_target()
    return int(target * GATE_K) if target else None


def _ttl_for_model(model: str) -> int:
    m = (model or "").strip().lower()
    best = -1
    ttl = 600
    for sub, t in TTL_BY_MODEL_SUBSTR.items():
        if sub in m and len(sub) > best:
            best = len(sub)
            ttl = t
    return ttl


def _delay_for_model(model: str) -> float:
    if TEST_DELAY_SECONDS is not None:
        return TEST_DELAY_SECONDS
    return _ttl_for_model(model) * TTL_FACTOR


def _rough_tokens(session_id: str) -> int:
    """Грубая оценка размера активного транскрипта (chars/4). Только чтение."""
    con = sqlite3.connect(f"file:{_STATE_DB}?mode=ro", uri=True)
    try:
        cur = con.execute(
            "SELECT COALESCE(SUM(LENGTH(content)), 0) FROM messages "
            "WHERE session_id = ? AND active = 1",
            (session_id,),
        )
        chars = cur.fetchone()[0] or 0
    finally:
        con.close()
    return int(chars // 4)


def _session_row(session_id: str) -> dict | None:
    con = sqlite3.connect(f"file:{_STATE_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(
            "SELECT id, source, model, session_key, message_count, api_call_count, "
            "archived, ended_at, last_activity_at "
            "FROM sessions WHERE id = ?",
            (session_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def scan_candidates() -> list[dict]:
    """Кандидаты прямо сейчас (для ручного dry-run): большие human-сессии."""
    con = sqlite3.connect(f"file:{_STATE_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(
            "SELECT id, source, model, message_count, last_activity_at "
            "FROM sessions WHERE archived = 0 AND ended_at IS NULL "
            "AND source IN ('telegram','desktop') "
            "ORDER BY last_activity_at DESC LIMIT 50"
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        con.close()
    out = []
    for r in rows:
        try:
            size = _rough_tokens(r["id"])
        except Exception:
            continue
        idle_min = (time.time() - (r["last_activity_at"] or 0)) / 60
        if size > GATE_TOKENS:
            out.append({**r, "rough_tokens": size, "idle_min": round(idle_min, 1)})
    return out


# ---------------------------------------------------------------------------
# Планировщик таймеров
# ---------------------------------------------------------------------------

_timers: dict[str, threading.Timer] = {}
_timers_lock = threading.Lock()


def _fire(session_id: str, stamp: float, model: str, ctx=None) -> None:
    with _timers_lock:
        _timers.pop(session_id, None)
    try:
        row = _session_row(session_id)
        if not row:
            return
        if row.get("source") not in HUMAN_SOURCES:
            return
        last_ts = row.get("last_activity_at") or 0
        if last_ts > stamp:
            logger.info("idle-compact: session %s active again, skip", session_id)
            return
        size = _rough_tokens(session_id)
        idle_min = (time.time() - last_ts) / 60 if last_ts else -1
        gate = _live_gate()
        if gate is None:
            logger.info("idle-compact: session %s no live gate, skip", session_id)
            return
        if size <= gate:
            logger.info(
                "idle-compact: session %s size ~%d <= gate %d, skip",
                session_id, size, gate,
            )
            return
        if DRY_RUN or not WRITE_ENABLED or (ALLOWLIST and session_id not in ALLOWLIST):
            logger.info(
                "idle-compact dry-run: session %s model %s idle %.1fmin "
                "size ~%d > gate %d -> would compress (no writes)",
                session_id, model, idle_min, size, gate,
            )
            return
        # -- боевое сжатие: allowlist как опциональный ограничитель --
        if WRITE_ENABLED and (not ALLOWLIST or session_id in ALLOWLIST):
            _do_compress(session_id, model, size, ctx)
            return
        logger.info("idle-compact: write path not yet enabled, skip %s", session_id)
    except Exception:
        logger.exception("idle-compact: fire failed for %s", session_id)


def _on_session_end(session_id: str = "", **kwargs) -> None:
    if not session_id:
        return
    try:
        row = _session_row(session_id)
    except Exception:
        logger.exception("idle-compact: db read failed")
        return
    if not row or row.get("source") not in HUMAN_SOURCES:
        return
    model = row.get("model") or ""
    delay = _delay_for_model(model)
    stamp = time.time()
    try:
        from contextvars import copy_context
        ctx = copy_context()  # секретный scope хода — нужен worker'у для API-ключей
    except Exception:
        ctx = None
    with _timers_lock:
        old = _timers.pop(session_id, None)
        if old is not None:
            old.cancel()
        t = threading.Timer(delay, _fire, args=(session_id, stamp, model, ctx))
        t.daemon = True
        _timers[session_id] = t
        t.start()
    gate = _live_gate()
    logger.info(
        "idle-compact: timer %.0fs for session %s model %s (gate %s)",
        delay, session_id, model, gate if gate is not None else "n/a",
    )


def _gate_floor(compressor) -> int | None:
    """Пол из живого порога компрессора + anti-thrash. Нет данных — None (скип)."""
    target = None
    try:
        thr = int(getattr(compressor, "threshold_tokens", 0) or 0)
        ratio = float(getattr(compressor, "summary_target_ratio", 0) or 0)
        if thr > 0 and ratio > 0:
            target = int(thr * ratio)
    except Exception:
        target = None
    if target is None:
        target = _live_target()
    if target is None:
        return None
    floor = int(target * GATE_K)
    last = getattr(compressor, "last_compression_rough_tokens", 0)
    if isinstance(last, int) and not isinstance(last, bool) and last > 0:
        floor = max(floor, last + target)
    return floor


def _do_compress(session_id: str, model: str, size: int, ctx) -> None:
    """Боевое сжатие через resident-агента: весь коммит делает ядро."""
    try:
        from gateway.run import _gateway_runner_ref, _cached_agent_for_hygiene
    except Exception:
        logger.exception("idle-compact: gateway import failed")
        return
    runner = _gateway_runner_ref()
    if runner is None:
        logger.info("idle-compact: no gateway runner, skip %s", session_id)
        return
    row = _session_row(session_id)
    route = (row.get("session_key") or "") if row else ""
    if not route:
        logger.info("idle-compact: no route for %s", session_id)
        return
    try:
        agent = _cached_agent_for_hygiene(runner, route)
    except Exception:
        logger.exception("idle-compact: cached agent lookup failed")
        return
    if agent is None:
        logger.info("idle-compact: no resident agent for %s, skip", session_id)
        return
    if not getattr(agent, "compression_enabled", False):
        return
    if getattr(agent, "api_mode", "") == "codex_app_server":
        return
    if getattr(agent, "compression_checkpoint_required", False):
        return
    comp = getattr(agent, "context_compressor", None)
    if comp is None:
        return
    if getattr(comp, "awaiting_real_usage_after_compression", False):
        return
    cd = getattr(comp, "get_active_compression_failure_cooldown", None)
    try:
        if callable(cd) and cd():
            return
    except Exception:
        return
    db = getattr(comp, "_session_db", None)
    get_conv = getattr(db, "get_messages_as_conversation", None)
    if not callable(get_conv):
        logger.info("idle-compact: no db handle on compressor, skip %s", session_id)
        return
    try:
        history = get_conv(session_id)
    except Exception:
        logger.exception("idle-compact: history load failed")
        return
    messages = [m for m in history if m.get("role") in {"user", "assistant", "tool"}]
    try:
        from agent.model_metadata import estimate_messages_tokens_rough
        exact = int(estimate_messages_tokens_rough(messages))
    except Exception:
        exact = size
    floor = _gate_floor(comp)
    if floor is None:
        logger.info("idle-compact: session %s no live floor, skip", session_id)
        return
    if exact <= floor:
        logger.info(
            "idle-compact: session %s exact ~%d <= floor %d, skip",
            session_id, exact, floor)
        return
    logger.info(
        "idle-compact COMPRESS START: session %s model %s exact ~%d",
        session_id, model, exact)

    def work() -> None:
        try:
            fence = None
            try:
                from agent.conversation_compression import CompressionCommitFence
                fence = CompressionCommitFence()
            except Exception:
                fence = None
            kwargs: dict = {"approx_tokens": exact, "task_id": session_id,
                            "trigger": "idle_background"}
            if fence is not None:
                kwargs["commit_fence"] = fence

            def run() -> None:
                try:
                    agent._compress_context(messages, "", **kwargs)
                except TypeError:
                    kwargs.pop("commit_fence", None)
                    agent._compress_context(messages, "", **kwargs)

            if ctx is not None:
                ctx.run(run)
            else:
                run()
            logger.info("idle-compact COMPRESS DONE: session %s", session_id)
        except Exception:
            logger.exception("idle-compact COMPRESS FAILED: %s", session_id)
        finally:
            evict = getattr(runner, "_evict_cached_agent", None)
            if callable(evict):
                try:
                    evict(route)
                except Exception:
                    pass

    th = threading.Thread(target=work, daemon=True, name=f"idle-compact-{session_id[:8]}")
    th.start()


def register(ctx) -> None:
    """Точка входа плагина: только подписка на хук, тяжёлого нет."""
    ctx.register_hook("on_session_end", _on_session_end)
    logger.info(
        "idle-compact 0.2.0 loaded: DRY_RUN=%s WRITE_ENABLED=%s gate=%s",
        DRY_RUN, WRITE_ENABLED, _live_gate(),
    )


def unregister(ctx) -> None:
    with _timers_lock:
        for t in _timers.values():
            try:
                t.cancel()
            except Exception:
                pass
        _timers.clear()
    logger.info("idle-compact unloaded, timers cancelled")
