# idle-compact

Automatically compacts the context of long dialogs **before** the prompt
cache expires. Without it, compaction fires right after your message —
you wait ~1.5 min and pay for a cold read; with it, compaction runs while
you're away, over the warm cache (~50x cheaper), and there's nothing to
wait for. How: after every reply a timer of 0.9× the model's cache TTL is
armed; long silence plus context above 1.5× the compaction threshold —
the Hermes core compacts via its standard path.

Measured: ~10%+ savings on active days, plus minutes of waiting eliminated.

## How it works

1. After every reply a timer is armed: Spark — 9 min, DeepSeek — ~76 min.
   A new message resets and re-arms it.
2. Fired: silence, human source, context above 1.5× threshold —
   compact now, while reads go through the warm cache.
3. Compaction is done by the Hermes core via its standard path: locks,
   cooldown, anti-thrash, no lost messages on races.
4. Short/fresh/service dialogs are left alone.

## Supported models (opencode-go provider)

Measured on live sessions, TTLs baked in:

| Model | Cache TTL | Timer |
|---|---|---|
| `muse-spark-1.3-contributor` | 10 min | 9 min |
| `deepseek-v4.1-flash` | 85–90 min | ~76 min |

Other models: the plugin works (gate and compaction follow the core's
live threshold), but the timer defaults to 10 min — their TTLs aren't
measured yet. We'll measure from logs and add rows to the table.

## Install (one command)

Requires an installed Hermes Agent.

```bash
hermes plugins install npetroff24/hermes-plugin-idle-compact --enable
```

What happens: the plugin downloads from GitHub, enables itself and is
picked up by the live gateway without a restart. Nothing to edit by hand.

Verify it works (the line appears within a minute):

```bash
grep -a "idle-compact" ~/.hermes/logs/gateway.log | tail -2
# idle-compact 0.3.0 loaded: DRY_RUN=False WRITE_ENABLED=True gate=live-per-fire
```

Windows (PowerShell/CMD):

```cmd
findstr "idle-compact" %LOCALAPPDATA%\hermes\logs\gateway.log
```

After that the plugin arms timers after every reply and logs
`timer ...`, `COMPRESS START/DONE` into the same log.

## Rollback

```bash
hermes plugins disable idle-compact
```

Compaction archives rows (`active=0`), never deletes. Pre-flight snapshots
of the active session live in `rollback_<session>.json` (not in the repo).

---

## По-русски

Плагин автоматически сжимает контекст длинных диалогов до истечения кеша.
Зачем: иначе сжатие случается после твоего сообщения — ждёшь ~1,5 мин
и платишь за холодное чтение; с плагином сжатие идёт в простое по тёплому
кешу (в ~50 раз дешевле) и ждать не нужно. Как: после каждого ответа
ставится таймер 0,9×TTL модели; долгая тишина и контекст больше порога×1,5 —
сжимает ядро Hermes своим штатным путём.

Замерено: ~10%+ экономии на активных днях + минуты несъеденного ожидания.

Логика, модели, установка и откат — как в английской части выше.
Проверка на Windows: `findstr "idle-compact" %LOCALAPPDATA%\hermes\logs\gateway.log`.
