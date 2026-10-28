"""Учёт экономии idle-compact: деньги и время, по каждому сжатию и по сессии.

Логика учёта (согласована с Ником 28.10.2026):

Сжатие в простое ломает префикс, поэтому следующий живой запрос идёт
холодным, но коротким. Сравниваем с миром без плагина:

  без сжатия:  холодное чтение N токенов по цене fresh
  со сжатием:  тёплое чтение N при сжатии + выход саммари
               + холодное чтение M на следующем живом ходе

Поэтому одно и то же сжатие может быть и плюсом, и минусом:

  gap >= TTL  — кэш всё равно умер бы. Плюс равен сэкономленным токенам
                (N - M), умноженным на цену ввода, которую реально заплатил
                этот запрос, минус стоимость самого сжатия.
  gap <  TTL  — юзер вернулся, кэш был бы ещё тёплым. Тогда плагин только
                потратил деньги: считаем как (что заплатили) минус
                (что заплатили бы тёплым по N).

Время: длительность самого сжатия — это ровно то ожидание, которое иначе
встало бы в критическом пути у порога.

Леджер — JSONL, дописывается, ничего не переписывает:
  ~/.hermes/idle_compact_ledger.jsonl
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Цены, $ за 1M токенов ────────────────────────────────────────────────
# Замерено живьём на opencode-go 28.10.2026, сходится до 7-го знака:
#   muse cold 40530 tok + 422 out = 0.0041374 | warm 40305 cached = 0.00022311
#   deepseek cold 40535 tok + 12 out = 0.0121749 | warm 40448 cached = 0.00028319
PRICES: dict[str, dict[str, float]] = {
    "muse-spark": {"fresh": 0.10, "read": 0.002, "out": 0.20},
    "deepseek-v4.1": {"fresh": 0.30, "read": 0.006, "out": 1.20},
}
_FALLBACK = {"fresh": 0.10, "read": 0.002, "out": 0.20}  # muse как самый частый

_lock = threading.Lock()
# session_id -> {"pending": {...}, "totals": {...}, "durs": [...]}
_state: dict[str, dict] = {}
# session_id -> {"start": ts, "cost": 0.0, "calls": 0, "before": N}
_compacting: dict[str, dict] = {}


def _home() -> Path:
    import os
    import sys

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
    return Path.home() / (".hermes")


def ledger_path() -> Path:
    return _home() / "idle_compact_ledger.jsonl"


def price_for(model: str) -> dict[str, float]:
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


# ── Хуки ────────────────────────────────────────────────────────────────


def begin_compaction(session_id: str, model: str, before_tokens: int) -> None:
    """Плагин стартует фоновое сжатие: открываем окно учёта его стоимости."""
    with _lock:
        _compacting[session_id] = {
            "start": time.time(),
            "cost": 0.0,
            "calls": 0,
            "before": int(before_tokens),
            "model": model,
        }


def end_compaction(session_id: str, after_tokens: int | None = None) -> None:
    """Сжатие закончилось: превращаем окно в pending-событие, ждём живой ход."""
    with _lock:
        win = _compacting.pop(session_id, None)
        if not win:
            return
        dur = time.time() - win["start"]
        st = _sess(session_id)
        st["durs"].append(dur)
        st["spent_usd"] += win["cost"]
        st["pending"] = {
            "ts": win["start"],
            "model": win["model"],
            "before": win["before"],
            "after": int(after_tokens or 0),
            "dur_s": round(dur, 1),
            "cost_usd": round(win["cost"], 6),
            "calls": win["calls"],
        }
        logger.info(
            "idle-compact SAVINGS: сжатие %s: %d -> %d токенов, %.1f сек, "
            "стоило $%.5f (%d вызовов), ждём живой ход",
            session_id, win["before"], int(after_tokens or 0), dur, win["cost"], win["calls"],
        )


def abort_compaction(session_id: str) -> None:
    with _lock:
        _compacting.pop(session_id, None)


def on_post_api_request(**kw) -> None:
    """Считаем стоимость каждого запроса; при компакции — приписываем сжатию."""
    sid = kw.get("session_id") or ""
    if not sid:
        return
    usage = kw.get("usage") or {}
    model = kw.get("model") or (kw.get("response_model") or "")
    inp = int(usage.get("input_tokens") or 0)
    read = int(usage.get("cache_read_tokens") or 0)
    write = int(usage.get("cache_write_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    prompt = int(usage.get("prompt_tokens") or (inp + read + write))
    p = price_for(model)
    cost = (
        inp / 1_000_000 * p["fresh"]
        + read / 1_000_000 * p["read"]
        + write / 1_000_000 * p["fresh"]
        + out / 1_000_000 * p["out"]
    )

    with _lock:
        win = _compacting.get(sid)
        if win is not None:
            win["cost"] += cost
            win["calls"] += 1
            return
        st = _sess(sid)
        pend = st.pop("pending", None) if st.get("pending") else None
        if pend is None:
            return

    if pend is None:
        return

    gap = max(0.0, kw.get("started_at") or time.time()) - pend["ts"] if kw.get("started_at") else time.time() - pend["ts"]
    ttl = _ttl_seconds(pend["model"])
    would_be_cold = gap >= ttl
    n_before = pend["before"]
    n_after = pend["after"] or prompt
    compact_cost = pend["cost_usd"]

    if would_be_cold:
        # Кэш всё равно протух бы: экономия = короткий промпт вместо длинного.
        weighted_in = (cost - out / 1_000_000 * p["out"]) / max(1, prompt)
        saved = (n_before - n_after) * weighted_in - compact_cost
        verdict = "предотвращён промах"
    else:
        # Юзер вернулся раньше TTL — кэш был бы тёплым, сжатие в минус.
        paid = cost
        would_pay_warm = cost_of(n_before, "read", pend["model"]) + out / 1_000_000 * p["out"]
        saved = would_pay_warm - paid - compact_cost
        verdict = "кэш был бы тёплым (зря сжали)"

    with _lock:
        st = _sess(sid)
        st["events"] += 1
        st["saved_usd"] += saved
        if saved > 0:
            st["profitable"] += 1
            # Время: столько клиент ждал бы у порога в критическом пути
            st["saved_s"] += threshold_compact_avg()
        else:
            st["wasted"] += 1

    ev = {
        "ev": "settle",
        "ts": round(time.time(), 1),
        "session": sid,
        "model": pend["model"],
        "before": n_before,
        "after": n_after,
        "compact_cost_usd": round(compact_cost, 6),
        "compact_s": pend["dur_s"],
        "gap_s": round(gap, 1),
        "ttl_s": ttl,
        "verdict": verdict,
        "saved_usd": round(saved, 6),
        "saved_wait_s": round(threshold_compact_avg(), 0) if saved > 0 else 0,
        "prompt_tokens": prompt,
        "cached_tokens": read,
    }
    _append(ev)
    logger.info(
        "idle-compact SAVINGS: %s | %s | пауза %.0f мин против TTL %.0f мин | "
        "сжатие $%.5f | экономия $%.5f + %.0f сек ожидания",
        sid, verdict, gap / 60, ttl / 60, compact_cost, saved,
        ev["saved_wait_s"],
    )


def _ttl_seconds(model: str) -> int:
    m = (model or "").strip().lower()
    if "deepseek-v4" in m:
        return 5100
    return 600  # muse-spark и прочее


# ── Время: сколько ждал бы клиент, если бы сжатие пришло в критический путь ──
# Собственная длительность фонового сжатия клиенту не видна — считать её
# «сэкономленным временем» бессмысленно. Считаем среднее по НАСТОЯЩИМ сжатиям
# у порога (их видно в gateway.log: «Session hygiene: … auto-compressing» →
# «… finished after the turn-hold was released»).
THRESHOLD_COMPACT_DEFAULT_S = 60.0
_AVG_CACHE: dict[str, float] = {"ts": 0.0, "value": THRESHOLD_COMPACT_DEFAULT_S}
_AVG_TTL_S = 3600.0


def threshold_compact_avg(days: int = 60) -> float:
    """Средняя длительность сжатия у порога по gateway.log, с кэшем на час."""
    now = time.time()
    if now - _AVG_CACHE["ts"] < _AVG_TTL_S:
        return _AVG_CACHE["value"]
    value = THRESHOLD_COMPACT_DEFAULT_S
    try:
        import datetime as _dt
        import re

        log = _home() / "logs" / "gateway.log"
        if log.exists():
            lines = log.read_text(encoding="utf-8", errors="ignore").splitlines()
            cutoff = _dt.datetime.now() - _dt.timedelta(days=days)

            def _ts(s: str) -> _dt.datetime:
                return _dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")

            starts: dict[str, _dt.datetime] = {}
            fins: dict[str, _dt.datetime] = {}
            for i, line in enumerate(lines):
                m = re.match(
                    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*Session hygiene: .*auto-compressing",
                    line,
                )
                if m:
                    t0 = _ts(m.group(1))
                    if t0 < cutoff:
                        continue
                    for nxt in lines[i:i + 4]:
                        mm = re.search(r"for session (\S+)", nxt)
                        if mm:
                            starts[mm.group(1)] = t0
                            break
                    continue
                m2 = re.match(
                    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*Session hygiene compression "
                    r"for session (\S+) finished",
                    line,
                )
                if m2:
                    fins[m2.group(2)] = _ts(m2.group(1))
            durs = [
                (fins[sid] - t0).total_seconds()
                for sid, t0 in starts.items()
                if sid in fins and (fins[sid] - t0).total_seconds() > 0
            ]
            if durs:
                value = sum(durs) / len(durs)
    except Exception:
        logger.exception("idle-compact: threshold avg parse failed")
    _AVG_CACHE["ts"] = now
    _AVG_CACHE["value"] = value
    return value


def session_summary(session_id: str) -> dict:
    """Сводка по сессии: деньги, время, сколько сжатий сработало в плюс/минус."""
    with _lock:
        st = _state.get(session_id)
        if not st:
            return {}
        durs = list(st["durs"])
        avg = sum(durs) / len(durs) if durs else 60.0
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
        "итого $%+.5f | съэкономлено времени %.0f сек | сжатий %d (в плюс %d, зря %d), "
        "среднее сжатие %.0f сек",
        session_id, s["saved_usd"], s["spent_usd"], s["net_usd"],
        s["saved_s"], s["events"], s["profitable"], s["wasted"], s["avg_compact_s"],
    )


def ledger_tail(limit: int = 20) -> list[dict]:
    """Последние записи леджера — для ручной проверки."""
    try:
        lines = ledger_path().read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(x) for x in lines if x.strip()]
    except Exception:
        return []
