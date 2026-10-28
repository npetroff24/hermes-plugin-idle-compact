"""Учёт экономии idle-compact — по ФАКТИЧЕСКИМ токенам из state.db.

Почему так, а не через хуки: aux-вызовы сжатия не стреляют `post_api_request`
(компрессор идёт через `auxiliary_client.call_llm` → `post_auxiliary_call`),
поэтому ловить их хуком нельзя. Зато Hermes сам пишет по каждому такому
вызову строку в `session_model_usage` с `task='compression'` — с точными
`input_tokens`, `cache_read_tokens`, `output_tokens`.

Отсюда:
  * стоимость сжатия = дельта этих счётчиков до/после сжатия, по прайсу;
  * `cache_read_tokens` в дельте показывает ФАКТ — читалось ли по кешу
    (а не догадку «пауза против TTL»).

Сравнение с миром без плагина:

  пауза >= TTL (кеш всё равно умер бы):
      без плагина: aux читает N_before по цене fresh
      с плагином: фактические (input × fresh + cache_read × read + output × out)
      экономия = N_before × fresh − фактическая стоимость сжатия

  пауза <  TTL (кеш был бы ещё тёплым):
      без плагина: aux читал бы N_before по цене read
      экономия = N_before × read − фактическая стоимость сжатия  (обычно < 0)

Леджер — JSONL в `~/.hermes/idle_compact_ledger.jsonl`, дописывается.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Цены, $ за 1M токенов ────────────────────────────────────────────────
# Замерено живьём на opencode-go 28.10.2026 (сверено с биллингом до 4-го знака):
#   muse cold 40530 tok + 422 out = 0.0041374 | warm 40305 cached = 0.00022311
# DeepSeek: off-peak 0.15/0.003/0.60, peak 0.30/0.006/1.20 — берём peak
# (консервативно: завышает расход, значит экономию не завышает).
PRICES: dict[str, dict[str, float]] = {
    "muse-spark": {"fresh": 0.10, "read": 0.002, "out": 0.20},
    "deepseek-v4.1": {"fresh": 0.30, "read": 0.006, "out": 1.20},
    "deepseek-v4": {"fresh": 0.14, "read": 0.0028, "out": 0.28},
    "mimo-v2": {"fresh": 0.14, "read": 0.0028, "out": 0.28},
}
_FALLBACK = {"fresh": 0.10, "read": 0.002, "out": 0.20}

_lock = threading.Lock()
_state: dict[str, dict] = {}
_compacting: dict[str, dict] = {}


# ── Дом и пути ───────────────────────────────────────────────────────────


def _home() -> Path:
    import os

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
    return Path.home() / ".hermes"


def ledger_path() -> Path:
    return _home() / "idle_compact_ledger.jsonl"


def state_db_path() -> Path:
    return _home() / "state.db"


# ── Цены ─────────────────────────────────────────────────────────────────


def price_for(model: str) -> dict[str, float]:
    """Цена по подстроке модели, longest-match wins."""
    m = (model or "").strip().lower()
    best = -1
    pick = _FALLBACK
    for sub, p in PRICES.items():
        if sub in m and len(sub) > best:
            best = len(sub)
            pick = p
    return pick


def cost_of(tokens: int, kind: str, model: str) -> float:
    p = price_for(model)
    return max(0, int(tokens)) / 1_000_000 * p[kind]


def cost_of_facts(facts: dict, model: str) -> float:
    """Стоимость по фактическим токенам: input, cache_read, output."""
    p = price_for(model)
    return (
        max(0, int(facts.get("input", 0))) / 1_000_000 * p["fresh"]
        + max(0, int(facts.get("cache_read", 0))) / 1_000_000 * p["read"]
        + max(0, int(facts.get("output", 0))) / 1_000_000 * p["out"]
    )


# ── Фактические счётчики сжатия из state.db ──────────────────────────────


def compression_counters(
    session_id: str, model: str = "", db_path: Path | None = None
) -> dict:
    """(input, cache_read, output, calls) по task='compression' для сессии.

    Читает ту же строку, которую пишет ядро на каждый aux-вызов сжатия.
    Модель не фильтруем: смена модели посреди сессии не должна терять вызовы.
    """
    facts = {"input": 0, "cache_read": 0, "output": 0, "calls": 0}
    if not session_id:
        return facts
    path = db_path or state_db_path()
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except Exception:
        logger.debug("idle-compact: state.db open failed", exc_info=True)
        return facts
    try:
        row = con.execute(
            "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(cache_read_tokens),0), "
            "COALESCE(SUM(output_tokens),0), COALESCE(SUM(api_call_count),0) "
            "FROM session_model_usage WHERE session_id = ? AND task = 'compression'",
            (session_id,),
        ).fetchone()
        if row:
            facts = {
                "input": int(row[0] or 0),
                "cache_read": int(row[1] or 0),
                "output": int(row[2] or 0),
                "calls": int(row[3] or 0),
            }
    except Exception:
        logger.debug("idle-compact: compression counters read failed", exc_info=True)
    finally:
        con.close()
    return facts


def delta(before: dict, after: dict) -> dict:
    """Разница счётчиков сжатия: что потратил именно этот проход."""
    return {
        "input": max(0, int(after.get("input", 0)) - int(before.get("input", 0))),
        "cache_read": max(0, int(after.get("cache_read", 0)) - int(before.get("cache_read", 0))),
        "output": max(0, int(after.get("output", 0)) - int(before.get("output", 0))),
        "calls": max(0, int(after.get("calls", 0)) - int(before.get("calls", 0))),
    }


# ── Чистый расчёт экономии (тестируется без сети и без базы) ─────────────


def compute_saving(
    *, facts: dict, gap_s: float, ttl_s: int, model: str
) -> tuple[float, str, float]:
    """(экономия $, вердикт, полная стоимость сжатия $).

    ``gap_s`` — сколько прошло ОТ НАШЕГО СЖАТИЯ до возврата пользователя
    (не пауза перед сжатием: она всегда 0.9×TTL и кеш там жив).

    Читается не весь контекст, а только «середина» (input + cache_read из
    фактических счётчиков), поэтому baseline строится по этому же объёму:
    без плагина те же токены прочитались бы по цене fresh (кеш мёртв) или
    read (кеш жив). Генерация саммари (output) есть в обоих мирах и
    сокращается — иначе тёплый случай показывал бы ложный минус.
    """
    fresh = price_for(model)["fresh"]
    read = price_for(model)["read"]
    out_price = price_for(model)["out"]
    read_tokens = max(0, int(facts.get("input", 0))) + max(0, int(facts.get("cache_read", 0)))
    out_tokens = max(0, int(facts.get("output", 0)))
    compact_cost = cost_of_facts(facts, model)
    would_be_cold = gap_s >= ttl_s

    if would_be_cold:
        baseline = read_tokens / 1_000_000 * fresh + out_tokens / 1_000_000 * out_price
        verdict = "предотвращён холодный вход"
    else:
        baseline = read_tokens / 1_000_000 * read + out_tokens / 1_000_000 * out_price
        verdict = "кеш был бы тёплым (зря сжали)"
    return baseline - compact_cost, verdict, compact_cost


def ttl_seconds(model: str) -> int:
    m = (model or "").strip().lower()
    if "deepseek-v4" in m:
        return 5100
    return 600


# ── Леджер ───────────────────────────────────────────────────────────────


def _append(ev: dict) -> None:
    try:
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("idle-compact: ledger write failed")


def _sess(session_id: str) -> dict:
    st = _state.get(session_id)
    if st is None:
        st = {
            "pending": None,
            "saved_usd": 0.0,
            "saved_s": 0.0,
            "events": 0,
            "profitable": 0,
            "wasted": 0,
            "spent_usd": 0.0,
            "durs": [],
        }
        _state[session_id] = st
    return st


# ── Хуки жизненного цикла сжатия ─────────────────────────────────────────


def begin_compaction(session_id: str, model: str, before_tokens: int) -> None:
    """Сжатие стартует: запоминаем счётчики сжатия и размер до."""
    with _lock:
        _compacting[session_id] = {
            "start": time.time(),
            "before": int(before_tokens),
            "model": model,
            "facts0": compression_counters(session_id),
        }


def end_compaction(session_id: str, after_tokens: int | None = None) -> None:
    """Сжатие закончилось: фиксируем фактическую стоимость прохода."""
    with _lock:
        win = _compacting.pop(session_id, None)
        if not win:
            return
        dur = time.time() - win["start"]
        facts = delta(win.get("facts0") or {}, compression_counters(session_id))
        compact_cost = cost_of_facts(facts, win["model"])
        st = _sess(session_id)
        st["durs"].append(dur)
        st["spent_usd"] += compact_cost
        st["pending"] = {
            "ts": win["start"],
            "model": win["model"],
            "before": win["before"],
            "after": int(after_tokens or 0),
            "dur_s": round(dur, 1),
            "cost_usd": round(compact_cost, 6),
            "facts": facts,
        }
    logger.info(
        "idle-compact SAVINGS: сжатие %s: %d -> %d токенов, %.1f сек, "
        "факт input=%d cache_read=%d out=%d, стоило $%.5f (вызовов %d), ждём живой ход",
        session_id, win["before"], int(after_tokens or 0), dur,
        facts["input"], facts["cache_read"], facts["output"], compact_cost, facts["calls"],
    )


def abort_compaction(session_id: str) -> None:
    with _lock:
        _compacting.pop(session_id, None)


def on_post_api_request(**kw) -> None:
    """Живой вызов после фонового сжатия — момент расчёта.

    Стоимость самого запроса не нужна: сравнение идёт по фактическим
    токенам сжатия из базы, а не по этому вызову.
    """
    sid = kw.get("session_id") or ""
    if not sid:
        return
    with _lock:
        if sid in _compacting:
            return
        st = _state.get(sid)
        if not st or not st.get("pending"):
            return
        pend = st["pending"]
        st["pending"] = None

    now = time.time()
    started = kw.get("started_at") or now
    gap = max(0.0, float(started) - float(pend["ts"]))
    ttl = ttl_seconds(pend["model"])
    saved, verdict, compact_cost = compute_saving(
        facts=pend.get("facts") or {},
        gap_s=gap,
        ttl_s=ttl,
        model=pend["model"],
    )

    with _lock:
        st = _sess(sid)
        st["events"] += 1
        st["saved_usd"] += saved
        if saved > 0:
            st["profitable"] += 1
            st["saved_s"] += float(pend.get("dur_s") or 0.0)
        else:
            st["wasted"] += 1

    ev = {
        "ev": "settle",
        "ts": round(now, 1),
        "session": sid,
        "model": pend["model"],
        "before": pend["before"],
        "after": pend["after"],
        "compact_cost_usd": round(compact_cost, 6),
        "compact_s": pend["dur_s"],
        "facts": pend.get("facts"),
        "gap_s": round(gap, 1),
        "ttl_s": ttl,
        "verdict": verdict,
        "saved_usd": round(saved, 6),
        "saved_wait_s": round(float(pend.get("dur_s") or 0.0), 1) if saved > 0 else 0,
    }
    _append(ev)
    logger.info(
        "idle-compact SAVINGS: %s | %s | возврат через %.0f мин после сжатия "
        "(TTL %.0f мин) | сжатие $%.5f | экономия $%+.5f",
        sid, verdict, gap / 60, ttl / 60, compact_cost, saved,
    )


# ── Сводка ───────────────────────────────────────────────────────────────


def session_summary(session_id: str) -> dict:
    with _lock:
        st = _state.get(session_id)
        if not st:
            return {}
        durs = list(st["durs"])
        avg = sum(durs) / len(durs) if durs else 0.0
        return {
            "saved_usd": round(st["saved_usd"], 5),
            "spent_usd": round(st["spent_usd"], 5),
            "net_usd": round(st["saved_usd"] - st["spent_usd"], 5),
            "saved_s": round(st["saved_s"], 1),
            "events": st["events"],
            "profitable": st["profitable"],
            "wasted": st["wasted"],
            "avg_compact_s": round(avg, 1),
        }


def log_summary(session_id: str) -> None:
    s = session_summary(session_id)
    if not s:
        return
    logger.info(
        "idle-compact SUMMARY: сессия %s | экономия $%.5f, потрачено $%.5f, "
        "итого $%+.5f | время %.0f сек | сжатий %d (в плюс %d, зря %d), среднее %.0f сек",
        session_id, s["saved_usd"], s["spent_usd"], s["net_usd"], s["saved_s"],
        s["events"], s["profitable"], s["wasted"], s["avg_compact_s"],
    )


def ledger_tail(limit: int = 20) -> list[dict]:
    try:
        lines = ledger_path().read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(x) for x in lines if x.strip()]
    except Exception:
        return []
