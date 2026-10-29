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

import contextlib
import logging
import os
import sqlite3
import sys
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
    "deepseek-v4.1": 3300,   # 55 мин: кеш живёт 85–90 мин, но агент выселяется из кэша gateway на 60-й
    "deepseek-v4": 3300,
    "muse-spark": 600,
}
TTL_FACTOR = 0.9


def _test_settings() -> dict:
    """Тестовый режим без правки кода: ~/.hermes/idle_compact_test.json.

    {"seconds": 300, "gate": 20000} — таймер и гейт для быстрой проверки.
    Файла нет — обычный режим.
    """
    try:
        import json

        path = _home() / "idle_compact_test.json"
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.debug("idle-compact: test settings read failed", exc_info=True)
        return {}
GATE_K = 1.5
THRESHOLD_TOKENS = 500_000   # сверено: compression.threshold_tokens (телеметрия)
TARGET_RATIO = 0.2           # сверено: tokens_after ~0.2 x threshold
HUMAN_SOURCES = {"telegram", "desktop", "cli", "tui", "messaging"}

_HERMES_HOME_FALLBACK = Path.home() / ".hermes"


def _home() -> Path:
    """Дом Hermes той же лесенкой, что ядро: override → HERMES_HOME → платформа
    (Windows: %LOCALAPPDATA%\\hermes, иначе ~/.hermes)."""
    try:
        from hermes_constants import get_hermes_home
        h = get_hermes_home()
        if h:
            return Path(h)
    except Exception:
        pass
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(os.path.expandvars(os.path.expanduser(env)))
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "hermes"
    return _HERMES_HOME_FALLBACK


def _state_db() -> str:
    return str(_home() / "state.db")

GATE_TOKENS = int(THRESHOLD_TOKENS * TARGET_RATIO * GATE_K)  # = 150000


_warned_no_config = False


def _live_target() -> int | None:
    """Живая цель из config.yaml. Нет конфига — None (скип, не вшитое)."""
    global _warned_no_config
    try:
        import yaml
        with open(str(_home() / "config.yaml"), "r", encoding="utf-8") as fh:
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
    test = _test_settings()
    if test.get("gate"):
        try:
            return int(test["gate"])
        except Exception:
            pass
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
    test = _test_settings()
    if test.get("seconds"):
        try:
            return float(test["seconds"])
        except Exception:
            pass
    return _ttl_for_model(model) * TTL_FACTOR


def _rough_tokens(session_id: str) -> int:
    """Грубая оценка размера активного транскрипта (chars/4). Только чтение."""
    con = sqlite3.connect(f"file:{_state_db()}?mode=ro", uri=True)
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
    con = sqlite3.connect(f"file:{_state_db()}?mode=ro", uri=True)
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
    con = sqlite3.connect(f"file:{_state_db()}?mode=ro", uri=True)
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

# Ожидающие таймеры переживают reload плагина: {session_id: {deadline, stamp, model}}
_PENDING_FILE = "idle_compact_pending.json"


def _pending_path() -> Path:
    return _home() / _PENDING_FILE


@contextlib.contextmanager
def _pending_guard():
    """Эксклюзивный доступ к файлу состояния между процессами.

    Без этого 89 загрузок плагина в сутки перетирали чужие записи: каждый
    процесс писал «только то, что восстановил он», и таймеры терялись.
    """
    path = _pending_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    fh = None
    try:
        fh = open(lock_path, "a+")
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except Exception:
        logger.debug("idle-compact: pending lock unavailable", exc_info=True)
    try:
        yield
    finally:
        if fh is not None:
            try:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                fh.close()
            except Exception:
                pass


def _load_pending() -> dict[str, dict]:
    try:
        import json
        data = json.loads(_pending_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("idle-compact: pending state read failed")
        return {}


def _save_pending(state: dict[str, dict]) -> None:
    try:
        import json
        path = _pending_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        logger.exception("idle-compact: pending state write failed")


def _remember(session_id: str, deadline: float, stamp: float, model: str) -> None:
    with _pending_guard():
        state = _load_pending()
        state[session_id] = {"deadline": deadline, "stamp": stamp, "model": model}
        _save_pending(state)


def _forget(session_id: str) -> None:
    with _pending_guard():
        state = _load_pending()
        if state.pop(session_id, None) is not None:
            _save_pending(state)


def _resident(session_id: str):
    """(agent, compressor, route) живой сессии или None. Только чтение."""
    try:
        from gateway.run import _gateway_runner_ref, _cached_agent_for_hygiene
    except Exception:
        return None
    try:
        runner = _gateway_runner_ref()
    except Exception:
        return None
    if runner is None:
        return None
    row = _session_row(session_id)
    route = (row.get("session_key") or "") if row else ""
    if not route:
        return None
    try:
        agent = _cached_agent_for_hygiene(runner, route)
    except Exception:
        return None
    if agent is None:
        return None
    comp = getattr(agent, "context_compressor", None)
    if comp is None:
        return None
    return agent, comp, route


def _estimate_history(comp, session_id: str) -> int | None:
    """Точная оценка ядра по истории из БД. None — не вышло."""
    try:
        db = getattr(comp, "_session_db", None)
        get_conv = getattr(db, "get_messages_as_conversation", None)
        if not callable(get_conv):
            return None
        history = get_conv(session_id)
        messages = [m for m in history if m.get("role") in {"user", "assistant", "tool"}]
        from agent.model_metadata import estimate_messages_tokens_rough
        return int(estimate_messages_tokens_rough(messages))
    except Exception:
        return None


def _fire(session_id: str, stamp: float, model: str, ctx=None) -> None:
    with _timers_lock:
        _timers.pop(session_id, None)
    _forget(session_id)
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
        # Гейт: сначала живой порог компрессора (покрывает и ratio-only режим
        # без threshold_tokens), иначе грубая оценка по config.yaml.
        floor = None
        exact = None
        res = _resident(session_id)
        if res is not None:
            _, comp, _ = res
            floor = _gate_floor(comp)
            if floor is not None:
                exact = _estimate_history(comp, session_id)
        if floor is None:
            gate = _live_gate()
            if gate is None:
                logger.info("idle-compact: session %s no live gate, skip", session_id)
                return
            floor = gate
        size = exact if exact is not None else _rough_tokens(session_id)
        idle_min = (time.time() - last_ts) / 60 if last_ts else -1
        if size <= floor:
            logger.info(
                "idle-compact: session %s size ~%d <= floor %d, skip",
                session_id, size, floor,
            )
            return
        if DRY_RUN or not WRITE_ENABLED or (ALLOWLIST and session_id not in ALLOWLIST):
            logger.info(
                "idle-compact dry-run: session %s model %s idle %.1fmin "
                "size ~%d > floor %d -> would compress (no writes)",
                session_id, model, idle_min, size, floor,
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
    _remember(session_id, stamp + delay, stamp, model)
    gate = _live_gate()
    logger.info(
        "idle-compact: timer %.0fs for session %s model %s (gate %s | test=%s ttl=%s)",
        delay, session_id, model, gate if gate is not None else "n/a",
        _test_settings() or "-", _ttl_for_model(model),
    )


def _gate_floor(compressor) -> int | None:
    """Пол из живого порога компрессора + anti-thrash. Нет данных — None (скип)."""
    # Тестовый режим: явный гейт из файла имеет приоритет.
    test = _test_settings()
    if test.get("gate"):
        try:
            return int(test["gate"])
        except Exception:
            pass
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
    try:
        from . import savings
        savings.begin_compaction(session_id, model, exact)
    except Exception:
        logger.exception("idle-compact: savings begin failed")

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
                # Включаем warm-путь: префикс = последний реальный запрос сессии.
                engine = getattr(agent, "context_compressor", None)
                warm_on = False
                cfg = _capture_for(session_id)
                if engine is not None and hasattr(engine, "begin_warm"):
                    try:
                        engine.begin_warm(
                            (cfg or {}).get("body"),
                            {
                                "base_url": (cfg or {}).get("base_url") or getattr(agent, "base_url", ""),
                                "api_mode": (cfg or {}).get("api_mode") or getattr(agent, "api_mode", ""),
                                "api_key": getattr(agent, "api_key", ""),
                            },
                        )
                        warm_on = True
                        if not cfg:
                            logger.info("idle-compact: warm без захвата — сжатие пойдёт через aux")
                    except Exception:
                        logger.debug("idle-compact: begin_warm failed", exc_info=True)
                try:
                    try:
                        agent._compress_context(messages, "", **kwargs)
                    except TypeError:
                        kwargs.pop("commit_fence", None)
                        agent._compress_context(messages, "", **kwargs)
                finally:
                    if warm_on and engine is not None:
                        try:
                            stats = engine.end_warm()
                            if stats:
                                logger.info(
                                    "idle-compact WARM: cache_read=%s свежий_вход=%s выход=%s",
                                    stats.get("cache_read"), stats.get("fresh_input"),
                                    stats.get("output_tokens"),
                                )
                        except Exception:
                            logger.debug("idle-compact: end_warm failed", exc_info=True)

            if ctx is not None:
                ctx.run(run)
            else:
                run()
            logger.info("idle-compact COMPRESS DONE: session %s", session_id)
            # Размер после сжатия — тем же оценщиком, что и до
            after = None
            try:
                from agent.model_metadata import estimate_messages_tokens_rough
                new_history = get_conv(session_id)
                _nh = new_history if isinstance(new_history, (list, tuple)) else []
                new_msgs = [m for m in _nh if isinstance(m, dict) and m.get("role") in {"user", "assistant", "tool"}]
                after = int(estimate_messages_tokens_rough(new_msgs))
            except Exception:
                after = None
            try:
                from . import savings
                savings.end_compaction(session_id, after)
                savings.log_summary(session_id)
            except Exception:
                logger.exception("idle-compact: savings end failed")
        except Exception:
            logger.exception("idle-compact COMPRESS FAILED: %s", session_id)
            try:
                from . import savings
                savings.abort_compaction(session_id)
            except Exception:
                pass
        finally:
            evict = getattr(runner, "_evict_cached_agent", None)
            if callable(evict):
                try:
                    evict(route)
                except Exception:
                    pass

    th = threading.Thread(target=work, daemon=True, name=f"idle-compact-{session_id[:8]}")
    th.start()


def _restore_pending() -> None:
    """Восстанавливаем таймеры, пережившие reload: дожидаемся остатка срока."""
    state = _load_pending()
    if not state:
        return
    now = time.time()
    restored = dropped = 0
    for session_id, entry in list(state.items()):
        try:
            deadline = float(entry.get("deadline") or 0)
            stamp = float(entry.get("stamp") or 0)
            model = str(entry.get("model") or "")
        except Exception:
            dropped += 1
            continue
        if deadline <= 0 or stamp <= 0:
            dropped += 1
            continue
        row = _session_row(session_id)
        # Сессия исчезла/закрыта/не human — забываем
        if not row or row.get("source") not in HUMAN_SOURCES or row.get("ended_at"):
            dropped += 1
            continue
        # Уже был активный ход после нашего таймера — ждать нечего
        if (row.get("last_activity_at") or 0) > stamp:
            dropped += 1
            continue
        delay = max(0.0, deadline - now)
        with _timers_lock:
            old = _timers.pop(session_id, None)
            if old is not None:
                old.cancel()
            t = threading.Timer(delay, _fire, args=(session_id, stamp, model, None))
            t.daemon = True
            _timers[session_id] = t
            t.start()
        restored += 1
        logger.info(
            "idle-compact: restored timer %.0fs for session %s model %s",
            delay, session_id, model,
        )
    fresh = {sid: e for sid, e in state.items()
             if sid in _timers}
    if dropped or len(fresh) != len(state):
        _save_pending(fresh)
    logger.info(
        "idle-compact: pending restore done — restored %s, dropped %s", restored, dropped)


# Захваченные запросы: session_id -> {"body": payload, "model", "base_url", "api_mode"}
_captures: dict[str, dict] = {}
_captures_lock = threading.Lock()
# Свой движок (если ядро его приняло)
_WARM_ENGINE = None


def _on_pre_api_request(**kw) -> None:
    """Запоминаем последний реальный запрос — он станет префиксом warm-сжатия.

    Хук отдаёт payload в обёртке ``{"method": "POST", "body": {...}}``
    (см. ``_api_request_payload_for_hook``), поэтому тело разворачиваем.
    """
    sid = kw.get("session_id") or ""
    raw = kw.get("request")
    if not sid or not isinstance(raw, dict):
        return
    body = raw.get("body") if isinstance(raw.get("body"), dict) else raw
    if not isinstance(body, dict) or not body:
        return
    try:
        import copy as _copy

        entry = {
            "body": _copy.deepcopy(body),
            "model": kw.get("model") or "",
            "base_url": kw.get("base_url") or "",
            "api_mode": kw.get("api_mode") or "",
            "ts": time.time(),
        }
    except Exception:
        logger.debug("idle-compact: capture copy failed", exc_info=True)
        return
    with _captures_lock:
        _captures[sid] = entry
    logger.debug("idle-compact: captured %s keys=%s mode=%s", sid, sorted(body)[:8], entry["api_mode"])


def _capture_for(session_id: str) -> dict | None:
    with _captures_lock:
        entry = _captures.get(session_id)
    if not entry:
        return None
    # Старше TTL кеша — префикс уже не тёплый, повтор бессмысленен
    if time.time() - float(entry.get("ts") or 0) > 7200:
        return None
    return entry


_watchdog_started = False


def _sweep_due() -> int:
    """Сработать по всем просроченным дедлайнам. Возвращает число запусков.

    Нужен потому, что одиночный threading.Timer живёт только в своём процессе:
    при reload/спавне он исчезает, и ожидание молча теряется.
    """
    now = time.time()
    with _pending_guard():
        state = _load_pending()
        due = [(sid, e) for sid, e in state.items() if float(e.get("deadline") or 0) <= now]
    fired = 0
    for sid, entry in due:
        try:
            stamp = float(entry.get("stamp") or 0)
            model = str(entry.get("model") or "")
            deadline = float(entry.get("deadline") or 0)
            ttl = _delay_for_model(model)
            # Совсем старые дедлайны не трогаем: кеш давно мёртв, сожмёт ядро при возврате.
            if now - deadline > max(ttl * 2, 3600):
                _forget(sid)
                logger.info("idle-compact: дедлайн %s устарел, ожидание снято", sid)
                continue
            logger.info("idle-compact: часовой догоняет дедлайн сессии %s", sid)
            _fire(sid, stamp, model, None)
            fired += 1
        except Exception:
            logger.exception("idle-compact: sweep failed for %s", sid)
    return fired


def _watchdog() -> None:
    while True:
        time.sleep(30)
        try:
            _sweep_due()
        except Exception:
            logger.exception("idle-compact: watchdog tick failed")


def _boot_async() -> None:
    """Тяжёлая инициализация в стороне от register(): иначе ядро ругается
    «load timed out after 10s» и плагин считается недогруженным."""

    def _run() -> None:
        time.sleep(2)
        try:
            from gateway.run import _gateway_runner_ref
        except Exception:
            return
        try:
            if _gateway_runner_ref() is None:
                return
        except Exception:
            return
        try:
            _restore_pending()
        except Exception:
            logger.exception("idle-compact: pending restore failed")
        try:
            _start_watchdog()
        except Exception:
            logger.exception("idle-compact: watchdog start failed")

    threading.Thread(target=_run, name="idle-compact-boot", daemon=True).start()


def _start_watchdog() -> None:
    global _watchdog_started
    if _watchdog_started:
        return
    _watchdog_started = True
    t = threading.Thread(target=_watchdog, name="idle-compact-watchdog", daemon=True)
    t.start()
    logger.info("idle-compact: часовой запущен (проверка раз в 30 сек)")


def register(ctx) -> None:
    """Точка входа плагина: движок, хук конца хода и хук захвата запроса."""
    # Свой контекст-движок: штатный компрессор + warm-путь суммирования.
    # Если не удастся зарегистрировать — плагин продолжит работать как раньше (aux-путь).
    global _WARM_ENGINE
    try:
        from .warm_engine import IdleWarmCompressor, signature_ok

        ok, why = signature_ok()
        if not ok:
            logger.warning("idle-compact: warm-движок отключён — %s", why)
        else:
            EngineClass = IdleWarmCompressor.build()
            engine = EngineClass(model="")
            if ctx.register_context_engine(engine) is not None:
                _WARM_ENGINE = engine
                logger.info("idle-compact: зарегистрирован свой контекст-движок (штатный компрессор + warm)")
            else:
                logger.warning("idle-compact: ядро не приняло контекст-движок — warm отключён")
    except Exception:
        logger.exception("idle-compact: регистрация warm-движка не удалась")

    try:
        from . import savings

        ctx.register_hook("pre_api_request", _on_pre_api_request)
    except Exception:
        logger.exception("idle-compact: не удалось подписаться на pre_api_request")

    ctx.register_hook("on_session_end", _on_session_end)
    try:
        from . import savings
        ctx.register_hook("post_api_request", savings.on_post_api_request)
    except Exception:
        logger.exception("idle-compact: savings hooks not registered")
    logger.info(
        "idle-compact 0.6.5 loaded: DRY_RUN=%s WRITE_ENABLED=%s gate=live-per-fire",
        DRY_RUN, WRITE_ENABLED,
    )
    try:
        _boot_async()
    except Exception:
        logger.exception("idle-compact: boot failed")


def unregister(ctx) -> None:
    with _timers_lock:
        for t in _timers.values():
            try:
                t.cancel()
            except Exception:
                pass
        _timers.clear()
    logger.info("idle-compact unloaded, timers cancelled")
