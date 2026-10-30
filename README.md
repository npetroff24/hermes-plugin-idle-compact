# idle-compact

Background compaction for idle Hermes-agent chats — before prompt-cache expiry.

## The problem

Long chats hit the context limit, and stock compression hurts three times:
it fires mid-request — you wait tens of seconds; it computes **cold** — the
aux call bypasses prompt-cache (~0% hits by our measurements), so every
compaction is billed at full rate; and a long idle lets the cache expire
completely — your next turn re-reads the whole context at full price.

## Advantages

1. **Zero waiting.** Compacts ahead of time, while idle — you come back to an
   already-slim chat. No waiting on compaction, ever.
2. **Cache-warm, not cache-blind.** The summary request repeats your last turn
   and hits prompt-cache at 96–98% instead of 0% — the compaction itself costs
   pennies.
3. **Timing per model.** Every model has its own cache TTL: DeepSeek ~85 min,
   Spark/GPT/Gemini ~10 min, Claude/Kimi/Qwen/MiniMax and the rest ~5 min.
   Starts at 10% before expiry (TTL × 0.9).
4. **Skips the small stuff.** The gate comes from the live core compressor
   (target × 2) — short chats are skipped, nothing wasted.
5. **Money is visible.** Every compaction logs SAVINGS: before → after, cost;
   on your return — whether the entry was warm or cold and what was actually
   saved.
6. **Survives restarts.** Timers persist on disk — a gateway restart never
   breaks a scheduled compaction.
7. **Windows and Linux.** One codebase: locks via `msvcrt`/`fcntl`, paths via
   `%LOCALAPPDATA%`/`~/.hermes`.

Bottom line: smaller context — cheaper every answer, zero waiting, every
number in the log.

## Warm-path measurements (2026-10-29, opencode-go, live requests)

| model | transport | cache_read | fresh input | cache hit |
|---|---|---|---|---|
| muse-spark-1.3-contributor | codex_responses | 1393 | 83 | 94% |
| deepseek-v4.1-flash | chat_completions | 1792 | 46 | 97.5% |
| minimax-m3.1-flash | anthropic_messages | 1920 | 153 | 92.6% |

Summaries stay structural (Goal / Current state / Next step); tools and
reasoning are unaffected.

## How it works

1. After every turn — a TTL × 0.9 timer (live TTLs in `TTL_BY_MODEL_SUBSTR`:
   deepseek 85 min → 4590 s, spark/gpt/gemini 10 min → 540 s, rest 5 min →
   270 s).
2. On fire: session still quiet, human source, size above gate
   (gate = target × 2, target = live `threshold_tokens × target_ratio`).
3. The plugin grabs the last real request (`pre_api_request` hook) and enables
   the warm path on its own context engine.
4. The core does the compaction (`resident._compress_context`): locks,
   cooldown, anti-thrash, watermark against races. The plugin only swaps the
   summary LLM call — all middle-replacement logic stays core.
5. Warm unavailable (no capture, route changed, error) — automatic fallback to
   the stock aux path.
6. Small/young/cron sessions are left alone.

## Custom context engine

The plugin registers `idle_warm` — a subclass of the stock `ContextCompressor`
overriding a single method, `_call_summary_llm`. Nothing in the core is
patched: plain subclassing of a public class.

If the core changes that method's signature, the plugin disables warm and keeps
working via aux (`inspect.signature` check at startup).

Requires `context.engine: idle_warm` in config:

```bash
hermes config set context.engine idle_warm
```

## Install

```bash
hermes plugins install <git-url> --enable
hermes config set context.engine idle_warm
```

## Rollback

```bash
hermes config set context.engine compressor
hermes plugins disable idle-compact
```

Compaction archives rows (`active=0`), never deletes.

---

# idle-compact (на русском)

Фоновое сжатие простаивающих чатов Hermes-агента — до истечения prompt-cache.

## Проблема

Длинные чаты упираются в лимит контекста, а штатное сжатие бьёт трижды:
срабатывает в момент вашего запроса — вы ждёте десятки секунд; считает
**на холодную** — aux-вызов идёт мимо prompt-cache (~0% попаданий по нашим
замерам), каждое сжатие оплачивается по полному тарифу; а долгий простой
протухает кеш окончательно — следующий ход перечитывает весь контекст
по полной цене.

## Преимущества

1. **Ноль ожидания.** Сжимает заранее, в простое — возвращаетесь, чат уже
   ужатый. Ждать сжатия не приходится вообще.
2. **Сжатие по кешу, а не мимо.** Запрос суммирования повторяет последний ход
   и попадает в prompt-cache на 96–98% вместо 0% — само сжатие стоит копейки.
3. **Момент подбирает сам.** У каждой модели свой TTL кеша: DeepSeek ~85 мин,
   Spark/GPT/Gemini ~10 мин, Claude/Kimi/Qwen/MiniMax и остальные ~5 мин.
   Старт за 10% до протухания (TTL × 0.9).
4. **Мелочь не жмёт.** Порог берёт у живого компрессора ядра (цель × 2) —
   короткие чаты пропускает, зря не тратит.
5. **Деньги видны.** После каждого сжатия пишет SAVINGS: было → стало, цена;
   на вашем возврате — тёплым или холодным был вход и сколько реально
   сэкономлено.
6. **Переживает рестарт.** Таймеры сохраняются на диск — рестарт шлюза сжатие
   не срывает.
7. **Windows и Linux.** Один код: лок через `msvcrt`/`fcntl`, пути через
   `%LOCALAPPDATA%`/`~/.hermes`.

Итого: меньше контекст — дешевле каждый ответ, ноль ожидания сжатия, каждая
цифра в логе.

## Замеры warm-пути (29.10.2026, opencode-go, живые запросы)

| модель | транспорт | cache_read | свежий вход | кеш |
|---|---|---|---|---|
| muse-spark-1.3-contributor | codex_responses | 1393 | 83 | 94% |
| deepseek-v4.1-flash | chat_completions | 1792 | 46 | 97.5% |
| minimax-m3.1-flash | anthropic_messages | 1920 | 153 | 92.6% |

Саммари при этом остаётся структурным (Goal / Current state / Next step),
tools и reasoning не ломаются.

## Логика

1. После каждого хода — таймер TTL × 0.9 (боевые TTL по таблице
   `TTL_BY_MODEL_SUBSTR`: deepseek 85 мин → 4590 сек, spark/gpt/gemini
   10 мин → 540 сек, остальные 5 мин → 270 сек).
2. Сработал: сессия тиха, источник human, размер > гейта
   (гейт = цель × 2, цель = живой `threshold_tokens × target_ratio`).
3. Плагин захватывает последний реальный запрос (хук `pre_api_request`) и
   включает warm-путь на своём контекст-движке.
4. Сжатие делает ядро (`resident._compress_context`): локи, cooldown,
   anti-thrash, watermark против гонок. Плагин подменяет только LLM-вызов
   суммирования — вся логика замены середины остаётся ядровой.
5. Warm недоступен (нет захвата, сменился маршрут, ошибка) — автоматический
   откат на штатный aux-путь.
6. Маленькие/молодые/cron-сессии не трогаются.

## Свой контекст-движок

Плагин регистрирует `idle_warm` — наследник штатного `ContextCompressor`,
который переопределяет единственный метод `_call_summary_llm`. Ничего в ядре
не патчится: обычное наследование публичного класса.

Если ядро сменит сигнатуру этого метода, плагин отключит warm и продолжит
работать через aux (проверка `inspect.signature` на старте).

Нужен `context.engine: idle_warm` в конфиге:

```bash
hermes config set context.engine idle_warm
```

## Установка

```bash
hermes plugins install <git-url> --enable
hermes config set context.engine idle_warm
```

## Откат

```bash
hermes config set context.engine compressor
hermes plugins disable idle-compact
```

Сжатие архивирует строки (`active=0`), не удаляет.
