"""idle-compact — фоновое сжатие простаивающих сессий ДО истечения prompt-cache.

Логика:
- после каждого хода (хук on_session_end) ставим/сбрасываем таймер сессии:
  delay = TTL(модель) x 0.9 (таблица TTL_BY_MODEL_SUBSTR + замер/дефолт);
- таймер сработал: сессия всё ещё тиха, источник human, размер > гейта —
  сжимаем ядром (штатный путь) с warm-суммированием по кешу;
- гейт = цель x 2, цель = threshold_tokens x target_ratio
  (живой порог компрессора; без него — из config.yaml).
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

try:  # POSIX: flock для файла состояния между процессами
    import fcntl
except ImportError:
    fcntl = None
try:  # Windows: msvcrt вместо fcntl
    import msvcrt
except ImportError:
    msvcrt = None


def _lock_fh(fh) -> None:
    """Эксклюзивная блокировка файла: fcntl на POSIX, msvcrt на Windows."""
    if fcntl is not None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    elif msvcrt is not None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
    # иначе нечем лочить — работаем без лока (однопроцессный режим)


def _unlock_fh(fh) -> None:
    if fcntl is not None:
        with contextlib.suppress(Exception):
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:
        with contextlib.suppress(Exception):
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

# ---------------------------------------------------------------------------
# Конфиг
# ---------------------------------------------------------------------------

DRY_RUN = False           # боевое: все human-сессии
WRITE_ENABLED = True      # боевое сжатие включено
# Пустой = все human-сессии; непустой = только перечисленные (тесты/откат)
ALLOWLIST: set[str] = set()

# Единственный источник правды о TTL — model_ttl.py (его же читает savings.py).
from .model_ttl import DEFAULT_TTL_SECONDS, TTL_BY_MODEL_SUBSTR, ttl_for_model

TTL_FACTOR = 0.9

# Модели, для которых уже жаловались на обрезку таймера выселением (один раз за процесс).
_warned_evict_cap: set[str] = set()


def _test_settings(session_id: str | None = None) -> dict:
    """Тестовый режим без правки кода: <HERMES_HOME>/idle_compact_test.json.

    {"seconds": 300, "gate": 20000} — таймер и гейт для быстрой проверки.
    Поле "session" ограничивает режим одной сессией (префикс ID):
    {"seconds": 10, "gate": 150000, "session": "local-c11b24"} — остальные
    сессии работают штатно. Файла нет — обычный режим.
    """
    try:
        import json

        path = _home() / "idle_compact_test.json"
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        want = str(data.get("session") or "").strip()
        if want:
            have = str(session_id or "")
            if not have or not have.startswith(want):
                return {}
        return data
    except Exception:
        logger.debug("idle-compact: test settings read failed", exc_info=True)
        return {}
GATE_K = 2.0          # сжимаем только если контекст >= 2x цели сжатия
THRESHOLD_TOKENS = 500_000   # запасной порог, если конфиг нечитаем
TARGET_RATIO = 0.2           # сверено: tokens_after ~0.2 x threshold
HUMAN_SOURCES = {"telegram", "desktop", "cli", "tui", "messaging", "local"}

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


def _live_target(window_tokens: int | None = None) -> int | None:
    """Живая цель из config.yaml. Нет конфига — None (скип, не вшитое).

    Понимает оба вида порога: ``threshold_tokens`` (абсолютные токены) и
    ``threshold`` (доля окна 0..1). Долю умножаем только на ФАКТИЧЕСКОЕ окно
    модели (``window_tokens`` из живого компрессора); выдумывать окно нельзя —
    иначе для модели с окном 200К цель завышается впятеро.

    Без секции в конфиге — дефолты ядра (порог 0.50, для окон <512К пол 0.75),
    чтобы на чистой установке плагин работал, а не молчал.
    """
    global _warned_no_config
    try:
        import yaml
        with open(str(_home() / "config.yaml"), "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        comp = cfg.get("compression") or {}
        thr = comp.get("threshold_tokens")
        if thr is None and comp.get("threshold") is not None:
            frac = float(comp.get("threshold"))
            if frac <= 1:
                if not window_tokens or window_tokens <= 0:
                    raise ValueError("threshold is a fraction, but model window is unknown")
                thr = int(frac * int(window_tokens))
            else:
                thr = int(frac)
        if thr is None and window_tokens and window_tokens > 0:
            # Дефолт ядра: 0.50 окна, пол 0.75 для окон <512К.
            frac = 0.75 if int(window_tokens) < 512 * 1024 else 0.50
            thr = int(frac * int(window_tokens))
        ratio = comp.get("target_ratio")
        if ratio is None:
            ratio = TARGET_RATIO
        if thr is None:
            raise ValueError("no compression.threshold_tokens/threshold in config")
        return int(int(thr) * float(ratio))
    except Exception as exc:
        if not _warned_no_config:
            _warned_no_config = True
            logger.warning("idle-compact: no live target (%s), skipping until config readable", exc)
        return None


def _plugin_cfg() -> dict:
    """Секция plugins.idle_compact из config.yaml (или пустой словарь)."""
    try:
        import yaml
        with open(str(_home() / "config.yaml"), "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        return ((cfg.get("plugins") or {}).get("idle_compact") or {}) if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _hub_auto_ratio() -> float:
    """Порог автосжатия из хаба настроек (`plugins.entries.*.settings`)."""
    try:
        import yaml
        with open(str(_home() / "config.yaml"), "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        entries = (cfg.get("plugins") or {}).get("entries") or {}
        if not isinstance(entries, dict):
            return 0.0
        ordered = []
        if isinstance(entries.get("idle-compact"), dict):
            ordered.append(entries["idle-compact"])
        ordered.extend(v for k, v in entries.items()
                       if k != "idle-compact" and isinstance(v, dict))
        for entry in ordered:
            settings = entry.get("settings")
            if not isinstance(settings, dict):
                continue
            val = settings.get("auto_compact_ratio")
            if val is not None and float(val) > 0:
                return float(val)
    except Exception:
        pass
    return 0.0


def _gate_fraction(session_id: str | None = None) -> float:
    """Доля окна модели, после которой включается фоновое сжатие.

    Порядок: хаб настроек → ``<HERMES_HOME>/idle_compact_test.json`` (поле
    ``gate_fraction``) → ``plugins.idle_compact.gate_fraction`` в config.yaml.
    Ноль/нет — работает прежняя формула 2 × цель.
    """
    hub = _hub_auto_ratio()
    if hub > 0:
        return hub
    test = _test_settings(session_id)
    for src in (test.get("gate_fraction"), _plugin_cfg().get("gate_fraction")):
        try:
            if src is not None and float(src) > 0:
                return float(src)
        except Exception:
            continue
    return 0.0


def _live_gate(comp=None, session_id: str | None = None) -> int | None:
    test = _test_settings(session_id)
    if test.get("gate"):
        try:
            return int(test["gate"])
        except Exception:
            pass
    frac = _gate_fraction(session_id)
    if frac > 0:
        window = int(getattr(comp, "context_length", 0) or 0)
        if window > 0:
            # Порог от ФАКТИЧЕСКОГО окна модели: не зависит от lean/legacy,
            # threshold и target_ratio — одна понятная величина.
            return int(frac * window)
    target = _live_target()
    return int(target * GATE_K) if target else None


def _ttl_for_model(model: str) -> int:
    """TTL модели из общего модуля model_ttl.py."""
    return ttl_for_model(model)


def _evict_ttl() -> float:
    """TTL выселения агента из кеша gateway: config agent.agent_cache.idle_ttl_secs.

    Дефолт ядра — 3600 с (``_AGENT_CACHE_IDLE_TTL_SECS`` в gateway/run.py).
    Плагин агента только подглядывает, поэтому таймер обязан срабатывать
    раньше выселения — иначе «agent evicted/absent» и скип.
    """
    try:
        import yaml
        with open(str(_home() / "config.yaml"), "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        sec = ((cfg.get("agent") or {}).get("agent_cache") or {}) if isinstance(cfg, dict) else {}
        val = float(sec.get("idle_ttl_secs") or 0)
        if val > 0:
            return val
    except Exception:
        pass
    return 3600.0


def _delay_for_model(model: str, session_id: str | None = None) -> float:
    test = _test_settings(session_id)
    if test.get("seconds"):
        try:
            return float(test["seconds"])
        except Exception:
            pass
    # Задержка = min(TTL модели, TTL выселения агента) x 0.9: сжатие идёт
    # пока живы И провайдерский кеш, И агент в памяти gateway.
    # Карта TTL нужна также для вердикта «был бы кеш тёплым»
    # и отсечки устаревших дедлайнов.
    model_ttl = _ttl_for_model(model)
    evict = _evict_ttl()
    if model_ttl > evict and model not in _warned_evict_cap:
        _warned_evict_cap.add(model)
        logger.warning(
            "idle-compact: %s: кеш живёт %dс, но Hermes выселяет агент "
            "через %dс — таймер урезан до %dс. Поднимите "
            "agent.agent_cache.idle_ttl_secs для полного TTL.",
            model, model_ttl, int(evict), int(min(model_ttl, evict) * TTL_FACTOR),
        )
    return float(min(model_ttl, evict)) * TTL_FACTOR


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
            "AND source IN ('telegram','desktop','cli','tui','messaging','local') "
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
        fh = open(lock_path, "a+b")
        if msvcrt is not None and fcntl is None and fh.tell() == 0:
            fh.write(b"\x00")  # msvcrt лочит байт-диапазон: файл не должен быть пустым
            fh.flush()

        _lock_fh(fh)
    except Exception:
        logger.debug("idle-compact: pending lock unavailable", exc_info=True)
    try:
        yield
    finally:
        if fh is not None:
            try:
                _unlock_fh(fh)

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


def _pid_alive(pid: int) -> bool:
    """Жив ли процесс-владелец дедлайна (POSIX и Windows)."""
    if pid <= 0:
        return False
    try:
        import os as _os
        if _os.name == "nt":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        _os.kill(pid, 0)
        return True
    except Exception:
        return False


def _remember(session_id: str, deadline: float, stamp: float, model: str) -> None:
    """Запомнить дедлайн вместе с pid процесса-владельца.

    Пендинг общий для gateway и desktop. Без владельца чужой процесс забирал
    дедлайн, не находил агента, удалял запись и скипал — ожидание терялось
    навсегда (реальный случай 09:48).
    """
    with _pending_guard():
        state = _load_pending()
        state[session_id] = {"deadline": deadline, "stamp": stamp, "model": model,
                             "pid": os.getpid()}
        _save_pending(state)


def _forget(session_id: str) -> None:
    with _pending_guard():
        state = _load_pending()
        if state.pop(session_id, None) is not None:
            _save_pending(state)


def _resident(session_id: str):
    """(agent, compressor, route, runner) живой сессии или None. Только чтение.

    Причину отсутствия пишем в лог (один раз за сессию): без неё диагностика
    «api_size=n/a» бесполезна — непонятно, нет runner, нет ключа или нет агента.

    ``runner`` возвращаем наружу: он нужен вызывающему, чтобы после сжатия
    вернуть агента в кеш. Раньше он терялся и eviction на обычном пути падал.
    """
    try:
        from gateway.run import _gateway_runner_ref
    except Exception as exc:
        _resident_reason(session_id, f"gateway import failed: {type(exc).__name__}")
        return None
    runner = None
    try:
        runner = _gateway_runner_ref()
    except Exception as exc:
        _resident_reason(session_id, f"runner_ref failed: {type(exc).__name__}")
        return None
    if runner is None:
        desktop = _resident_desktop(session_id)
        if desktop is not None:
            return desktop
        _resident_reason(session_id, "no gateway runner and no desktop session in this process")
        return None
    row = _session_row(session_id)
    route = (row.get("session_key") or "") if row else ""
    if not route:
        _resident_reason(session_id, "session has no session_key in db")
        return None
    try:
        from gateway.run import _cached_agent_for_hygiene
    except Exception as exc:
        _resident_reason(session_id, f"cache lookup import failed: {type(exc).__name__}")
        return None
    try:
        agent = _cached_agent_for_hygiene(runner, route)
    except Exception as exc:
        _resident_reason(session_id, f"cache lookup failed: {type(exc).__name__}")
        return None
    if agent is None:
        desktop = _resident_desktop(session_id)
        if desktop is not None:
            return desktop
        _resident_reason(session_id, f"agent evicted/absent (route={route[:40]})")
        return None
    comp = getattr(agent, "context_compressor", None)
    if comp is None:
        _resident_reason(session_id, "agent has no context_compressor")
        return None
    return agent, comp, route, runner


def _resident_desktop(session_id: str):
    """Живой агент desktop-сессии: tui_gateway держит его в своём реестре.

    В процессе ``hermes serve`` нет gateway-runner, поэтому единственный
    доступ к агенту — реестр ``tui_gateway.server._sessions``. Публичного
    API для этого нет; все обращения обёрнуты в try/except, чтобы смена
    внутренней структуры ломала только фоновое сжатие, а не сам агент.
    """
    try:
        from tui_gateway import server as tui
    except Exception as exc:
        _resident_reason(session_id, f"tui_gateway import failed: {type(exc).__name__}: {exc}")
        return None
    sessions = getattr(tui, "_sessions", None)
    if not isinstance(sessions, dict):
        _resident_reason(session_id, f"tui_gateway._sessions is {type(sessions).__name__}")
        return None
    # Подробный снимок реестра: без него не видно, почему сессия не находится.
    try:
        _snap = []
        for _k, _s in list(sessions.items())[:8]:
            _a = _s.get("agent") if isinstance(_s, dict) else None
            _snap.append(f"{str(_k)[:12]}→{getattr(_a, 'session_id', None)}")
        logger.info(
            "idle-compact: реестр desktop — сессий=%d, ищем=%s, состав=%s",
            len(sessions), session_id[-12:], "; ".join(_snap) or "пусто",
        )
    except Exception:
        pass
    for _key, sess in list(sessions.items()):
        if not isinstance(sess, dict):
            continue
        agent = sess.get("agent")
        if agent is None:
            continue
        if getattr(agent, "session_id", None) == session_id or sess.get("session_key") == session_id:
            comp = getattr(agent, "context_compressor", None)
            if comp is None:
                _resident_reason(
                    session_id,
                    f"desktop agent has no context_compressor (dir has "
                    f"{sum(1 for a in dir(agent) if 'compress' in a.lower())} similar attrs)",
                )
                return None
            return agent, comp, str(sess.get("session_key") or session_id), None
    _resident_reason(
        session_id,
        f"desktop registry has no match (keys: {[str(k)[:10] for k in list(sessions)[:5]]})",
    )
    return None


_warned_resident: set[str] = set()
_last_warm_usage: dict | None = None
_history_db = None


def _resident_reason(session_id: str, reason: str) -> None:
    """Причина отсутствия агента.

    Логируем каждый раз: одноразовый лог уже стоил нескольких циклов отладки —
    первая причина оставалась в прошлом, а последующие срабатывания молчали.
    Таймер срабатывает раз в несколько минут, спама это не даёт.
    """
    logger.info("idle-compact: нет живого агента для %s — %s", session_id, reason)


def _history_getter(comp):
    """Функция ``(session_id) -> messages``: публичный SessionDB, иначе приватный.

    Публичный ``hermes_state.SessionDB(read_only=True)`` — стабильный API;
    приватный ``comp._session_db`` оставляем запасным путём для сборок, где
    публичного чтения ещё нет.
    """
    global _history_db
    try:
        if _history_db is None:
            from hermes_state import SessionDB
            # Без явного пути: SessionDB сам берёт DEFAULT_DB_PATH и учитывает
            # переопределения (--db, профили), иначе читаем чужую базу.
            _history_db = SessionDB(read_only=True)
        fn = getattr(_history_db, "get_messages_as_conversation", None)
        if callable(fn):
            return fn
    except Exception:
        pass
    db = getattr(comp, "_session_db", None)
    fn = getattr(db, "get_messages_as_conversation", None)
    return fn if callable(fn) else None


def _estimate_history(comp, session_id: str) -> int | None:
    """Точная оценка ядра по истории из БД. None — не вышло."""
    try:
        get_conv = _history_getter(comp)
        if not callable(get_conv):
            return None
        history = get_conv(session_id)
        if not history:
            # Публичное чтение вернуло пусто (другая БД/профиль) — падаем на
            # приватный handle компрессора, чтобы не скипать сессию молча.
            fb = getattr(getattr(comp, "_session_db", None), "get_messages_as_conversation", None)
            if callable(fb):
                history = fb(session_id)
        messages = [m for m in history if m.get("role") in {"user", "assistant", "tool"}]
        from agent.model_metadata import estimate_messages_tokens_rough
        return int(estimate_messages_tokens_rough(messages))
    except Exception:
        return None


def _fire(session_id: str, stamp: float, model: str, ctx=None) -> None:
    with _timers_lock:
        _timers.pop(session_id, None)
    # Чужой дедлайн (другого процесса) не трогаем: его разбудит владелец.
    try:
        with _pending_guard():
            _entry = (_load_pending() or {}).get(session_id) or {}
        _owner = int(_entry.get("pid") or 0)
        if _owner and _owner != os.getpid() and _pid_alive(_owner):
            return
    except Exception:
        pass
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
        global _last_warm_usage
        _last_warm_usage = None
        comp = None
        res = _resident(session_id)
        agent_live = None
        route_live = ""
        runner_live = None
        if res is not None:
            agent_live, comp, route_live, runner_live = res
            floor = _gate_floor(comp, session_id)
            if floor is not None:
                # Реальный размер последнего промпта из usage API: оценка по
                # символам занижает в 2–3 раза (JSON, tool_calls, system prompt).
                api_size = int(getattr(comp, "last_prompt_tokens", 0) or 0)
                est = _estimate_history(comp, session_id)
                exact = max(api_size, est or 0) or None
        if floor is None:
            gate = _live_gate(comp, session_id)
            if gate is None:
                logger.info("idle-compact: session %s no live gate, skip", session_id)
                return
            floor = gate
        size = exact if exact is not None else _rough_tokens(session_id)
        idle_min = (time.time() - last_ts) / 60 if last_ts else -1
        if size <= floor:
            logger.info(
                "idle-compact: session %s size ~%d <= floor %d, skip "
                "(api_size=%s estimate=%s)",
                session_id, size, floor,
                getattr(comp, "last_prompt_tokens", "n/a"),
                exact,
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
            _do_compress(session_id, model, size, ctx, agent=agent_live,
                         route=route_live, runner=runner_live)
            return
        logger.info("idle-compact: write path not yet enabled, skip %s", session_id)
    except Exception:
        logger.exception("idle-compact: fire failed for %s", session_id)


_post_api_seen: set[str] = set()


def _on_post_api(session_id: str = "", **kwargs) -> None:
    """Ставим/сдвигаем таймер после каждого ответа модели.

    Хук ``on_session_end`` в Desktop-процессе не вызывается вовсе (проверено:
    после перезапуска 12:56 таймеры перестали ставиться, хотя ходы шли).
    ``post_api_request`` приходит на каждый ход и в обоих процессах, поэтому
    ожидание держится на нём.
    """
    if not session_id:
        logger.info("idle-compact: post_api hook reached WITHOUT session_id")
        return
    if session_id not in _post_api_seen:
        _post_api_seen.add(session_id)
        logger.info("idle-compact: post_api hook reached %s", session_id)
    try:
        _arm_engine(session_id)
    except Exception:
        logger.debug("idle-compact: engine arm failed", exc_info=True)
    try:
        _on_session_end(session_id=session_id)
    except Exception:
        logger.exception("idle-compact: timer hook failed for %s", session_id)


def _arm_engine(session_id: str) -> None:
    """Держим движок агента заряженным последним реальным запросом.

    Сжатие запускает не только наш таймер: ядро делает это само
    (``compression.prepare_ahead``, post-turn, inline при переполнении). Если у
    движка уже лежит свежий захват, любой из этих путей пойдёт по тёплому кешу
    без нашего участия — ровно то, что нужно для сокращения приватного кода.
    """
    cap = _capture_for(session_id)
    if not cap:
        return
    ts = float(cap.get("ts") or 0)
    # Старше TTL кеша повтор бессмысленен: префикса уже нет в кеше провайдера.
    if not ts or time.time() - ts > 600:
        return
    found = _resident(session_id)
    if not found:
        return
    agent, comp, _route, _runner = found
    if _test_settings(session_id).get("prepare_ahead"):
        # Диагностика: включаем ядровый prepare_ahead без перезапуска агента —
        # иначе он читается только при сборке (agent_init).
        try:
            from agent.prepared_compaction import PreparedCompaction
            if getattr(comp, "prepared_compaction", None) is None:
                comp.prepared_compaction = PreparedCompaction()
                logger.info("idle-compact: prepare_ahead включён для теста (%s)", session_id)
        except Exception:
            logger.debug("idle-compact: prepare_ahead enable failed", exc_info=True)
    begin = getattr(comp, "begin_warm", None)
    if not callable(begin):
        return
    route = {
        "api_mode": cap.get("api_mode") or getattr(agent, "api_mode", ""),
        "base_url": cap.get("base_url") or getattr(agent, "base_url", ""),
        "api_key": getattr(agent, "api_key", ""),
        "session_id": session_id,
        "provider": getattr(agent, "provider", ""),
        "_cap_ts": ts,
    }
    begin(cap, route)


def _on_session_end(session_id: str = "", **kwargs) -> None:
    if not session_id:
        return
    try:
        row = _session_row(session_id)
    except Exception:
        logger.exception("idle-compact: db read failed")
        return
    if not row or row.get("source") not in HUMAN_SOURCES:
        logger.debug(
            "idle-compact: session %s source %r not human, no timer",
            session_id, (row or {}).get("source"),
        )
        return
    model = row.get("model") or ""
    delay = _delay_for_model(model, session_id)
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
    gate = _live_gate(session_id=session_id)
    logger.info(
        "idle-compact: timer %.0fs for session %s model %s (gate %s | test=%s ttl=%s)",
        delay, session_id, model, gate if gate is not None else "n/a",
        _test_settings(session_id) or "-", _ttl_for_model(model),
    )


def _gate_floor(compressor, session_id: str | None = None) -> int | None:
    """Пол из живого порога компрессора + anti-thrash. Нет данных — None (скип)."""
    # Тестовый режим: явный гейт из файла имеет приоритет.
    test = _test_settings(session_id)
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
    frac = _gate_fraction(session_id)
    window_now = int(getattr(compressor, "context_length", 0) or 0)
    if frac > 0 and window_now > 0:
        # Порог от окна модели — приоритетнее цели и режима.
        return int(frac * window_now)
    if target is None:
        # Фактическое окно модели из живого компрессора — не выдуманное.
        window = int(getattr(compressor, "context_length", 0) or 0)
        target = _live_target(window_tokens=window or None)
    if target is None:
        return None
    floor = int(target * GATE_K)
    last = getattr(compressor, "last_compression_rough_tokens", 0)
    if isinstance(last, int) and not isinstance(last, bool) and last > 0:
        floor = max(floor, last + target)
    return floor


def _do_compress(session_id: str, model: str, size: int, ctx, agent=None,
                 route: str = "", runner=None) -> None:
    """Боевое сжатие через resident-агента: весь коммит делает ядро.

    ``agent`` приходит из ``_resident`` — он уже нашёл живого агента (в том
    числе через реестр Desktop-процесса). Искать его здесь заново нельзя:
    в ``hermes serve`` нет gateway-runner, и старая проверка скипала сессию,
    хотя агент был на руках.
    """
    if agent is None:
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
        logger.info("idle-compact: session %s api_mode=codex_app_server, skip", session_id)
        return
    if getattr(agent, "compression_checkpoint_required", False):
        logger.info("idle-compact: session %s checkpoint required, skip", session_id)
        return
    comp = getattr(agent, "context_compressor", None)
    if comp is None:
        return
    if getattr(comp, "awaiting_real_usage_after_compression", False):
        return
    cd = getattr(comp, "get_active_compression_failure_cooldown", None)
    try:
        if callable(cd) and cd():
            logger.info("idle-compact: session %s compressor cooldown, skip", session_id)
            return
    except Exception:
        logger.info("idle-compact: session %s cooldown check failed, skip", session_id)
        return
    get_conv = _history_getter(comp)
    if not callable(get_conv):
        logger.info("idle-compact: no db handle on compressor, skip %s", session_id)
        return
    try:
        history = get_conv(session_id)
        if not history:
            fb = getattr(getattr(comp, "_session_db", None), "get_messages_as_conversation", None)
            if callable(fb):
                history = fb(session_id)
    except Exception:
        logger.exception("idle-compact: history load failed")
        return
    messages = [m for m in history if m.get("role") in {"user", "assistant", "tool"}]
    try:
        from agent.model_metadata import estimate_messages_tokens_rough
        exact = int(estimate_messages_tokens_rough(messages))
    except Exception:
        exact = size
    floor = _gate_floor(comp, session_id)
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

    did_compress = False

    def work() -> None:
        nonlocal did_compress
        try:
            fence = None
            try:
                from agent.conversation_compression import CompressionCommitFence
                fence = CompressionCommitFence()
            except Exception:
                # Ядро само создаёт fence, если плагин его не передал
                # (agent/compression_facade.py): без него сжатие всё равно
                # защищено, поэтому здесь не отказываемся от работы.
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
                                "session_id": session_id,
                                "provider": getattr(agent, "provider", ""),
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
                                global _last_warm_usage
                                _last_warm_usage = stats
                                logger.info(
                                    "idle-compact WARM: cache_read=%s свежий_вход=%s выход=%s",
                                    stats.get("cache_read"), stats.get("fresh_input"),
                                    stats.get("output_tokens"),
                                )
                        except Exception:
                            logger.debug("idle-compact: end_warm failed", exc_info=True)
                    # Сжатие могло не состояться: ядро держит лизинг (другой
                    # воркер уже сжимает) или прервало попытку. Тогда агент
                    # выселять нельзя — уничтожим тёплый кеш без сжатия.
                    comp_obj = getattr(agent, "context_compressor", None)
                    skipped = bool(getattr(comp_obj, "_compression_skipped_due_to_lock", False))
                    aborted = bool(getattr(agent, "_last_compress_aborted", False))
                    shrank = True
                    if not (skipped or aborted):
                        try:
                            from agent.model_metadata import estimate_messages_tokens_rough
                            _now = get_conv(session_id)
                            _rows = [m for m in (_now or []) if isinstance(m, dict)]
                            shrank = int(estimate_messages_tokens_rough(_rows)) < int(size)
                        except Exception:
                            shrank = True
                    did_compress = not (skipped or aborted) and shrank
                    if not did_compress:
                        logger.info(
                            "idle-compact: сжатие не выполнено (lock=%s abort=%s shrank=%s) — агент не выселяем",
                            skipped, aborted, shrank,
                        )

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
                savings.end_compaction(session_id, after, warm_usage=_last_warm_usage)
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
            # Выселяем агента только если сжатие реально шло (иначе выкинем
            # живой агент без причины) и только когда в кеше лежит тот же
            # объект. Проверка неатомарна — это best-effort, как и в ядре;
            # не смогли подтвердить совпадение — не трогаем.
            if not did_compress or runner is None:
                return
            evict = getattr(runner, "_evict_cached_agent", None)
            if callable(evict):
                try:
                    same = False
                    try:
                        from gateway.run import _cached_agent_for_hygiene
                        same = _cached_agent_for_hygiene(runner, route) is agent
                    except Exception:
                        same = False
                    if same:
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
        # Дедлайн просрочен настолько, что кеш всё равно мёртв (реальный случай:
        # рестарт через 2.5 часа после дедлайна, сжатие ушло холодным).
        # Сжимать по такой метке бессмысленно — это сделает ядро при возврате.
        ttl = _delay_for_model(model, session_id)
        if now - deadline > max(ttl * 2, 3600):
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
# Поля запроса, которые живут только на стороне SDK и не должны уходить провайдеру.
_CLIENT_ONLY_KEYS = frozenset({
    "timeout", "http_client", "extra_headers", "extra_body", "extra_query",
    "max_retries", "stream", "stream_options",
})
_captures_lock = threading.Lock()
# Свой движок (если ядро его приняло)
_WARM_ENGINE = None


def _on_llm_execution(request=None, next_call=None, **kw) -> None:
    """Полный запрос к провайдеру — до всякой обрезки хуков.

    Хук ``pre_api_request`` урезает payload до 50 000 символов
    (``HERMES_PLUGIN_PAYLOAD_MAX_CHARS``), поэтому для реальной истории
    нужен middleware: он получает тот же dict, что уйдёт в провайдер.
    """
    try:
        sid = kw.get("session_id") or ""
        if sid and isinstance(request, dict):
            # Служебные поля SDK провайдер не принимает: "unknown parameter
            # `extra_headers`" — реальная ошибка 400 из логов.
            body = {
                k: v for k, v in request.items()
                if k not in _CLIENT_ONLY_KEYS
            }
            if body:
                with _captures_lock:
                    prev = _captures.get(sid)
                    _captures[sid] = {
                        "body": body,
                        "model": kw.get("model") or "",
                        "base_url": kw.get("base_url") or "",
                        "api_mode": kw.get("api_mode") or "",
                        "ts": time.time(),
                    }
                # Диагностика захвата: видно, что именно запомнили и насколько
                # это совпадает с реально отправленным запросом.
                # Диагностика захвата: те же метрики, что и у warm-запроса,
                # чтобы сверять построчно и видеть источник расхождения.
                _rows = body.get("input") or body.get("messages") or []
                _chars = 0
                for _r in _rows if isinstance(_rows, list) else []:
                    _c = _r.get("content") if isinstance(_r, dict) else None
                    if isinstance(_c, str):
                        _chars += len(_c)
                    elif isinstance(_c, list):
                        _chars += sum(len(str(_p.get("text") or "")) for _p in _c if isinstance(_p, dict))
                import json as _json
                _instr = body.get("instructions") or ""
                _tools = body.get("tools") or []
                try:
                    _tools_chars = len(_json.dumps(_tools, ensure_ascii=False))
                except Exception:
                    _tools_chars = -1
                logger.info(
                    "idle-compact: захват %s — строк=%d символов_истории=%d "
                    "instructions_символов=%d tools_символов=%d cache_key=%s",
                    sid, len(_rows) if isinstance(_rows, list) else 0, _chars,
                    len(_instr) if isinstance(_instr, str) else -1, _tools_chars,
                    body.get("prompt_cache_key"),
                )
    except Exception:
        logger.debug("idle-compact: middleware capture failed", exc_info=True)
    return next_call(request)


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
        mine = os.getpid()
        due = []
        for sid, e in state.items():
            if float(e.get("deadline") or 0) > now:
                continue
            owner = int(e.get("pid") or 0)
            if owner and owner != mine and _pid_alive(owner):
                continue  # чужой живой процесс сам разбудит своего агента
            due.append((sid, e))
    fired = 0
    for sid, entry in due:
        try:
            stamp = float(entry.get("stamp") or 0)
            model = str(entry.get("model") or "")
            deadline = float(entry.get("deadline") or 0)
            ttl = _delay_for_model(model, sid)
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
            # Цель и порог — из config.yaml: update_model() их не трогает,
            # поэтому дефолты конструктора (0.5/0.2) остались бы навсегда.
            eng_kwargs: dict = {"model": ""}
            try:
                import yaml

                with open(str(_home() / "config.yaml"), "r", encoding="utf-8") as fh:
                    _comp = (yaml.safe_load(fh) or {}).get("compression") or {}
                if _comp.get("target_ratio") is not None:
                    eng_kwargs["summary_target_ratio"] = float(_comp["target_ratio"])
                if _comp.get("threshold") is not None and _comp.get("threshold_tokens") is None:
                    _frac = float(_comp["threshold"])
                    if _frac <= 1:
                        eng_kwargs["threshold_percent"] = _frac
                if _comp.get("threshold_tokens") is not None:
                    eng_kwargs["threshold_tokens_cap"] = int(_comp["threshold_tokens"])
                if _comp.get("tail_mode") is not None:
                    _tm = str(_comp["tail_mode"]).strip().lower()
                    if _tm in ("legacy", "lean"):
                        eng_kwargs["tail_mode"] = _tm
            except Exception:
                logger.debug("idle-compact: engine config unreadable, defaults", exc_info=True)
            engine = EngineClass(**eng_kwargs)
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
        ctx.register_middleware("llm_execution", _on_llm_execution)
    except Exception:
        logger.exception("idle-compact: не удалось подписаться на захват запроса")

    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("post_api_request", _on_post_api)
    try:
        from . import savings
        ctx.register_hook("post_api_request", savings.on_post_api_request)
    except Exception:
        logger.exception("idle-compact: savings hooks not registered")
    logger.info(
        "idle-compact 0.9.13 loaded: DRY_RUN=%s WRITE_ENABLED=%s gate=live-per-fire",
        DRY_RUN, WRITE_ENABLED,
    )
    try:
        import yaml
        with open(str(_home() / "config.yaml"), "r", encoding="utf-8") as fh:
            _eng = ((yaml.safe_load(fh) or {}).get("context") or {}).get("engine", "")
        if str(_eng or "").strip().lower() not in ("idle_warm", "idle-warm"):
            logger.warning(
                "idle-compact: context.engine=%r — фон жмёт холодным aux без кеша; "
                "для тёплых саммари: hermes config set context.engine idle_warm",
                _eng or "compressor",
            )
    except Exception:
        pass
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
